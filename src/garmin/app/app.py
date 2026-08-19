# garmin/app/app.py

import os

from flask import Flask, render_template, request

from garmin.app.routes import bp as garmin_bp

def create_app():
    app = Flask(__name__)
    app.register_blueprint(garmin_bp)
    return app

def main():
    """Local development server.

    Enables the local-only pages (see garmin.app.routes.LOCAL_ONLY_PAGES) so
    the documented local command works with no extra setup. The deployed
    viewer is served by gunicorn against create_app() and so never takes this
    path -- which is exactly why the flag is set here and not in create_app.
    """
    os.environ.setdefault('GARMIN_ENABLE_LOCAL_PAGES', '1')
    app = create_app()
    app.run(debug=True)

if __name__ == '__main__':
    main()
