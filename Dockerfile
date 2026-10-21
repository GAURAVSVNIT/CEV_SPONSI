# One image, two processes (docker-compose.yml runs them):
#   serve  long-polls Telegram, sends the outbox at the paced rate, runs the
#          daily job at DAILY_RUN_AT
#   web    the browser UI, and the public one-click unsubscribe link
# State lives in ./data (mount it); secrets come from .env (env_file).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata: SEND_HOURS and DAILY_RUN_AT are local times, so TZ=Asia/Kolkata in
# .env has to mean something inside the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# The process runs as an unprivileged user. The entrypoint starts as root
# only long enough to make the mounted ./data writable by that user, which
# is what usually breaks a bind mount on a fresh server.
RUN useradd --create-home --uid 10001 sponsy \
 && sed -i 's/\r$//' deploy/entrypoint.sh \
 && chmod +x deploy/entrypoint.sh

# serve touches data/heartbeat between polls. A daily run on a free-tier key
# can hold the loop for many minutes, so "stuck" means 30 minutes of silence.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD ["python", "run.py", "healthcheck", "--max-age", "1800"]

ENTRYPOINT ["deploy/entrypoint.sh"]
CMD ["python", "run.py", "serve"]
