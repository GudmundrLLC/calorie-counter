FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml ./
RUN uv pip install --system fastapi "uvicorn[standard]" jinja2 openai python-multipart authlib itsdangerous httpx

COPY app.py auth.py ./
COPY templates/ templates/

RUN mkdir -p /app/data /app/uploads

ENV DEPLOY_MODE=railway
ENV PORT=8000

CMD python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2
