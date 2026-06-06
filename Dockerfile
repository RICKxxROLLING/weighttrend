FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DB_PATH=/data/weighttrend.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

# SQLite lives here; mount this as a volume so data survives restarts.
VOLUME ["/data"]

EXPOSE 8080

# 1 worker keeps a single SQLite connection pattern simple; threads handle the UI.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8080", "app:app"]
