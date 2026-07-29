# healthcheck-engine

A continuous health-check engine: one long-running asyncio process that
polls a configurable set of targets (public HTTP endpoints, authenticated
APIs, cloud status feeds, Azure VNet/quota capacity, databases) on their own
intervals and emits structured JSON lines to stdout — one event per check
result, plus periodic heartbeats. Connectors are plugins selected by `type`
in a YAML config; each one implements a single `check()` coroutine that
never raises. The JSON-lines-to-stdout contract is the whole point: it's
designed to be scraped by an OTEL collector or forwarded straight into
Splunk, with no separate metrics/export pipeline to maintain.

## Local run

This project uses [`uv`](https://docs.astral.sh/uv/) for environment and
dependency management — no bare `pip`/`venv`/`python`.

```bash
uv sync
```

Run the engine against the sample config:

```bash
CHECKS_CONFIG=config/checks.yaml uv run python -m healthcheck_engine.main
```

It runs until killed (Ctrl-C), printing one JSON line per check result and
a periodic heartbeat line. Against the sample config in a sandbox with no
real Vault/Azure/DB credentials, expect a mix of `ok` results (for
unauthenticated public endpoints like the GitHub/Azure/Google status feeds)
and `error`/`down` results for anything requiring real credentials — that's
expected, not a bug.

Run the test suite:

```bash
uv run pytest
```

## Docker build

```bash
docker build -t healthcheck-engine .
```

Run the image locally with the sample config mounted in:

```bash
docker run --rm \
  -e CHECKS_CONFIG=/etc/healthcheck/checks.yaml \
  -v "$(pwd)/config:/etc/healthcheck:ro" \
  healthcheck-engine
```

## Deploy

Kubernetes manifests live in `deploy/`:

- `deploy/serviceaccount.yaml` — the ServiceAccount the pod runs as. Its
  `azure.workload.identity/*` annotation/label are inert as far as this engine
  is concerned: all Azure authentication is explicit service-principal
  authentication (`ClientSecretCredential`), never `DefaultAzureCredential`.
  The SPN is supplied through the `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` /
  `AZURE_CLIENT_SECRET` environment variables wired in
  `deploy/deployment.yaml` from a `healthcheck-engine-spn` Secret — the engine
  fails fast at startup if any of the three is missing. A check may override
  this with a distinct SPN by adding an optional `vault_path` param (secret
  keys: `tenant_id`, `client_id`, `client_secret`).
- `deploy/configmap.yaml` — supplies `checks.yaml` to the container via a
  mounted volume. Its `data.checks.yaml` content must be kept in sync with
  `config/checks.yaml` — paste the real config in (or template it with
  kustomize/Helm) before applying.
- `deploy/deployment.yaml` — the Deployment itself. Its `image:` field
  (`<REGISTRY>/healthcheck-engine:0.1.0`) is a placeholder — point it at
  the image built and pushed for the target environment.

## How to add a connector

Use `src/healthcheck_engine/connectors/public_api.py` as the reference
pattern — it's the simplest connector.

1. Create `src/healthcheck_engine/connectors/<name>.py` with a class that
   extends `Connector` (from `connectors/base.py`) and implements
   `async def check(self) -> CheckResult`. `check()` must never raise —
   catch expected failure modes and map them to a `CheckResult` with the
   appropriate `status` and `error_type` (see `public_api.py` for the
   pattern of catching `httpx.TimeoutException` / `httpx.TransportError`
   and returning `DOWN`/`ERROR` results instead of propagating).
2. Register the class in `src/healthcheck_engine/connectors/__init__.py`'s
   `REGISTRY` dict, keyed by the `type` string used in config.
3. Add a sample entry under `checks:` in `config/checks.yaml` using the new
   `type` and whatever `params` the connector expects.
4. Add tests in `tests/test_<name>.py` that mock any external calls (see
   `tests/test_public_api.py`, which uses `respx` to mock HTTP — no real
   network calls in tests).
