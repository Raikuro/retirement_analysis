import os
import pandas as pd
import numpy as np
from dateutil.relativedelta import relativedelta
import time
from multiprocessing import Pool, cpu_count

# Configuration parameters
INITIAL_PORTFOLIO = 300_000  # € (scale only - results are percentage-based)
RETIREMENT_PERIODS = [30, 40, 50, 60]  # years - now calculated from the 60-year simulation
ALLOCATIONS = [
    (100, 0),     # 100% SP500 / 0% Bonds
    (75, 25),     # 75% SP500 / 25% Bonds
    (50, 50),     # 50% SP500 / 50% Bonds
    (25, 75),     # 25% SP500 / 75% Bonds
    (0, 100),     # 0% SP500 / 100% Bonds
]
WITHDRAWAL_RATES = [3.0, 3.25, 3.5, 3.75, 4.0, 4.25, 4.5, 4.75, 5.0]  # %
FINAL_VALUE_TARGETS = [0.0, 0.25, 0.5, 0.75, 1.0]  # % of the initial portfolio

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


def worker_simulation(task):
    start_date, allocation, withdrawal_rate = task
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

    sp500_value = INITIAL_PORTFOLIO * target_sp500_pct
    bonds_value = INITIAL_PORTFOLIO * target_bonds_pct

    portfolio_values = [INITIAL_PORTFOLIO]
    withdrawals = []
    min_values = [INITIAL_PORTFOLIO]
    max_values = [INITIAL_PORTFOLIO]

    for idx in range(start_idx, start_idx + max_period_months):
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

        portfolio_values.append(max(0, portfolio_value))
        withdrawals.append(max(0.0, withdrawn_amount))
        min_values.append(min(min_values[-1], portfolio_value))
        max_values.append(max(max_values[-1], portfolio_value))

        if portfolio_value <= 0:
            break

    result_records = []
    months_lasted = len(withdrawals)
    for retirement_period in valid_periods:
        months_needed = retirement_period * 12
        survived = months_lasted >= months_needed

        if survived:
            final_value = portfolio_values[months_needed]
            min_value = min_values[months_needed]
            max_value = max_values[months_needed]
            total_withdrawn = sum(withdrawals[:months_needed])
            end_date = start_date + relativedelta(months=months_needed)
        else:
            final_value = portfolio_values[-1]
            min_value = min_values[-1]
            max_value = max_values[-1]
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

    return result_records

if __name__ == '__main__':
    print(f"Fechas de inicio posibles (mínimo {min_period} años): {len(start_dates)}")
    print(f"Desde: {start_dates[0].strftime('%m/%Y')} hasta {start_dates[-1].strftime('%m/%Y')}\n")

    # Prepare tasks and run the pool
    all_results = []
    pool_tasks = [(sd, alloc, wr) for sd in start_dates for alloc in ALLOCATIONS for wr in WITHDRAWAL_RATES]
    processed = 0
    start_time = time.time()
    max_workers = max(1, cpu_count() - 1)

    print(f"Ejecutando {len(pool_tasks)} simulaciones en paralelo usando {max_workers} procesos...\n")

    with Pool(max_workers) as pool:
        for result_batch in pool.imap_unordered(worker_simulation, pool_tasks, chunksize=16):
            if result_batch:
                all_results.extend(result_batch)

            processed += 1
            if processed % 1000 == 0:
                elapsed = time.time() - start_time
                rate = processed / elapsed
                remaining = (len(pool_tasks) - processed) / rate
                status = f"Procesado: {processed}/{len(pool_tasks)} - Tiempo restante: {remaining/60:.1f} min"
                print(status.ljust(80), end='\r', flush=True)

    status = f"Procesado: {processed}/{len(pool_tasks)} - Finalizado"
    print(status.ljust(80))
    print("\n✓ Analysis complete, saving results...")
    # Convert to DataFrame and save results
    results_df = pd.DataFrame(all_results)
    output_dir = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(output_dir, exist_ok=True)
    results_df.to_csv(os.path.join(output_dir, 'backtest_retirement_detailed.csv'), index=False)
    
