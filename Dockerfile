FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY pyproject.toml .
COPY tenderfit ./tenderfit
RUN pip install --no-cache-dir .
COPY migrations ./migrations
COPY scripts ./scripts
ENV DOCUMENT_DIR=/data/documents HF_HOME=/data/hf
