import os
import shutil
import pandas as pd
import numpy as np
from dateutil.relativedelta import relativedelta
import time
from multiprocessing import Pool, cpu_count, Lock
import json
import sys

from db import DatabaseBackend, create_central_database

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
DATA_START = config['DATA_START']
DATA_END = config['DATA_END']
INPUT_DIR = config['INPUT_DIR']
STOCKS_FILE = config['STOCKS_FILE']
STOCKS_COLUMN = config['STOCKS_COLUMN']
BONDS_FILE = config['BONDS_FILE']
BONDS_COLUMN = config['BONDS_COLUMN']
CPI_FILE = config['CPI_FILE']
CPI_COLUMN = config['CPI_COLUMN']
WORKER_DB_CONN = None
CENTRAL_DB_LOCK = None  # Shared lock for database access
CENTRAL_DB_PATH = None  # Path to central database


def quote_path(path):
    return path.replace("'", "''")


def format_date(date):
    if isinstance(date, str):
        return date
    try:
        return date.strftime('%Y-%m-%d')
    except Exception:
        return pd.Timestamp(date).strftime('%Y-%m-%d')


def init_worker_shared(central_db_path, db_type, lock):
    """Initialize worker to use shared central database."""
    global WORKER_DB_CONN, CENTRAL_DB_LOCK, CENTRAL_DB_PATH
    WORKER_DB_CONN = DatabaseBackend.open(central_db_path, db_type=db_type)
    WORKER_DB_CONN.configure_worker()
    CENTRAL_DB_LOCK = lock
    CENTRAL_DB_PATH = central_db_path


def create_indexes(db_path, save_all_paths, db_type):
    conn = DatabaseBackend.open(db_path, db_type=db_type)
    conn.create_indexes(save_all_paths)
    conn.close()


def load_data():
    dirscript = os.path.dirname(os.path.abspath(__file__))
    input_dir = os.path.normpath(os.path.join(dirscript, INPUT_DIR))

    stocks_df = pd.read_csv(os.path.join(input_dir, STOCKS_FILE))
    bonds_df = pd.read_csv(os.path.join(input_dir, BONDS_FILE))
    cpi_df = pd.read_csv(os.path.join(input_dir, CPI_FILE))

    # Convert dates
    stocks_df['Fecha'] = pd.to_datetime(stocks_df['Fecha'], format='%m/%Y')
    bonds_df['Fecha'] = pd.to_datetime(bonds_df['Fecha'], format='%m/%Y')
    cpi_df['Fecha'] = pd.to_datetime(cpi_df['Fecha'], format='%m/%Y')

    # Convert values to float
    stocks_df[STOCKS_COLUMN] = stocks_df[STOCKS_COLUMN].str.replace(',', '').astype(float)
    bonds_df[BONDS_COLUMN] = bonds_df[BONDS_COLUMN].str.replace(',', '').astype(float)
    cpi_df[CPI_COLUMN] = cpi_df[CPI_COLUMN].astype(float)

    # Rename columns to generic names
    stocks_df = stocks_df.rename(columns={STOCKS_COLUMN: 'Stocks'})
    bonds_df = bonds_df.rename(columns={BONDS_COLUMN: 'Bonds'})
    cpi_df = cpi_df.rename(columns={CPI_COLUMN: 'CPI'})

    # Merge data
    data = pd.merge(stocks_df[['Fecha', 'Stocks']], bonds_df[['Fecha', 'Bonds']], on='Fecha', how='inner')
    data = pd.merge(data, cpi_df[['Fecha', 'CPI']], on='Fecha', how='inner')
    data = data.sort_values('Fecha').reset_index(drop=True)

    data['Stocks_Return'] = data['Stocks'].pct_change()
    data['Bonds_Return'] = data['Bonds'].pct_change()
    data['CPI_Return'] = data['CPI'].pct_change()

    data['Stocks_Real_Return'] = (1 + data['Stocks_Return']) / (1 + data['CPI_Return']) - 1
    data['Bonds_Real_Return'] = (1 + data['Bonds_Return']) / (1 + data['CPI_Return']) - 1

    return data

# Load data
data = load_data()

# Parse target dates from config
data_end = pd.to_datetime(DATA_END)
data_start = pd.to_datetime(DATA_START)

# Validate that target dates exist in the data
data_date_range = (data['Fecha'].min(), data['Fecha'].max())
if data_start < data_date_range[0] or data_start > data_date_range[1]:
    raise ValueError(f"DATA_START '{DATA_START}' is outside data range ({data_date_range[0].strftime('%Y-%m-%d')} to {data_date_range[1].strftime('%Y-%m-%d')})")

if data_end < data_date_range[0] or data_end > data_date_range[1]:
    raise ValueError(f"DATA_END '{DATA_END}' is outside data range ({data_date_range[0].strftime('%Y-%m-%d')} to {data_date_range[1].strftime('%Y-%m-%d')})")

