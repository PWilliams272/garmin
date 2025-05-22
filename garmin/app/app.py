# garmin/app/app.py

from flask import Flask, render_template, request
from garmin.analysis.analysis import analysis_bp
from garmin.app.routes import bp as weight_bp

def create_app():
    app = Flask(__name__)
    # Only register the weight_bp blueprint, not analysis_bp
    app.register_blueprint(weight_bp)

    @app.route('/')
    def index():
        return '<h1>Garmin Web App is running!</h1>'

    return app

def main():
    app = create_app()
    app.run(debug=True)

if __name__ == '__main__':
    main()
