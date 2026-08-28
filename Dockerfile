FROM python:3.11-slim

WORKDIR /app

# Copy and install requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application code
COPY . .

# Expose standard port (Render will override but it's good practice to expose)
EXPOSE 8000

# Run uvicorn on 0.0.0.0 and listen on the $PORT variable provided by Render (defaults to 8000 locally)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
