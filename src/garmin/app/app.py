# garmin/app/app.py

import os

from dotenv import load_dotenv
from flask import Flask

# Before importing routes: routes.DEFAULT_SOURCE and the local-only page gate
# are both resolved from the environment at import time, and the S3 sync below
# needs GARMIN_S3_BUCKET. Without this the dev server silently started with no
# bucket configured and skipped the sync entirely.
load_dotenv()

from garmin.app.routes import bp as garmin_bp  # noqa: E402,I001

def create_app():
    app = Flask(__name__)
    app.register_blueprint(garmin_bp)
    return app

def _sync_then_serve_local():
    """Refresh the local data copy from S3, then point the app at it.

    Local development wants both current data and fast reads, and those pull
    in opposite directions: S3 is the source of truth, but every read is a
    network round trip and the pages do a lot of reads. Syncing once at
    startup and then serving local files gets both.

    A failed sync is not fatal. Being offline, or lacking credentials, should
    leave the dev server running against whatever local data already exists
    rather than refusing to start -- but it says so and falls back to S3,
    because silently serving a stale copy that looks identical to fresh data
    is the failure worth avoiding.

    Returns:
        True if the app should read local files, False to stay on S3.
    """
    from garmin.io.local_sync import sync_from_s3

    if os.environ.get('GARMIN_SKIP_SYNC') == '1':
        print('Skipping S3 sync (GARMIN_SKIP_SYNC=1); reading S3 directly.')
        return False

    print('Syncing local data from S3...')
    try:
        totals = sync_from_s3()
    except Exception as exc:
        print(f'  Sync failed ({type(exc).__name__}: {exc}).')
        print('  Reading S3 directly instead -- local data may be incomplete.')
        return False

    print(f"  {totals['downloaded']} files updated "
          f"({totals['bytes'] / 1024 / 1024:.1f} MB), "
          f"{totals['skipped']} already current. Serving local.")
    return True


def main():
    """Local development server.

    Enables the local-only pages (see garmin.app.routes.LOCAL_ONLY_PAGES) so
    the documented local command works with no extra setup. The deployed
    viewer is served by gunicorn against create_app() and so never takes this
    path -- which is exactly why both the flag and the sync below live here
    rather than in create_app.
    """
    os.environ.setdefault('GARMIN_ENABLE_LOCAL_PAGES', '1')

    if _sync_then_serve_local():
        # Assigned after import rather than through GARMIN_VIEWER_SOURCE:
        # that variable's *presence* is what marks a deployment for the
        # local-only page gate, so setting it here would hide /modeling.
        from garmin.app import routes
        routes.DEFAULT_SOURCE = 'local'

    app = create_app()
    app.run(debug=True)

if __name__ == '__main__':
    main()
