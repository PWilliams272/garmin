"""
Lambda handler for triggering Garmin data update from AWS Lambda.
"""
from garmin.updaters import DataUpdater
from garmin.db.db_manager import DatabaseManager
from garmin.api import GarminSession

def lambda_handler(event, context):
    db_manager = DatabaseManager()
    session = GarminSession()
    updater = DataUpdater(session=session, db_manager=db_manager)
    updater.update_all()
    return {"status": "success"}
