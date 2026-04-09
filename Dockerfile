# Use Python 3.9 slim image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project into the retirement_analysis directory
RUN mkdir -p /app/retirement_analysis
COPY . /app/retirement_analysis

# Set working directory to retirement_analysis
WORKDIR /app/retirement_analysis

# Default command to run the analysis
CMD ["sh", "-c", "python backtest_retirement.py && python analyze_retirement_results.py"]