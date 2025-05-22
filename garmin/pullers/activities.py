## garmin/data_puller/activities.py
from datetime import datetime
import pandas as pd

class ActivityPuller:
    def __init__(self, session):
        self.session = session

    def pull_list(self, limit: int=50) -> list[dict]:
        url = f'/activitylist-service/activities/search/activities?limit={limit}'
        return self.session.get(url) or []

    def get_strength_workout(self, activity_id: str) -> pd.DataFrame:
        url = f'/activity-service/activity/{activity_id}/exerciseSets'
        res = self.session.get(url)
        sets = res.get('exerciseSets', [])
        if not sets:
            return pd.DataFrame()
        df = pd.DataFrame(sets)
        # ... same transformation as before ...
        return df