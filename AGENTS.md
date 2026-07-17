# Repository Guidelines

## Project Structure & Module Organization

CandleMind is split into a FastAPI backend and a Vite/React frontend. Backend application code lives in `backend/app/`: `routes/` contains API routers, `services/` contains trading, backtesting, ML, and reporting logic, and shared runtime pieces such as `database.py`, `state.py`, and `ws_manager.py` sit at the app root. Backend commands are grouped by responsibility under `backend/scripts/{data,training,evaluation,artifacts}/`; the supervised retraining entry point is `backend.scripts.training.retrain_multi_horizon`.

Frontend code lives in `frontend/src/`: `pages/` contains route-level screens, `components/` reusable UI, `context/` app state, `hooks/` React hooks, and `api/client.js` HTTP access. Generated data, models, reports, and runtime state live under `G:/CandleMind/CandleMind_data`; do not recreate a repository `data/` tree. Docker assets are in each service directory plus the root `docker-compose.yml`.

## Build, Test, and Development Commands

- `python -m venv .venv` then `pip install -r backend/requirements-dev.txt`: create a backend environment with test dependencies.
- `uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000`: run the API locally.
- `cd frontend && npm install`: install frontend dependencies.
- `cd frontend && npm run dev`: start the Vite dev server.
- `cd frontend && npm run build`: produce a production frontend build.
- `docker compose up --build`: build and run backend on `8000` and frontend on `3000`.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, snake_case functions/modules, and PascalCase classes. Keep FastAPI route handlers thin and place reusable domain logic in `backend/app/services/`. Use React function components, PascalCase component files, camelCase variables, and colocate page-specific UI in `frontend/src/pages/`. Prefer existing Tailwind utility patterns and lucide-react icons already used by the frontend.

## Testing Guidelines

Backend tests live under `backend/tests/` and use `pytest`; run them with `python -m pytest backend/tests -q`. Name new tests `test_*.py` and use FastAPI `TestClient` for route coverage. The frontend does not yet have a test runner, so verify `cd frontend && npm run build` for every frontend change. Add colocated `*.test.jsx` files when introducing a runner. Before submitting, run the backend tests, compile changed Python modules, and build the frontend.

## Commit & Pull Request Guidelines

Git history uses Conventional Commit prefixes such as `feat:`, `fix:`, and `chore:`. Keep subjects short and specific, for example `fix: strategy signal threshold handling`. Pull requests should include a concise summary, affected backend/frontend areas, setup or migration notes, linked issues if applicable, and screenshots for UI changes.

## Security & Configuration Tips

Do not commit real Binance keys, database files, model artifacts, or secrets. Use `.env.example` as the configuration template and keep local secrets in ignored environment files. Treat live trading changes as high risk: document testnet validation and default to non-destructive behavior when touching orders or strategy execution.
