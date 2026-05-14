FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY requirements.txt* ./

RUN if [ -f requirements.txt ]; then \
        pip install --no-cache-dir -r requirements.txt; \
    else \
        pip install --no-cache-dir \
            "fastapi==0.115.6" "uvicorn[standard]==0.32.1" \
            "sqlalchemy==2.0.36" "asyncpg==0.30.0" "alembic==1.14.0" \
            "pydantic==2.10.3" "pydantic-settings==2.7.0" \
            "structlog==24.4.0" "python-multipart==0.0.20"; \
    fi

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
