"""
Lambda handler for triggering Garmin data update from AWS Lambda.
"""
from garmin.updaters import DataUpdater
from garmin.api import GarminSession
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager

def lambda_handler(event, context):
    session = GarminSession()
    file_manager = FileManager()
    curated_store = CuratedDataStore(file_manager=file_manager)
    updater = DataUpdater(session=session, curated_store=curated_store)
    updater.update_all()
    return {"status": "success"}
