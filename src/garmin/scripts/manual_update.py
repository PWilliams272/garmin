from dotenv import load_dotenv
load_dotenv()

from garmin.updaters import DataUpdater
from garmin.api import GarminSession
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager

def main():
    session = GarminSession()
    file_manager = FileManager()
    curated_store = CuratedDataStore(file_manager=file_manager)
    updater = DataUpdater(session=session, curated_store=curated_store)
    updater.update_all()

if __name__ == "__main__":
    main()