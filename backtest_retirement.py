import os
import shutil
import pandas as pd
import numpy as np
from dateutil.relativedelta import relativedelta
import time
from multiprocessing import Pool, cpu_count
import json
import sys

from db import DatabaseBackend, database_extension

# Load configuration
with open(os.path.join(os.path.dirname(__file__), 'retirement_config.json'), 'r') as f:
    config = json.load(f)

# Configuration parameters
INITIAL_PORTFOLIO = config['INITIAL_PORTFOLIO']
RETIREMENT_PERIODS = config['RETIREMENT_PERIODS']
ALLOCATIONS = config['ALLOCATIONS']
WITHDRAWAL_RATES = config['WITHDRAWAL_RATES']
FINAL_VALUE_TARGETS = config['FINAL_VALUE_TARGETS']

# Write a complete monthly path for every execution
SAVE_ALL_PATHS = config['SAVE_ALL_PATHS']
DB_TYPE = config.get('DB_TYPE', None)
DB_FILE = config.get('DB_FILE', 'backtest_retirement.duckdb')
TEMP_DIR = os.path.join(os.path.dirname(__file__), config['TEMP_DIR'])
WORKER_DB_CONN = None


def quote_path(path):
    return path.replace("'", "''")


def format_date(date):
    if isinstance(date, str):
        return date
    try:
        return date.strftime('%Y-%m-%d')
    except Exception:
        return pd.Timestamp(date).strftime('%Y-%m-%d')


def init_worker(temp_dir, db_type, worker_extension):
    global WORKER_DB_CONN
    os.makedirs(temp_dir, exist_ok=True)
    worker_db_path = os.path.join(temp_dir, f'worker_{os.getpid()}{worker_extension}')
    if os.path.exists(worker_db_path):
        os.remove(worker_db_path)
    WORKER_DB_CONN = DatabaseBackend.open(worker_db_path, db_type=db_type)
    WORKER_DB_CONN.configure_worker()
    WORKER_DB_CONN.create_tables(SAVE_ALL_PATHS)