if data_start >= data_end:
    raise ValueError(f"DATA_START ({DATA_START}) must be before DATA_END ({DATA_END})")

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
stocks_real = data['Stocks_Real_Return'].to_numpy()
bonds_real = data['Bonds_Real_Return'].to_numpy()


def run_portfolio_path(start_idx, target_stocks_pct, target_bonds_pct, monthly_withdrawal, max_months):
    stocks_value = INITIAL_PORTFOLIO * target_stocks_pct
    bonds_value = INITIAL_PORTFOLIO * target_bonds_pct

    path_dates = []
    path_stocks = []
    path_bonds = []
    path_total = []
    path_withdrawals = []
    path_min = []
    path_max = []

    current_min = INITIAL_PORTFOLIO
    current_max = INITIAL_PORTFOLIO

    for idx in range(start_idx, start_idx + max_months):
        stocks_value *= (1 + stocks_real[idx])
        bonds_value *= (1 + bonds_real[idx])

        current_total = stocks_value + bonds_value
        withdrawal_remaining = monthly_withdrawal

        current_stocks_pct = stocks_value / current_total if current_total > 0 else 0
        current_bonds_pct = bonds_value / current_total if current_total > 0 else 0

        if current_stocks_pct > target_stocks_pct and stocks_value > 0:
            excess_stocks = stocks_value - (current_total * target_stocks_pct)
            withdrawal_from_stocks = min(excess_stocks, withdrawal_remaining)
            stocks_value -= withdrawal_from_stocks
            withdrawal_remaining -= withdrawal_from_stocks

        if withdrawal_remaining > 0 and current_bonds_pct > target_bonds_pct and bonds_value > 0:
            excess_bonds = bonds_value - (current_total * target_bonds_pct)
            withdrawal_from_bonds = min(excess_bonds, withdrawal_remaining)
            bonds_value -= withdrawal_from_bonds
            withdrawal_remaining -= withdrawal_from_bonds

        if withdrawal_remaining > 0:
            total_after_partial = stocks_value + bonds_value
            if total_after_partial > 0:
                stocks_withdrawal = withdrawal_remaining * (stocks_value / total_after_partial)
                bonds_withdrawal = withdrawal_remaining * (bonds_value / total_after_partial)
                stocks_value -= min(stocks_value, stocks_withdrawal)
                bonds_value -= min(bonds_value, bonds_withdrawal)

        current_total = stocks_value + bonds_value
        if current_total > 0:
            stocks_value = current_total * target_stocks_pct
            bonds_value = current_total * target_bonds_pct

        portfolio_value = stocks_value + bonds_value
        withdrawn_amount = monthly_withdrawal - withdrawal_remaining

        current_min = min(current_min, portfolio_value)
        current_max = max(current_max, portfolio_value)

        path_dates.append(fecha_array[idx])
        path_stocks.append(stocks_value)
        path_bonds.append(bonds_value)
        path_total.append(max(0, portfolio_value))
        path_withdrawals.append(max(0.0, withdrawn_amount))
        path_min.append(current_min)
        path_max.append(current_max)

        if portfolio_value <= 0:
            break

    return {
        'dates': path_dates,
        'stocks': path_stocks,
        'bonds': path_bonds,
        'total': path_total,
        'withdrawals': path_withdrawals,
        'min_values': path_min,
        'max_values': path_max,
    }


def insert_path_rows(start_date, allocation, withdrawal_rate, path_data):
    if not SAVE_ALL_PATHS or WORKER_DB_CONN is None:
        return

    WORKER_DB_CONN.insert_path_rows(start_date, allocation, withdrawal_rate, path_data, CENTRAL_DB_LOCK)


def insert_simulation_results(result_records):
    if WORKER_DB_CONN is None or not result_records:
        return

    WORKER_DB_CONN.insert_simulation_results(result_records, CENTRAL_DB_LOCK)


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

    stocks_pct, bonds_pct = allocation
    target_stocks_pct = stocks_pct / 100
    target_bonds_pct = bonds_pct / 100
    monthly_withdrawal = (INITIAL_PORTFOLIO * withdrawal_rate / 100) / 12

    path_data = run_portfolio_path(start_idx, target_stocks_pct, target_bonds_pct, monthly_withdrawal, max_period_months)
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
    
    # Create central database using abstraction
    central_db_path, _ = create_central_database(output_dir, SAVE_ALL_PATHS)
    print(f"[OK] Created central SQLite database at {central_db_path}\n")
    
    # Create a shared lock for database access
    db_lock = Lock()

    print(f"Running {len(pool_tasks)} simulations in parallel using {max_workers} processes...\n")
    pool_kwargs = {
        'processes': max_workers,
        'initializer': init_worker_shared,
        'initargs': (central_db_path, 'sqlite', db_lock),
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
    
    print(f"\n[OK] Analysis complete. Results saved to {central_db_path}")

    # Create indexes on the central database
    create_indexes(central_db_path, SAVE_ALL_PATHS, 'sqlite')
    print("[OK] Indexes created.")
