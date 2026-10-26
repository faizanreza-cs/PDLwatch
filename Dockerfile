FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_THREAD_LIMIT=1
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY pdlwatch ./pdlwatch
RUN uv sync --locked --no-editable --no-cache --extra llm --extra ocr
ENV PATH="/app/.venv/bin:$PATH"
# Generate the synthetic corpus and extract it at build time
RUN pdlwatch build
RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000
CMD ["uvicorn", "pdlwatch.api:app", "--host", "0.0.0.0", "--port", "8000"]
