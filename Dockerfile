FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY pyproject.toml .
COPY tenderfit ./tenderfit
RUN pip install --no-cache-dir .
COPY migrations ./migrations
COPY scripts ./scripts
RUN useradd --create-home --uid 1000 tenderfit && mkdir -p /data && chown tenderfit /data
ENV DOCUMENT_DIR=/data/documents HF_HOME=/data/hf EVAL_REPORT_DIR=/data/reports
USER tenderfit
