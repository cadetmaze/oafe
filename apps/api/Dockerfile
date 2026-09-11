# Build context is the repo root (see docker-compose.yml) so this Dockerfile
# can COPY the shared `core` package as well as the api app itself.
FROM python:3.11-slim

# ffmpeg is required by mediacache.faststart_mp4: cached videos are remuxed
# so their moov atom sits at the front, otherwise browsers can't report a
# duration or start playback until the entire file downloads.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg     && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY packages/core /srv/packages/core
RUN pip install --no-cache-dir -e /srv/packages/core

COPY apps/api/requirements.txt /srv/apps/api/requirements.txt
RUN pip install --no-cache-dir -r /srv/apps/api/requirements.txt

COPY apps/api /srv/apps/api

WORKDIR /srv/apps/api
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
