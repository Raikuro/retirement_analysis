import os
import sqlite3
import pandas as pd


def infer_db_type(db_type, db_path):
    if db_type:
        normalized = db_type.lower()
        if normalized == 'sqlite':
            return normalized
        raise ValueError(f"Unsupported DB_TYPE: {db_type}")

    extension = os.path.splitext(db_path)[1].lower()
    if extension in ('.sqlite', '.db', '.sqlite3'):
        return 'sqlite'
    return 'sqlite'


def find_results_database(output_dir, db_file, db_type):
    """
    Find and return path to results database.
    
    Priority:
    1. Central SQLite database (backtest_retirement.sqlite) - new Option 2
    2. Configured database file (DB_FILE from config)
    
    Returns: (db_path, db_type)
    """
    # Check for central SQLite database first (Option 2)
    central_db = os.path.join(output_dir, 'backtest_retirement.sqlite')
    if os.path.exists(central_db):
        return central_db, 'sqlite'
    
    # Fall back to configured database
    db_path = os.path.join(output_dir, db_file)
    if os.path.exists(db_path):
        return db_path, db_type or infer_db_type(None, db_path)
    
    # No database found, return default
    return db_path, db_type or 'sqlite'


def create_central_database(output_dir, save_all_paths):
    """
    Create central results database for multi-process writing.
    
    Uses SQLite with WAL mode for better multi-process concurrency.
    
    Returns: (db_path, db_type)
    """
    central_db_path = os.path.join(output_dir, 'backtest_retirement.sqlite')
    
    # Remove existing database if present
    if os.path.exists(central_db_path):
        os.remove(central_db_path)
    
    # Create and initialize central database
    conn = DatabaseBackend.open(central_db_path, db_type='sqlite')
    conn.execute('PRAGMA journal_mode=WAL')  # Enable WAL for better concurrency
    conn.execute('PRAGMA synchronous=NORMAL')  # Faster writes with WAL
    conn.create_tables(save_all_paths)
    conn.close()
    
    return central_db_path, 'sqlite'


class DatabaseBackend:
    def __init__(self, path):
        self.path = path

    @classmethod
    def open(cls, path, db_type=None):
        kind = infer_db_type(db_type, path)
        return SQLiteBackend(path)

    def execute(self, sql, params=None):
        raise NotImplementedError

    def executemany(self, sql, params):
        raise NotImplementedError

    def fetchdf(self, sql, params=None):
        raise NotImplementedError

    def attach(self, alias, path):
        quoted_path = path.replace("'", "''")
        self.execute(f"ATTACH '{quoted_path}' AS {alias}")

    def detach(self, alias):
        self.execute(f"DETACH {alias}")

    def close(self):
        raise NotImplementedError

    def configure_worker(self):
        return

    def create_tables(self, save_all_paths):
        self.execute('''
            CREATE TABLE simulation_results (
                start_date DATE,
                end_date DATE,
                allocation VARCHAR,
                withdrawal_rate DOUBLE,
                retirement_period INTEGER,
                final_value_target DOUBLE,
                final_value DOUBLE,
                success BOOLEAN,
                months_lasted INTEGER,
                years_lasted DOUBLE,
                min_value DOUBLE,
                max_value DOUBLE,
                total_withdrawn DOUBLE
            )
        ''')
        if save_all_paths:
            self.execute('''
                CREATE TABLE simulation_paths (
                    start_date DATE,
                    allocation VARCHAR,
                    withdrawal_rate DOUBLE,
                    month INTEGER,
                    date DATE,
                    stocks DOUBLE,
                    bonds DOUBLE,
                    total DOUBLE,
                    withdrawal DOUBLE,
                    min_value DOUBLE,
                    max_value DOUBLE
                )
            ''')

    def create_indexes(self, save_all_paths):
        self.execute('CREATE INDEX IF NOT EXISTS idx_results_allocation ON simulation_results(allocation)')
        self.execute('CREATE INDEX IF NOT EXISTS idx_results_withdrawal_rate ON simulation_results(withdrawal_rate)')
        self.execute('CREATE INDEX IF NOT EXISTS idx_results_final_value_target ON simulation_results(final_value_target)')
        self.execute('CREATE INDEX IF NOT EXISTS idx_results_retirement_period ON simulation_results(retirement_period)')
        if save_all_paths:
            self.execute('CREATE INDEX IF NOT EXISTS idx_paths_allocation ON simulation_paths(allocation)')
            self.execute('CREATE INDEX IF NOT EXISTS idx_paths_withdrawal_rate ON simulation_paths(withdrawal_rate)')

    def insert_path_rows(self, start_date, allocation, withdrawal_rate, path_data, db_lock=None):
        """Insert simulation path data into the database."""
        allocation_str = f"{allocation[0]}/{allocation[1]}"
        start_date_str = start_date.strftime('%Y-%m-%d') if hasattr(start_date, 'strftime') else str(start_date)
        rows = []
        for month_index, date in enumerate(path_data['dates'], start=1):
            date_str = date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date)
            rows.append(
                (start_date_str,
                 allocation_str,
                 float(withdrawal_rate),
                 int(month_index),
                 date_str,
                 float(path_data['stocks'][month_index - 1]),
                 float(path_data['bonds'][month_index - 1]),
                 float(path_data['total'][month_index - 1]),
                 float(path_data['withdrawals'][month_index - 1]),
                 float(path_data['min_values'][month_index - 1]),
                 float(path_data['max_values'][month_index - 1]))
            )

        if rows:
            if db_lock is not None:
                with db_lock:
                    self.execute('BEGIN TRANSACTION')
                    self.executemany(
                        '''INSERT INTO simulation_paths
                           (start_date, allocation, withdrawal_rate, month, date, stocks, bonds, total, withdrawal, min_value, max_value)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        rows
                    )
                    self.execute('COMMIT')
            else:
                self.execute('BEGIN TRANSACTION')
                self.executemany(
                    '''INSERT INTO simulation_paths
                       (start_date, allocation, withdrawal_rate, month, date, stocks, bonds, total, withdrawal, min_value, max_value)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    rows
                )
                self.execute('COMMIT')

    def insert_simulation_results(self, result_records, db_lock=None):
        """Insert simulation results into the database."""
        if not result_records:
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

        if db_lock is not None:
            with db_lock:
                self.execute('BEGIN TRANSACTION')
                self.executemany(
                    '''INSERT INTO simulation_results
                       (start_date, end_date, allocation, withdrawal_rate, retirement_period,
                        final_value_target, final_value, success, months_lasted, years_lasted,
                        min_value, max_value, total_withdrawn)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    rows
                )
                self.execute('COMMIT')
        else:
            self.execute('BEGIN TRANSACTION')
            self.executemany(
                '''INSERT INTO simulation_results
                   (start_date, end_date, allocation, withdrawal_rate, retirement_period,
                    final_value_target, final_value, success, months_lasted, years_lasted,
                    min_value, max_value, total_withdrawn)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                rows
            )
            self.execute('COMMIT')


class SQLiteBackend(DatabaseBackend):
    def __init__(self, path):
        super().__init__(path)
        self.conn = sqlite3.connect(path)

    def execute(self, sql, params=None):
        if params is None:
            return self.conn.execute(sql)
        return self.conn.execute(sql, params)

    def executemany(self, sql, params):
        return self.conn.executemany(sql, params)

    def fetchdf(self, sql, params=None):
        return pd.read_sql_query(sql, self.conn, params=params)

    def close(self):
        self.conn.commit()
        self.conn.close()

    def configure_worker(self):
        return
