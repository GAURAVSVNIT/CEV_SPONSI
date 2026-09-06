# One container, one process: `python run.py serve` long-polls Telegram so the
# card buttons react instantly. State lives in ./data (mount it), secrets come
# from .env (docker-compose passes it through as env_file).
FROM python:3.12-slim

WORKDIR /app

# Dependencies first so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "run.py", "serve"]
