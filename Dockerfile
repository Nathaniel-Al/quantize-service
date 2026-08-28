FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 10000
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT:-10000} app.main:app"]
