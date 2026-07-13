FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml ./
RUN uv sync --frozen --no-dev 2>/dev/null || uv pip install --system fastapi "uvicorn[standard]" jinja2 anthropic python-multipart

COPY app.py ./
COPY templates/ templates/

RUN mkdir -p /app/data /app/uploads

ENV DEPLOY_MODE=railway
ENV PORT=8000

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
