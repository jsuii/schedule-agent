#!/bin/sh
set -eu
python -m pytest --cov=scheduler --cov-report=term-missing -q
ruff check scheduler tests
ruff format --check scheduler tests
node --check scheduler/static/app.js