def prepare_temp_dir(temp_dir):
    if os.path.exists(temp_dir):
        for filename in os.listdir(temp_dir):
            file_path = os.path.join(temp_dir, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
    else:
        os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def merge_temp_databases(output_dir, db_file, temp_dir, save_all_paths, db_type):
    final_db_path = os.path.join(output_dir, db_file)
    if os.path.exists(final_db_path):
        os.remove(final_db_path)

    os.makedirs(output_dir, exist_ok=True)
    conn = DatabaseBackend.open(final_db_path, db_type=db_type)
    conn.create_tables(save_all_paths)

    worker_files = sorted(
        [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith(database_extension(db_file))]
    )
    print(f"Merging {len(worker_files)} worker databases...", flush=True)
    for idx, worker_file in enumerate(worker_files, start=1):
        attach_name = f'worker_{idx-1}'
        conn.attach(attach_name, worker_file)
        conn.execute(f"INSERT INTO simulation_results SELECT * FROM {attach_name}.simulation_results")
        if save_all_paths:
            conn.execute(f"INSERT INTO simulation_paths SELECT * FROM {attach_name}.simulation_paths")
        conn.detach(attach_name)
        print(f"  Merged worker {idx}/{len(worker_files)}", flush=True)

    conn.close()
    return final_db_path


def create_indexes(db_path, save_all_paths, db_type):
    conn = DatabaseBackend.open(db_path, db_type=db_type)
    conn.create_indexes(save_all_paths)
    conn.close()


def load_data():
    dirscript = os.path.dirname(os.path.abspath(__file__))
    input_dir = os.path.normpath(os.path.join(dirscript, '..', 'input'))

    sp500 = pd.read_csv(os.path.join(input_dir, 'sp500_tr.csv'))
    bonds = pd.read_csv(os.path.join(input_dir, 'treasury_10y.csv'))
    cpi = pd.read_csv(os.path.join(input_dir, 'cpi.csv'))

    # Convert dates
    sp500['Fecha'] = pd.to_datetime(sp500['Fecha'], format='%m/%Y')
    bonds['Fecha'] = pd.to_datetime(bonds['Fecha'], format='%m/%Y')
    cpi['Fecha'] = pd.to_datetime(cpi['Fecha'], format='%m/%Y')

    # Convert values to float
    sp500['SPX-TR'] = sp500['SPX-TR'].str.replace(',', '').astype(float)
    bonds['10Y BM'] = bonds['10Y BM'].str.replace(',', '').astype(float)
    cpi['CPI'] = cpi['CPI'].astype(float)

    # Merge data
    data = pd.merge(sp500, bonds, on='Fecha', how='inner')
    data = pd.merge(data, cpi, on='Fecha', how='inner')
    data = data.sort_values('Fecha').reset_index(drop=True)

    # Rename columns for convenience
    data.columns = ['Fecha', 'SP500', 'Bonds', 'CPI']

    data['SP500_Return'] = data['SP500'].pct_change()
    data['Bonds_Return'] = data['Bonds'].pct_change()
    data['CPI_Return'] = data['CPI'].pct_change()

    data['SP500_Real_Return'] = (1 + data['SP500_Return']) / (1 + data['CPI_Return']) - 1
    data['Bonds_Real_Return'] = (1 + data['Bonds_Return']) / (1 + data['CPI_Return']) - 1

    return data

# Load data
data = load_data()


# Adjust to match the study: Feb 1871 - Dec 2016
# Dynamically calculate the maximum date based on the period
data_end = pd.to_datetime('2016-12-01')
data_start = pd.to_datetime('1871-02-01')

# Generate all possible start dates for the minimum period
min_period = min(RETIREMENT_PERIODS)
max_start_date = data_end - relativedelta(years=min_period)

current_date = data_start
start_dates = []
while current_date <= max_start_date:
    start_dates.append(current_date)
    current_date += relativedelta(months=1)

# Convert global arrays for workers
fecha_array = data['Fecha'].to_numpy()
sp500_real = data['SP500_Real_Return'].to_numpy()
bonds_real = data['Bonds_Real_Return'].to_numpy()


def run_portfolio_path(start_idx, target_sp500_pct, target_bonds_pct, monthly_withdrawal, max_months):
    sp500_value = INITIAL_PORTFOLIO * target_sp500_pct
    bonds_value = INITIAL_PORTFOLIO * target_bonds_pct

    path_dates = []
    path_sp500 = []
    path_bonds = []
    path_total = []
    path_withdrawals = []
    path_min = []
    path_max = []

    current_min = INITIAL_PORTFOLIO
    current_max = INITIAL_PORTFOLIO

    for idx in range(start_idx, start_idx + max_months):
        sp500_value *= (1 + sp500_real[idx])
        bonds_value *= (1 + bonds_real[idx])

        current_total = sp500_value + bonds_value
        withdrawal_remaining = monthly_withdrawal

        current_sp500_pct = sp500_value / current_total if current_total > 0 else 0
        current_bonds_pct = bonds_value / current_total if current_total > 0 else 0

        if current_sp500_pct > target_sp500_pct and sp500_value > 0:
            excess_sp500 = sp500_value - (current_total * target_sp500_pct)
            withdrawal_from_sp500 = min(excess_sp500, withdrawal_remaining)
            sp500_value -= withdrawal_from_sp500
            withdrawal_remaining -= withdrawal_from_sp500

        if withdrawal_remaining > 0 and current_bonds_pct > target_bonds_pct and bonds_value > 0:
            excess_bonds = bonds_value - (current_total * target_bonds_pct)
            withdrawal_from_bonds = min(excess_bonds, withdrawal_remaining)
            bonds_value -= withdrawal_from_bonds
            withdrawal_remaining -= withdrawal_from_bonds

        if withdrawal_remaining > 0:
            total_after_partial = sp500_value + bonds_value
            if total_after_partial > 0:
                sp500_withdrawal = withdrawal_remaining * (sp500_value / total_after_partial)
                bonds_withdrawal = withdrawal_remaining * (bonds_value / total_after_partial)
                sp500_value -= min(sp500_value, sp500_withdrawal)
                bonds_value -= min(bonds_value, bonds_withdrawal)

        current_total = sp500_value + bonds_value
        if current_total > 0:
            sp500_value = current_total * target_sp500_pct
            bonds_value = current_total * target_bonds_pct

        portfolio_value = sp500_value + bonds_value
        withdrawn_amount = monthly_withdrawal - withdrawal_remaining

        current_min = min(current_min, portfolio_value)
        current_max = max(current_max, portfolio_value)

        path_dates.append(fecha_array[idx])
        path_sp500.append(sp500_value)
        path_bonds.append(bonds_value)
        path_total.append(max(0, portfolio_value))
        path_withdrawals.append(max(0.0, withdrawn_amount))
        path_min.append(current_min)
        path_max.append(current_max)

        if portfolio_value <= 0:
            break

    return {
        'dates': path_dates,
        'sp500': path_sp500,
        'bonds': path_bonds,
        'total': path_total,
        'withdrawals': path_withdrawals,
        'min_values': path_min,
        'max_values': path_max,
    }


def insert_path_rows(start_date, allocation, withdrawal_rate, path_data):
    if not SAVE_ALL_PATHS or WORKER_DB_CONN is None:
        return

    allocation_str = f"{allocation[0]}/{allocation[1]}"
    start_date_str = format_date(start_date)
    rows = []
    for month_index, date in enumerate(path_data['dates'], start=1):
        rows.append(
            (start_date_str,
             allocation_str,
             float(withdrawal_rate),
             int(month_index),
             format_date(date),
             float(path_data['sp500'][month_index - 1]),
             float(path_data['bonds'][month_index - 1]),
             float(path_data['total'][month_index - 1]),
             float(path_data['withdrawals'][month_index - 1]),
             float(path_data['min_values'][month_index - 1]),
             float(path_data['max_values'][month_index - 1]))
        )

    if rows:
        WORKER_DB_CONN.execute('BEGIN TRANSACTION')
        WORKER_DB_CONN.executemany(
            '''INSERT INTO simulation_paths
               (start_date, allocation, withdrawal_rate, month, date, sp500, bonds, total, withdrawal, min_value, max_value)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            rows
        )
        WORKER_DB_CONN.execute('COMMIT')


def insert_simulation_results(result_records):
    if WORKER_DB_CONN is None or not result_records:
        return

    rows = []
    for record in result_records:
        rows.append(
            (record['start_date'].strftime('%Y-%m-%d'),
             record['end_date'].strftime('%Y-%m-%d'),
             str(record['allocation']),
             float(record['withdrawal_rate']),
             int(record['retirement_period']),
             float(record['final_value_target']),
             float(record['final_value']),
             bool(record['success']),
             int(record['months_lasted']),
             float(record['years_lasted']),
             float(record['min_value']),
             float(record['max_value']),
             float(record['total_withdrawn']))
        )

    WORKER_DB_CONN.execute('BEGIN TRANSACTION')
    WORKER_DB_CONN.executemany(
        '''INSERT INTO simulation_results
           (start_date, end_date, allocation, withdrawal_rate, retirement_period,
            final_value_target, final_value, success, months_lasted, years_lasted,
            min_value, max_value, total_withdrawn)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        rows
    )
    WORKER_DB_CONN.execute('COMMIT')


def worker_simulation(task):
    task_index, start_date_idx, allocation, withdrawal_rate = task
    start_idx = start_date_idx
    start_date = pd.Timestamp(fecha_array[start_idx])
    available_months = len(fecha_array) - start_idx

    # Validate that we have at least the minimum period
    min_months = min(RETIREMENT_PERIODS) * 12
    if available_months < min_months:
        return []

    # determine the periods we can simulate fully
    valid_periods = [p for p in RETIREMENT_PERIODS if p * 12 <= available_months]
    if not valid_periods:
        return []

    max_period_months = max(valid_periods) * 12

    sp500_pct, bonds_pct = allocation
    target_sp500_pct = sp500_pct / 100
    target_bonds_pct = bonds_pct / 100
    monthly_withdrawal = (INITIAL_PORTFOLIO * withdrawal_rate / 100) / 12

    path_data = run_portfolio_path(start_idx, target_sp500_pct, target_bonds_pct, monthly_withdrawal, max_period_months)
    withdrawals = path_data['withdrawals']
    months_lasted = len(withdrawals)

    result_records = []
    for retirement_period in valid_periods:
        months_needed = retirement_period * 12
        survived = months_lasted >= months_needed

        if survived:
            final_value = path_data['total'][months_needed - 1]
            min_value = path_data['min_values'][months_needed - 1]
            max_value = path_data['max_values'][months_needed - 1]
            total_withdrawn = sum(withdrawals[:months_needed])
            end_date = start_date + relativedelta(months=months_needed)
        else:
            final_value = path_data['total'][-1]
            min_value = path_data['min_values'][-1]
            max_value = path_data['max_values'][-1]
            total_withdrawn = sum(withdrawals)
            end_date = start_date + relativedelta(months=months_lasted)

        for final_value_target in FINAL_VALUE_TARGETS:
            needed_value = INITIAL_PORTFOLIO * final_value_target
            target_success = survived and (final_value >= needed_value)

            result_records.append({
                'start_date': start_date,
                'end_date': end_date,
                'allocation': f"{allocation[0]}/{allocation[1]}",
                'withdrawal_rate': withdrawal_rate,
                'retirement_period': retirement_period,
                'final_value_target': final_value_target,
                'final_value': final_value,
                'success': target_success,
                'months_lasted': months_lasted,
                'years_lasted': months_lasted / 12,
                'min_value': min_value,
                'max_value': max_value,
                'total_withdrawn': total_withdrawn,
            })

    if SAVE_ALL_PATHS:
        insert_path_rows(start_date, allocation, withdrawal_rate, path_data)

    insert_simulation_results(result_records)
    return len(result_records)


if __name__ == '__main__':
    print(f"Possible start dates (minimum {min_period} years): {len(start_dates)}")
    print(f"From: {start_dates[0].strftime('%m/%Y')} to {start_dates[-1].strftime('%m/%Y')}\n")

    # Prepare tasks and run the pool
    pool_tasks = [(i, idx, alloc, wr) for i, (idx, alloc, wr) in enumerate((idx, alloc, wr) for idx in range(len(start_dates)) for alloc in ALLOCATIONS for wr in WITHDRAWAL_RATES)]
    processed = 0
    total_records = 0
    start_time = time.time()
    max_workers = max(1, cpu_count() - 1)

    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    prepare_temp_dir(TEMP_DIR)
    worker_extension = database_extension(DB_FILE)

    print(f"Running {len(pool_tasks)} simulations in parallel using {max_workers} processes...\n")
    pool_kwargs = {
        'processes': max_workers,
        'initializer': init_worker,
        'initargs': (TEMP_DIR, DB_TYPE, worker_extension),
    }
    use_carriage = sys.stdout.isatty()

    chunksize = max(1, len(pool_tasks) // (max_workers * 4))

    with Pool(**pool_kwargs) as pool:
        for result_batch in pool.imap_unordered(worker_simulation, pool_tasks, chunksize=chunksize):
            processed += 1
            if result_batch:
                total_records += result_batch

            if processed % 1000 == 0:
                elapsed = time.time() - start_time
                rate = processed / elapsed
                remaining_seconds = int((len(pool_tasks) - processed) / rate)
                remaining_minutes = remaining_seconds // 60
                remaining_secs = remaining_seconds % 60
                status = f"Processed: {processed}/{len(pool_tasks)} - Remaining time: {remaining_minutes}m {remaining_secs}s"
                if use_carriage:
                    print(status.ljust(80), end='\r', flush=True)
                else:
                    print(status.ljust(80), flush=True)

    status = f"Processed: {processed}/{len(pool_tasks)} - Completed"
    print(status.ljust(80), flush=True)
    
    print("Merging results...", flush=True)
    final_db_path = merge_temp_databases(output_dir, DB_FILE, TEMP_DIR, SAVE_ALL_PATHS, DB_TYPE)
    print(f"\n✓ Analysis complete. Merged worker databases into {final_db_path}")

    create_indexes(final_db_path, SAVE_ALL_PATHS, DB_TYPE)
    print("✓ Indexes created.")

    if os.path.exists(TEMP_DIR):
        try:
            shutil.rmtree(TEMP_DIR)
            print(f"✓ Cleaned up temp directory: {TEMP_DIR}")
        except Exception as e:
            print(f"⚠ Warning: Could not remove temp directory {TEMP_DIR}: {e}")
