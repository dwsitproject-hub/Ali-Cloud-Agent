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

# Default: serve via gunicorn. docker-compose overrides `command` to run
# migrations + seed first (see docker-compose.yml).
CMD ["gunicorn", "--workers", "1", "--chdir", "Backend", "--bind", "0.0.0.0:5000", "wsgi:app"]
