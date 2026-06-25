#!/usr/bin/env bash
# Start Cloud Agent Monitoring
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
pip install -r Backend/requirements.txt
exec python Backend/app.py
