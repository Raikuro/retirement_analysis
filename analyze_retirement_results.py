import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np
from matplotlib.patches import Rectangle

# CONFIGURATION
periods = [30, 40, 50, 60]
allocations_order = ['100/0', '75/25', '50/50', '25/75', '0/100']
allocations_labels = ['100% Stocks', '75% Stocks', '50% Stocks', '25% Stocks', '0% Stocks']
withdrawal_rates = [3.0, 3.25, 3.5, 3.75, 4.0, 4.25, 4.5, 4.75, 5.0]
targets_to_plot = [0.0, 0.5, 1.0]

# Load results
output_dir = os.path.join(os.path.dirname(__file__), 'output')
print("Loading results...")
results = pd.read_csv(os.path.join(output_dir, 'backtest_retirement_detailed.csv'))
print(f"\nTotal simulations: {len(results)}")

# Precompute row labels
row_labels = [f"{period}y" for alloc in allocations_order for period in periods]

def plot_success_matrix(matrix, target_pct, output_dir):
    fig, ax = plt.subplots(figsize=(14, 10), dpi=150)
    cmap = sns.color_palette("RdYlGn", as_cmap=True)
    sns.heatmap(matrix, annot=True, fmt='.1f', cmap=cmap, 
                cbar_kws={'label': 'Success Rate (%)'}, 
                ax=ax, vmin=0, vmax=100,
                xticklabels=[f"{r}%" for r in withdrawal_rates],
                yticklabels=row_labels,
                linewidths=0.5, linecolor='gray')
    
    ax.xaxis.tick_top()
    ax.xaxis.set_label_position('top')
    ax.set_xlabel('Withdrawal Rate', fontsize=12, fontweight='bold')
    ax.set_ylabel('')
    
    for i, alloc_label in enumerate(allocations_labels):
        y_start = i * 4
        rect = Rectangle((0, y_start), len(withdrawal_rates), 4, 
                        fill=False, edgecolor='black', linewidth=2.5)
        ax.add_patch(rect)
        ax.text(-0.1, y_start + 1.5, alloc_label, 
               rotation=90, va='center', ha='right', fontsize=11, fontweight='bold',
               transform=ax.get_yaxis_transform())
    
    ax.set_title(f'Success Matrix - Target {target_pct}% Final Value\n(Historical 1871-2016)', 
                 fontsize=14, fontweight='bold', pad=20)
    plt.subplots_adjust(left=0.06, right=0.95, top=0.9, bottom=0.1)
    
    filename = f'success_matrix_target_{target_pct}.png'
    filepath = os.path.join(output_dir, filename)
    plt.savefig(filepath, dpi=300, bbox_inches='tight')
    plt.close()

# Generate matrices and plots
for target in targets_to_plot:
    print(f"\nGenerating matrix for target {target}...")
    target_data = results[results['final_value_target'] == target]
    
    # Create matrix using groupby
    grouped = target_data.groupby(['allocation', 'retirement_period', 'withdrawal_rate'])['success'].mean() * 100
    matrix_data = []
    for alloc in allocations_order:
        for period in periods:
            row_values = [grouped.get((alloc, period, rate), 0) for rate in withdrawal_rates]
            matrix_data.append(row_values)
    
    matrix = np.array(matrix_data)
    target_pct = int(target * 100)
    plot_success_matrix(matrix, target_pct, output_dir)