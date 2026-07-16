# Cloud Agent Monitoring - app image.
# Runs the Flask app under gunicorn (single worker => single APScheduler).
# Templates/static come from Frontend/; flat Python modules live in Backend/.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/Backend \
    FLASK_APP=app

WORKDIR /app

# Install deps first for better layer caching.
COPY Backend/requirements.txt Backend/requirements.txt
RUN pip install --no-cache-dir -r Backend/requirements.txt

# App code + templates.
COPY Backend/ Backend/
COPY Frontend/ Frontend/

EXPOSE 5000

# Default: serve via gunicorn. One worker => a single APScheduler; --threads
# gives the web side concurrency so a long scan can't block dashboard polling.
# docker-compose overrides `command` to run migrations + seed first.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--chdir", "Backend", "--bind", "0.0.0.0:5000", "wsgi:app"]
