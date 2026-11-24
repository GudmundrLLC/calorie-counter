FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml ./
RUN uv pip install --system fastapi "uvicorn[standard]" jinja2 openai python-multipart authlib itsdangerous httpx

# App modules + templates + static (the DDUUI rewrite adds static/ and the
# gateway_* / llm modules; earlier images copied only app.py + auth.py).
COPY app.py auth.py gateway_auth.py gateway_access.py llm.py ./
COPY templates/ templates/
COPY static/ static/

RUN mkdir -p /app/data /app/uploads

ENV DEPLOY_MODE=railway
ENV PORT=8000

CMD python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2 --proxy-headers --forwarded-allow-ips="*"
