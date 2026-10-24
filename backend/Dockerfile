# Image for any host that runs containers (Render, Railway, Fly.io, Cloud Run, a VM, ...).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
# Migrations are a separate, deliberate step (alembic upgrade head); they never run on startup.
# Set FORWARDED_ALLOW_IPS to your platform's proxy address so rate limiting sees real client IPs.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
