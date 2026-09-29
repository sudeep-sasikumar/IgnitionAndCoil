# Ignition & Coil scanner - Linux image for a VPS (see README "Running on a VPS" and docker-compose.yaml).
# The dashboard login is enforced automatically: docker-compose.yaml binds the dashboard to 0.0.0.0,
# and the app refuses to serve beyond localhost without DASHBOARD_PASSWORD.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    IC_HOME=/app \
    TZ=UTC

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 1000 scanner && mkdir -p /app/var && chown -R scanner /app
USER scanner

EXPOSE 8000
# deploy/docker_start.py keeps the live config.yaml in the data volume (Settings page survives redeploys)
CMD ["python", "deploy/docker_start.py"]
