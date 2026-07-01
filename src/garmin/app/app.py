# garmin/app/app.py

from flask import Flask, render_template, request
from garmin.app.routes import bp as garmin_bp

def create_app():
    app = Flask(__name__)
    app.register_blueprint(garmin_bp)
    return app

def main():
    app = create_app()
    app.run(debug=True)

if __name__ == '__main__':
    main()
