FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global --add safe.directory '*'

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ingest ingest
COPY agent agent
COPY ui ui
COPY evals evals
COPY scripts scripts

ENV FALKORDB_HOST=falkordb FALKORDB_PORT=6379
EXPOSE 8000
CMD ["uvicorn", "agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
