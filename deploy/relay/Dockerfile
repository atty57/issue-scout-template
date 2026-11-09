# The Issue Scout email relay only: email_relay.py and the address check it shares with email_delivery.py.
# No engine, model or user code. Build from the repository root:
#   docker build -f deploy/relay/Dockerfile -t issue-scout-relay .
# Configuration comes from the environment (see .env.relay.example); keep RESEND_API_KEY and
# SCOUT_EMAIL_RELAY_SECRET in the host's secret store, never in this image.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-email-relay.txt .
RUN pip install --no-cache-dir -r requirements-email-relay.txt
COPY email_delivery.py email_relay.py ./
EXPOSE 8080
# Gunicorn writes no access log unless asked, so workflow tokens and message bodies stay out of logs.
# ponytail: runs as root so it can write the host-mounted /data volume; add a user and chown at start if your host allows.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "45", "email_relay:create_app()"]
