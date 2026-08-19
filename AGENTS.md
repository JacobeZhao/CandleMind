# Repository Guidelines

## Project Structure & Module Organization

CandleMind has a FastAPI backend in `backend/app/` and a Vite/React frontend
in `frontend/src/`. API routers live in `backend/app/routes/`; reusable
market-data, AI, backtest, and exchange-execution logic lives in
`backend/app/services/`; SAR+ADX strategy code lives in
`backend/app/strategies/`. Supported commands are limited to
`backend/scripts/data/` and `backend/scripts/evaluation/`.

Frontend route screens are in `frontend/src/pages/`, shared UI in
`components/`, state in `context/`, and HTTP calls in `api/client.js`.
Generated data, reports, runtime state, and historical artifacts belong under
`G:/CandleMind/CandleMind_data`, never in a repository `data/` tree.
Small, synthetic, immutable contract fixtures are the sole exception: keep them
under `backend/tests/fixtures/` with checksums, provenance, and generation
details. Never place production or downloaded market data there.

## Build, Test, and Development Commands

- `pip install -r backend/requirements-dev.txt`: install backend and test dependencies.
- `python -m uvicorn backend.app.main:app --reload --port 8000`: run the API.
- `cd frontend && npm ci && npm run dev`: install and start Vite.
- `cd frontend && npm run build`: verify the production frontend bundle.
- `python -m pytest backend/tests -q`: run backend tests.
- `powershell -File ops/verify.ps1`: run the complete isolated verification gate.

## Coding Style & Testing

Use 4-space Python indentation, snake_case functions/modules, PascalCase
classes, and thin FastAPI handlers. Use React function components, PascalCase
component files, camelCase variables, existing Tailwind patterns, and
`lucide-react` icons. Name tests `test_*.py`; use mocks for Binance and
network calls. Every backend change must pass pytest and compilation; every
frontend change must pass `npm test` and `npm run build`.

## Commits, Security, And Trading Safety

Use Conventional Commit prefixes such as `feat:`, `fix:`, and `chore:`.
Pull requests should state affected areas, verification commands, migration
notes, and include screenshots for UI changes. Never commit secrets, databases,
market data, or generated artifacts. Treat order code as high risk: keep testnet
as the default, validate account mode, quantities, and idempotency, and require
documented testnet validation plus explicit authorization before mainnet writes. Documentation changes must
keep the root Chinese README, `docs/` index, and linked operating contracts in
sync; the project is distributed under the root MIT `LICENSE`.
