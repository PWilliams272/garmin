"""
Lambda handler for the analyzer/cache-build step: quality classification +
GP/STS trend fitting on curated data, then precomputing every page's
web-response JSON into curated/viewer_cache/. Runs against S3, on its own
schedule after garmin-data-updater's daily pull (see lambda_update.py) --
kept as a separate Lambda because its dependencies (scipy/scikit-learn/
statsmodels) don't fit a standard zip-packaged Lambda's 250MB unzipped
limit, so this one deploys as a container image instead (see
infra/docker/analyzer.Dockerfile).
"""
from garmin.analysis.analysis_pipeline import analyze_all
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.scripts.manual_build_viewer_cache import build_viewer_cache


def lambda_handler(event, context):
    file_manager = FileManager()
    curated_store = CuratedDataStore(file_manager=file_manager)
    analyze_all(curated_store)
    build_viewer_cache("s3")
    return {"status": "success"}
