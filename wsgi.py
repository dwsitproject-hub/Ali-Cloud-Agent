"""WSGI entry point.

Use this if you run under a WSGI server (e.g. gunicorn) instead of `python app.py`.
It runs the one-time bootstrap (first scan + start the scheduler) and exposes `app`.

IMPORTANT: run with a SINGLE worker, otherwise the scheduler is duplicated and
scans/alerts fire multiple times, e.g.:

    gunicorn --workers 1 --bind 127.0.0.1:5000 wsgi:app
"""
from app import _bootstrap, app

_bootstrap()

__all__ = ["app"]
