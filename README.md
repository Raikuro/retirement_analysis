# 📊 Retirement Strategy Analysis

## What this project does
This project simulates retirement withdrawal strategies using historical inflation-adjusted data and generates comparative success analysis across different portfolio allocations, withdrawal rates, and retirement horizons.

- `backtest_retirement.py`: generates simulation data using monthly historical returns.
- `analyze_retirement_results.py`: creates success matrices and plots from the simulation results.

## Requirements
- Python 3.7+ (or newer)
- pandas
- numpy
- matplotlib
- seaborn
- python-dateutil

Install dependencies with:
```bash
pip install pandas numpy matplotlib seaborn python-dateutil
```

## Basic usage
1. Run the full backtest:
```bash
python backtest_retirement.py
```

2. Generate the charts:
```bash
python analyze_retirement_results.py
```

## Generated files
Results are saved in `retirement_analysis/output/`.

- `backtest_retirement_detailed.csv`: full simulation results for every scenario.
- `success_matrix_target_0.png`, `success_matrix_target_50.png`, `success_matrix_target_100.png`: success rate matrices.

## What each script does

### `backtest_retirement.py`
- Loads historical SP500, 10-year Treasury bond, and CPI data from `../input/`.
- Computes monthly real returns adjusted for inflation.
- Simulates monthly withdrawals with dynamic rebalancing to maintain target allocation.
- Tests multiple combinations: allocations (100/0 to 0/100 Stocks/Bonds), withdrawal rates (3% to 5%), and retirement horizons (30-60 years).
- Generates monthly start dates from 1871 through the latest date available for each horizon.
- Uses multiprocessing to speed up simulation.
- Saves detailed results in `output/backtest_retirement_detailed.csv`.

### `analyze_retirement_results.py`
- Reads the CSV produced by the backtest.
- Creates success matrices and saves PNG files in `output/`.

## Quick note
Recommended flow:
1. `backtest_retirement.py`
2. `analyze_retirement_results.py`

## Input data
The scripts use data from the `../input/` folder.

- `sp500_tr.csv`: historical S&P 500 total return data.
- `treasury_10y.csv`: historical 10-year Treasury bond data.
- `cpi.csv`: consumer price index data for inflation adjustment.
