# OAFE

OAFE is a visual dataset discovery and curation app. Describe the data you
need, add seed files and filters, review candidate examples, and turn the
approved results into an exportable dataset.

The repository contains the Next.js 16 frontend plus the FastAPI, PostgreSQL,
worker, and object-storage services used by the dataset-request workflow.

## Prerequisites

- Docker Desktop, running before you start the stack
- Node.js 20.9 or newer and npm
- Free local ports: `3000`, `8000`, `5433`, and `10000`

The Docker database is exposed on host port `5433` so it can run alongside a
Homebrew PostgreSQL instance that already uses `5432`. Containers still reach
the database internally at `postgres:5432`.

## Local setup

Install the frontend dependencies and create the Docker environment file:

```bash
npm install
cp .env.example .env
```

The example is ready for both host processes and Docker. Keep these local
database values in `.env`:

```dotenv
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/datacurate
DOCKER_DATABASE_URL=postgresql://postgres:postgres@postgres:5432/datacurate
NEXT_PUBLIC_API_URL=http://localhost:8000
POSTGRES_PORT=5433

# Recommended for model-assisted planning and managed web search.
ORBITRAGE_API_KEY=

# Optional: authenticated Hugging Face requests receive higher rate limits.
HF_TOKEN=
```

Add real tokens locally without committing them. The remaining model defaults
in `.env.example` can be left unchanged.

The hostname and port difference is intentional. Next.js and
`npm run db:migrate` use `DATABASE_URL`; Compose maps `DOCKER_DATABASE_URL`
into `DATABASE_URL` inside every container. A separate `.env.local` is not
needed. To use a remote PostgreSQL or Supabase database, set both variables to
the same remote connection string. Alternatively, leave `DOCKER_DATABASE_URL`
empty and Compose falls back to `DATABASE_URL`.

Build and start the API and the three workers required by the current
dataset-request flow:

```bash
docker compose up -d --build api worker-agent worker-agent-build worker-export
```

Compose starts PostgreSQL and Azurite automatically. Before the API or any
worker starts, the one-shot `migrate` service applies every pending SQL
migration and must exit successfully. To inspect that gate or rerun it after
adding a migration, use `docker compose logs migrate` or
`docker compose run --rm migrate`.

Finally, start the frontend:

```bash
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The API is available at
[http://localhost:8000](http://localhost:8000).

The `worker-ingest` and `worker-recommend` services support the older catalog,
search, and moodboard flows. They are not required to create and run a request
from the current UI.

## Verify the backend

Check the API and its database schema:

```bash
curl http://localhost:8000/health
```

A fully configured response has `status: "ok"`, `database: true`,
`schema: true`, empty `missing_tables` and `missing_columns` arrays, and
`agent_configured: true`.

Without an Orbitrage key, health reports `status: "degraded"` and the agent
uses its deterministic keyword planner and Hugging Face search. Set
`ORBITRAGE_API_KEY` for model-assisted planning, judging, and managed web
search. `HF_TOKEN` is optional; anonymous Hugging Face requests work with
lower rate limits.

If the UI says **Unable to save the dataset request**, first confirm that
`.env` uses `localhost:5433` for `DATABASE_URL`, then restart `npm run dev`
after changing environment files. Next.js keeps its PostgreSQL pool for the
life of the process. Then inspect the migration gate and running services:

```bash
docker compose ps --all
docker compose logs --tail=100 migrate api worker-agent worker-agent-build worker-export
```

Seed uploads are capped at 24 files and 512 MiB combined. This leaves room for
normal MP4 and MOV reference clips while preventing a single request from
exhausting the Next.js process or PostgreSQL connection. Requests over either
limit receive HTTP 413 with a specific error message.

## Architecture

- `src/` — Next.js frontend, request persistence route, and agent API client
- `apps/api/` — FastAPI gateway and request, job, search, and export endpoints
- `apps/workers/` — interactive agent, dataset builder, export, ingest, and
  recommendation workers
- `packages/core/datacurate_core/` — shared database, queue, Hugging Face,
  object storage, agent-state, and dataset utilities
- `db/migrations/` — ordered PostgreSQL schema migrations
- `docker-compose.yml` — local PostgreSQL, Azurite, API, and worker services

Slow work is persisted as PostgreSQL jobs. The browser polls durable request
state and events, so refreshing a workspace does not discard agent progress.
Azurite provides the local Azure Blob-compatible store for previews and
exports.

## Commands

```bash
npm run dev        # Start the Next.js development server
npm run db:migrate # Apply pending PostgreSQL migrations
npm run lint       # Run ESLint
npm run typecheck  # Generate route types and check TypeScript
npm run build      # Create a production build
npm start          # Serve the production build
```

## Upstream integration

The backend platform was selectively integrated from
[`AbhyudayPatel/olaamigo`](https://github.com/AbhyudayPatel/olaamigo) at commit
`5bf1ce6`. The API, workers, shared core package, migrations, and deployment
documentation were brought into this repository while preserving OAFE's
current root frontend.
