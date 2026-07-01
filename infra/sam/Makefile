build-GarminDataUpdaterFunction:
	mkdir -p "$(ARTIFACTS_DIR)/garmin"
	rsync -av --exclude 'app/' src/garmin/ "$(ARTIFACTS_DIR)/garmin/"
	pip install -r src/garmin/requirements-lambda.txt -t "$(ARTIFACTS_DIR)"