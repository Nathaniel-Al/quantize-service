FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT:-10000} app.main:app"]
