FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Render sets $PORT itself; shell-form CMD (no brackets) lets it expand here.
CMD gunicorn app:server -b 0.0.0.0:$PORT
