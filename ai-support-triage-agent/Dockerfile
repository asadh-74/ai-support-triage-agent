# A container = your app + everything it needs, packaged so it runs the same everywhere
# (your laptop, GitHub Actions, Render, AWS, Azure, Google Cloud).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first: Docker caches this layer, so rebuilds are fast
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY kb ./kb

# Security: never run as root inside the container
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Render injects $PORT; default to 8000 locally
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
