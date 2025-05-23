from dotenv import load_dotenv
load_dotenv()

from garmin.updaters import DataUpdater
from garmin.db.db_manager import DatabaseManager
from garmin.api import GarminSession

def main():
    db_manager = DatabaseManager()
    session = GarminSession()
    updater = DataUpdater(session=session, db_manager=db_manager)
    updater.update_all()

if __name__ == "__main__":
    main()