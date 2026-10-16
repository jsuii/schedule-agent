#!/bin/sh
set -eu
python -m pytest --cov=scheduler --cov-report=term-missing -q
ruff check scheduler tests scripts
ruff format --check scheduler tests scripts
node --check scheduler/static/app.js
