FROM python:3.11-slim

# Keep Python output unbuffered and avoid .pyc clutter.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./

# Copy application source and migrations.
COPY app ./app

# Install the CPU-only torch wheel first so `pip install .` doesn't pull the
# multi-GB CUDA build for the RemoteCLIP embedding stack.
RUN pip install --upgrade pip \
 && pip install torch==2.2.2 torchvision==0.17.2 --index-url https://download.pytorch.org/whl/cpu \
 && pip install .

COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

EXPOSE 8000

# Runs migrations, then starts the API server.
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
