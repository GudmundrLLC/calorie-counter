FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml ./
# numpy + onnxruntime + tokenizers: in-app IBM Granite (granite_inproc.py ·
# tier 1 of llm_client). Weights download at runtime to the Railway volume
# ($RAILWAY_VOLUME_MOUNT_PATH/models), never into the image.
RUN uv pip install --system fastapi "uvicorn[standard]" jinja2 openai python-multipart authlib itsdangerous httpx \
    numpy "onnxruntime>=1.20" "tokenizers>=0.20"

COPY app.py auth.py gateway_auth.py gateway_access.py llm_client.py granite_inproc.py voice_xai.py ./
COPY templates/ templates/
COPY static/ static/

RUN mkdir -p /app/data /app/uploads

ENV DEPLOY_MODE=railway
ENV PORT=8000

# One worker: in-app Granite keeps the model in process memory and runs one
# generation at a time; two workers would load (and download) it twice. AI
# calls run in the threadpool, so the event loop stays responsive.
CMD python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips="*"
