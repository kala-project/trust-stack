# Getting Started

## Requirements

- Linux x86_64
- CPython 3.14.4
- Node.js 22 or newer
- GNU Make, `uv`, `pdftotext`, and `bubblewrap`
- GitHub Copilot access for the logged-in user

The core Python application uses the standard library. Model-runtime dependencies are isolated and hash-pinned. Frontend dependencies are locked in `frontend/package-lock.json`.

## Setup

```sh
npm ci --prefix frontend
make build
make init
python3 scripts/prepare_copilot_runtime.py --install
python3 scripts/prepare_copilot_cli.py
make public-test-data
make init-owner
```

Authenticate the pinned Copilot CLI as the local user if needed. Automatic CLI downloads and updates are disabled after preparation.

Start the API and worker in separate terminals:

```sh
make serve
```

```sh
make worker
```

Open <http://127.0.0.1:8765/> and sign in with the owner password created by `make init-owner`.

For loopback-only development without authentication:

```sh
var/copilot-runtime/venv/bin/python -m workbench.server --no-auth
```

Never expose `--no-auth` beyond loopback.

## Run a Real Research Task

Submit this bundled analyst question in **New research**, review the proposed scope, and confirm execution:

> 以FY2025 Q4为时点，评估微软快速增长的AI资本投入所面临的风险与已可观察回报。结合FY2023—FY2025公开资料，区分现金资本开支与含融资租赁投入，分析Azure增长、云盈利、现金流与仍无法量化的客户级或项目级ROI；不要把收入增长直接当作AI投资的因果回报。

Two additional analyst scenarios are defined in `config/public-test-data.json`. A completed model run is not automatically a formal report: verification and publication are separate stages, and review status remains visible in the reader.

For source download behavior, validation, rights, and availability, see [Public Test Data](public-test-data.md).

## Development Checks

```sh
make check
```

This runs the reproducible public Python suite, frontend type checking, frontend tests, and a production frontend build. Tests that invoke a live model or external network are intentionally not part of the default check.

## Repository Layout

- `workbench/` — Python runtime, storage, verification, publication, and HTTP API
- `frontend/` — React source and frontend tests
- `web/` — reviewed static assets served by the Python application
- `config/` — versioned runtime policies and public data manifests
- `scripts/` — supported setup, data, audit, and maintenance tools
- `tests/` — deterministic Python and browser-contract tests
- `docs/` — stable public architecture and operating documentation

## Operating Boundaries

- Keep the default listener on loopback for local use.
- Do not disable SSRF or source-identity controls to force a download.
- Keep databases, downloaded documents, credentials, and report artifacts under ignored `var/` paths.
- Preserve published versions and issue corrections instead of rewriting them.
- Treat every generated report as requiring review appropriate to its use.

See [Security](../SECURITY.md) for security reporting and deployment assumptions. The machine-readable client API contract is at [`contracts/client-http-api.v1.openapi.json`](contracts/client-http-api.v1.openapi.json).

TrustStack is available under the [MIT License](../LICENSE). Third-party notices are under `third-party/`. Public test documents retain their publishers' rights and are not relicensed by this repository.
