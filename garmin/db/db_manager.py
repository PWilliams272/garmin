# garmin/db/database_manager.py

import os
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from garmin.db.models import Base

# --- Generalized Database Manager ---
class DatabaseManager:
    def __init__(self, db_uri=None, environment=None):
        """
        Initialize the database manager.
        
        Args:
            db_uri (str, optional): A full SQLAlchemy connection string.
                If not provided, it will be chosen based on the environment.
            environment (str, optional): 'aws' or 'local'. If not provided,
                the code will try to detect AWS Lambda via AWS_EXECUTION_ENV.
        """
        if environment is None:
            if 'AWS_EXECUTION_ENV' in os.environ:
                environment = 'aws'
            else:
                environment = 'local'
        if db_uri is None:
            if environment == 'aws':
                db_uri = os.environ.get('DATABASE_URL')
                if not db_uri:
                    raise ValueError("DATABASE_URL environment variable must be set in AWS.")
            else:
                # Construct an absolute path relative to the project root.
                base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
                db_path = os.path.join(base_dir, 'data', 'garmin.db')
                db_uri = f'sqlite:///{db_path}'
        self.engine = create_engine(db_uri)
        self.Session = sessionmaker(bind=self.engine)
        self._create_tables()
    
    def _create_tables(self):
        """Creates all tables based on the defined models, if they don't exist."""
        # If using PostgreSQL, ensure the 'garmin' schema exists
        if self.engine.url.get_backend_name() == 'postgresql':
            with self.engine.connect() as conn:
                conn.execute(text("CREATE SCHEMA IF NOT EXISTS garmin"))
        Base.metadata.create_all(self.engine)
    
    def add_record(self, record):
        """Add a single record (an instance of a model)."""
        session = self.Session()
        try:
            session.add(record)
            session.commit()
        except Exception as e:
            session.rollback()
            print("Error adding record:", e)
        finally:
            session.close()
    
    def add_records(self, records):
        """Adds a list of records (model instances) in a single batch."""
        session = self.Session()
        try:
            session.add_all(records)
            session.commit()
        except Exception as e:
            session.rollback()
            print("Error adding records:", e)
        finally:
            session.close()
    
    def update_record(self, model_class, unique_field, unique_value, data):
        """
        Updates a record for a given model class based on a unique field.
        If the record does not exist, it creates one.
        """
        session = self.Session()
        try:
            record = session.query(model_class).filter(
                getattr(model_class, unique_field) == unique_value
            ).first()
            if record is None:
                record = model_class(**data)
                session.add(record)
            else:
                for key, value in data.items():
                    setattr(record, key, value)
            session.commit()
        except Exception as e:
            session.rollback()
            print("Error updating record:", e)
        finally:
            session.close()
    
    def get_records(self, model_class):
        """Retrieves all records for the given model class."""
        session = self.Session()
        try:
            records = session.query(model_class).all()
            return records
        finally:
            session.close()

    def get_df(self, table_name):
        """
        Retrieves all records from the table corresponding to model_class as a Pandas DataFrame.
        
        Args:
            model_class: The SQLAlchemy model class whose table should be read.
        
        Returns:
            DataFrame: The table contents.
        """
        # Using read_sql with a simple SELECT query:
        df = pd.read_sql(f"SELECT * FROM {table_name}", con=self.engine)
        return df

    def drop_table(self, model_class):
        """
        Drops the table corresponding to the given SQLAlchemy model class.
        
        Example:
            db_manager.drop_table(HealthStat)
        """
        try:
            model_class.__table__.drop(self.engine)
            print(f"Table '{model_class.__tablename__}' dropped.")
        except Exception as e:
            print(f"Failed to drop table '{model_class.__tablename__}':", e)

# Helper for global singleton DB manager
_db_manager = None

def get_db_manager():
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager

def get_db_session():
    """Return a new SQLAlchemy session from the singleton DatabaseManager."""
    return get_db_manager().Session()