# CandleMind

CandleMind is a FastAPI and React trading research application. The repository
contains reproducible source, tests, configuration templates, and maintenance
tools. Market data, trained models, runtime databases, reports, and logs live
under the external G-drive data root described in
[`docs/DATA_LAYOUT.md`](docs/DATA_LAYOUT.md).

## Prerequisites

- Python 3.12
- Node.js 20 and npm
- Docker Desktop with Compose, for the container workflow
- Access to `G:\CandleMind\CandleMind_data`, or equivalent paths configured in
  a local `.env`

## Start With Docker

```powershell
Copy-Item .env.example .env
powershell -ExecutionPolicy Bypass -File ops/dev-compose.ps1
```

The application is available at <http://localhost:3000>; the API is at
<http://localhost:8000>, and its readiness endpoint is `/api/ping`. The script
builds both images, starts Compose, and waits for the API to respond.

## Run From Source

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements-dev.txt
python -m uvicorn backend.app.main:app --reload --env-file .env --port 8000
```

In another terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Vite serves the frontend on <http://localhost:5173> and proxies API and
WebSocket traffic to the backend.

## Verify Changes

```powershell
powershell -ExecutionPolicy Bypass -File ops/verify.ps1
```

The verifier uses an ignored `.tmp/verify` data layout, so tests never depend
on or modify the real G-drive store. Pass `-InstallFrontend` to force a clean
`npm ci` before the frontend build.

## Repository Map

- `backend/app/`: API routes, trading services, and reusable RL implementation.
- `backend/scripts/`: data, training, evaluation, and artifact maintenance CLIs.
- `backend/tests/`: pytest coverage for API, execution, data, model, and RL rules.
- `frontend/src/`: React pages, components, context, hooks, and API client.
- `docs/`: canonical operations guidance and archived research decisions.
- `ops/`: local development and host-specific administration scripts.

Read [`AGENTS.md`](AGENTS.md) before automated changes and
[`backend/scripts/README.md`](backend/scripts/README.md) before running any
training or artifact-writing command. No current RL candidate is approved for
production, and promoted supervised releases are immutable.
