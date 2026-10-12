FROM python:3.12-slim

WORKDIR /app

# REQ_FILE=requirements.txt (api) or requirements-embedder.txt (embedder service)
ARG REQ_FILE=requirements.txt
COPY api/${REQ_FILE} ./req.txt
RUN pip install --no-cache-dir -r req.txt

COPY porter/ ./porter/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Run as non-root (production hygiene: a compromised API process should not
# own the filesystem or be able to install packages).
RUN useradd --create-home --uid 1000 porter \
    && mkdir -p /home/porter/models \
    && chown -R porter:porter /app /home/porter
USER porter

EXPOSE 8000 8001
