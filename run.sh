#!/usr/bin/env bash
# Start Cloud Agent Monitoring
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
pip install -r requirements.txt
exec python app.py
