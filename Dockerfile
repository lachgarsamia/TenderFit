FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY tenderfit ./tenderfit
RUN pip install --no-cache-dir .
COPY migrations ./migrations
COPY scripts ./scripts
