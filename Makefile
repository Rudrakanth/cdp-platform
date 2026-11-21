.PHONY: install demo serve test bench
install: ; pip install -r requirements.txt
demo:    ; python -m cdp.cli --data-dir ./data demo --profiles 20000
serve:   ; CDP_DATA_DIR=./data python -m uvicorn --factory cdp.api:create_app --port 8000
test:    ; python -m pytest -q
bench:   ; python -m cdp.cli --data-dir ./data bench --runs 5
