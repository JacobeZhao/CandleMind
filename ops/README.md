# Operations

- `dev-compose.ps1` is the supported local Docker Compose startup command. It
  builds once, starts the services, and checks `/api/ping`.
- `verify.ps1` runs backend tests, Python compilation, frontend tests, and the
  frontend build with a disposable system-temporary data, runtime, and cache
  layout, leaving repository data untouched.
- `host/` contains platform-specific administration helpers. These are not
  required to build or run CandleMind and must be reviewed on the target host.

Application data and runtime state remain outside this directory. Configure
their host paths through a local `.env` based on `.env.example`.
