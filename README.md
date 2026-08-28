# Stateful Two-Phase Candidate Admission API

FastAPI service implementing `POST /quantize` with freeze/select phases.

## Run locally

```bash
docker build -t quantize-service .
docker run --rm -p 10000:10000 quantize-service
```

Or without Docker:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 10000
```

## Render

Create a **Web Service** from this GitHub repository and select **Docker** as the runtime. Render will use the `Dockerfile`. The application listens on Render's `PORT` environment variable.

The grader endpoint is:

`https://<your-render-service>.onrender.com/quantize`

## State

Freeze records are stored in SQLite for the lifetime of the running service. A new Render instance/deployment with an ephemeral filesystem starts with an empty state, which is normal for this implementation of the stateful API contract unless an external persistent database is added.
