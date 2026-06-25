"""WSGI entry point.

Use this if you run under a WSGI server (e.g. gunicorn) instead of `python app.py`.
It runs the one-time bootstrap (first scan + start the scheduler) and exposes `app`.

IMPORTANT: run with a SINGLE worker, otherwise the scheduler is duplicated and
scans/alerts fire multiple times, e.g.:

    gunicorn --workers 1 --chdir Backend --bind 127.0.0.1:5000 wsgi:app
"""
import os
import sys

# Ensure this Backend/ directory is importable regardless of the server's cwd
# (gunicorn/systemd may launch from elsewhere). Flat modules import as `config`,
# `scheduler`, `from agents import ...`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import _bootstrap, app  # noqa: E402

_bootstrap()

__all__ = ["app"]
