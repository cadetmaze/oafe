# Build context is the repo root (see docker-compose.yml).
# ffmpeg is required for real video keyframe/preview + audio waveform
# generation (thumbnails.py gracefully degrades without it, but production
# workers should always have it).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY packages/core /srv/packages/core
RUN pip install --no-cache-dir -e /srv/packages/core

COPY apps/workers/requirements.txt /srv/apps/workers/requirements.txt
RUN pip install --no-cache-dir -r /srv/apps/workers/requirements.txt

COPY apps/workers /srv/apps/workers

WORKDIR /srv/apps/workers

# Role-specific no-argument entrypoints (avoids Azure Container Apps CLI's
# `--command`/`--args` list parameters, which mis-parse any dash-prefixed
# token like `--poll` as the start of a new flag rather than a value) —
# each Container App just points `--command` at ONE of these, no args needed.
RUN printf '#!/bin/sh\nexec python -m ingest_worker --poll\n' > /usr/local/bin/run-ingest.sh && \
    printf '#!/bin/sh\nexec python -m recommend_worker\n' > /usr/local/bin/run-recommend.sh && \
    printf '#!/bin/sh\nexec python -m export_worker\n' > /usr/local/bin/run-export.sh && \
    printf '#!/bin/sh\nexec python -m agent_worker\n' > /usr/local/bin/run-agent.sh && \
    chmod +x /usr/local/bin/run-ingest.sh /usr/local/bin/run-recommend.sh /usr/local/bin/run-export.sh /usr/local/bin/run-agent.sh

CMD ["python", "-m", "recommend_worker"]
