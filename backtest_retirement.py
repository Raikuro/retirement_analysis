import csv
import heapq
import os
import shutil
import pandas as pd
import numpy as np
from dateutil.relativedelta import relativedelta
import time
from multiprocessing import Pool, cpu_count
import json

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
PATH_OUTPUT_FILE = config['PATH_OUTPUT_FILE']
TEMP_DIR = os.path.join(os.path.dirname(__file__), config['TEMP_DIR'])
WORKER_PATH_FILE = None


def init_worker(temp_dir):
    global WORKER_PATH_FILE
    WORKER_PATH_FILE = os.path.join(temp_dir, f'paths_{os.getpid()}.csv')
    open(WORKER_PATH_FILE, 'a', newline='', encoding='utf-8').close()


def merge_path_temp_files(temp_dir, output_file, output_fields):
    temp_files = sorted(
        [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith('.csv')]
    )
    iterators = []

    def row_generator(file_path):
        with open(file_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    yield (int(row['task_index']), row)
                except (KeyError, TypeError, ValueError):
                    continue

    merged = heapq.merge(*(row_generator(path) for path in temp_files))

    with open(output_file, 'w', newline='', encoding='utf-8') as out_f:
        writer = csv.DictWriter(out_f, fieldnames=output_fields)
        writer.writeheader()
        for _, row in merged:
            row.pop('task_index', None)
            writer.writerow(row)


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


def write_path_rows(path_file, task_index, start_date, allocation, withdrawal_rate, path_data):
    allocation_str = f"{allocation[0]}/{allocation[1]}"
    fieldnames = [
        'task_index', 'start_date', 'allocation', 'withdrawal_rate', 'month', 'date',
        'sp500', 'bonds', 'total', 'withdrawal', 'min_value', 'max_value'
    ]
    file_is_empty = not os.path.exists(path_file) or os.path.getsize(path_file) == 0
    with open(path_file, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if file_is_empty:
            writer.writeheader()
        for month_index, date in enumerate(path_data['dates'], start=1):
            date_str = pd.Timestamp(date).strftime('%Y-%m-%d')
            writer.writerow({
                'task_index': task_index,
                'start_date': pd.Timestamp(start_date).strftime('%Y-%m-%d'),
                'allocation': allocation_str,
                'withdrawal_rate': withdrawal_rate,
                'month': month_index,
                'date': date_str,
                'sp500': path_data['sp500'][month_index - 1],
                'bonds': path_data['bonds'][month_index - 1],
                'total': path_data['total'][month_index - 1],
                'withdrawal': path_data['withdrawals'][month_index - 1],
                'min_value': path_data['min_values'][month_index - 1],
                'max_value': path_data['max_values'][month_index - 1],
            })


def worker_simulation(task):
    task_index, start_date, allocation, withdrawal_rate = task
    start_idx = np.searchsorted(fecha_array, start_date)
    if start_idx >= len(fecha_array) or fecha_array[start_idx] != start_date:
        return []

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

    if SAVE_ALL_PATHS and WORKER_PATH_FILE is not None:
        write_path_rows(WORKER_PATH_FILE, task_index, start_date, allocation, withdrawal_rate, path_data)

    return result_records


if __name__ == '__main__':
    print(f"Possible start dates (minimum {min_period} years): {len(start_dates)}")
    print(f"From: {start_dates[0].strftime('%m/%Y')} to {start_dates[-1].strftime('%m/%Y')}\n")

    # Prepare tasks and run the pool
    all_results = []
    pool_tasks = [(i, sd, alloc, wr) for i, (sd, alloc, wr) in enumerate((sd, alloc, wr) for sd in start_dates for alloc in ALLOCATIONS for wr in WITHDRAWAL_RATES)]
    processed = 0
    start_time = time.time()
    max_workers = max(1, cpu_count() - 1)

    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    path_output_file = os.path.join(output_dir, PATH_OUTPUT_FILE)

    if SAVE_ALL_PATHS:
        if os.path.exists(TEMP_DIR):
            for filename in os.listdir(TEMP_DIR):
                file_path = os.path.join(TEMP_DIR, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
        else:
            os.makedirs(TEMP_DIR, exist_ok=True)

        temp_dir = TEMP_DIR
        print(f"Saving full paths to temporary files in {temp_dir}...\n")
        pool_kwargs = {
            'processes': max_workers,
            'initializer': init_worker,
            'initargs': (temp_dir,),
        }
    else:
        print(f"Running {len(pool_tasks)} simulations in parallel using {max_workers} processes...\n")
        pool_kwargs = {
            'processes': max_workers,
        }

    with Pool(**pool_kwargs) as pool:
        for result_batch in pool.imap_unordered(worker_simulation, pool_tasks, chunksize=16):
            if result_batch:
                all_results.extend(result_batch)

            processed += 1
            if processed % 1000 == 0:
                elapsed = time.time() - start_time
                rate = processed / elapsed
                remaining = (len(pool_tasks) - processed) / rate
                status = f"Processed: {processed}/{len(pool_tasks)} - Remaining time: {remaining/60:.1f} min"
                print(status.ljust(80), end='\r', flush=True)

    status = f"Processed: {processed}/{len(pool_tasks)} - Completed"
    print(status.ljust(80))
    print("\n✓ Analysis complete, saving results...")
    if SAVE_ALL_PATHS:
        output_fields = [
            'start_date', 'allocation', 'withdrawal_rate', 'month', 'date',
            'sp500', 'bonds', 'total', 'withdrawal', 'min_value', 'max_value'
        ]
        merge_path_temp_files(temp_dir, path_output_file, output_fields)
        for filename in os.listdir(temp_dir):
            file_path = os.path.join(temp_dir, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        print(f"Merged path temp files into {path_output_file}\n")

    # Convert to DataFrame and save results
    results_df = pd.DataFrame(all_results)
    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    results_df.to_csv(os.path.join(output_dir, 'backtest_retirement_detailed.csv'), index=False)
