"""Shared SQLAlchemy instance.

Kept in its own module so models, the app factory, and CLI commands can all
import the same `db` without circular imports.
"""
from __future__ import annotations

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
