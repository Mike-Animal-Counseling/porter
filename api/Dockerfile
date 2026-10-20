FROM python:3.12-slim

WORKDIR /app

# REQ_FILE=requirements.txt (api) or requirements-embedder.txt (embedder service)
ARG REQ_FILE=requirements.txt
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
COPY api/requirements.txt ./requirements.txt
COPY api/${REQ_FILE} ./req.txt
COPY requirements-lock.txt ./constraints.txt
COPY requirements-embedder-lock.txt ./model-constraints.txt
RUN if [ "$REQ_FILE" = "requirements-embedder.txt" ]; then \
        pip install --no-cache-dir -c model-constraints.txt torch --index-url "$TORCH_INDEX_URL"; \
    fi \
    && pip install --no-cache-dir -c constraints.txt -c model-constraints.txt -r req.txt

COPY porter/ ./porter/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY VERSION ./VERSION

# Run as non-root (production hygiene: a compromised API process should not
# own the filesystem or be able to install packages).
RUN useradd --create-home --uid 1000 porter \
    && mkdir -p /home/porter/models /home/porter/data \
    && chown -R porter:porter /app /home/porter
USER porter

EXPOSE 8000 8001
