FROM python:3.12.10-slim
WORKDIR /app
COPY requirements.lock requirements.txt pytest.ini ./
RUN pip install --no-cache-dir -r requirements.lock
ENV TIKTOKEN_CACHE_DIR=/app/tokenizer-cache
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
COPY backend backend
COPY scripts scripts
COPY data data
COPY configs configs
COPY migrations migrations
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
