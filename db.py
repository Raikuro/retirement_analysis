import os
import sqlite3
import pandas as pd

try:
    import duckdb
except ImportError:
    duckdb = None


def infer_db_type(db_type, db_path):
    if db_type:
        normalized = db_type.lower()
        if normalized in ('duckdb', 'sqlite'):
            return normalized
        raise ValueError(f"Unsupported DB_TYPE: {db_type}")

    extension = os.path.splitext(db_path)[1].lower()
    if extension == '.duckdb':
        return 'duckdb'
    if extension in ('.sqlite', '.db', '.sqlite3'):
        return 'sqlite'
    return 'duckdb'


def database_extension(db_file):
    extension = os.path.splitext(db_file)[1]
    return extension if extension else '.db'


class DatabaseBackend:
    def __init__(self, path):
        self.path = path

    @classmethod
    def open(cls, path, db_type=None):
        kind = infer_db_type(db_type, path)
        if kind == 'duckdb':
            if duckdb is None:
                raise ImportError('duckdb is not installed')
            return DuckDBBackend(path)
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
                    sp500 DOUBLE,
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


class DuckDBBackend(DatabaseBackend):
    def __init__(self, path):
        super().__init__(path)
        self.conn = duckdb.connect(database=path)

    def execute(self, sql, params=None):
        return self.conn.execute(sql, params or [])

    def executemany(self, sql, params):
        return self.conn.executemany(sql, params)

    def fetchdf(self, sql, params=None):
        return self.conn.execute(sql, params or []).fetchdf()

    def close(self):
        self.conn.close()

    def configure_worker(self):
        try:
            self.execute('PRAGMA threads=1')
        except Exception:
            pass


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
