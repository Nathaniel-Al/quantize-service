FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Single worker: freeze-record persistence relies on a shared SQLite file
# plus an in-process lock for check-then-insert atomicity. Multiple
# worker *processes* would each have their own lock, reintroducing a
# race on simultaneous first-time freezes of the same freezeId. Threads
# within the one worker are fine (the lock covers them).
ENV WEB_CONCURRENCY=1

EXPOSE 8000

CMD ["sh", "-c", "gunicorn -w 1 --threads 4 -b 0.0.0.0:${PORT:-8000} app:app"]
