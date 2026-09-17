# Railway Deployment Failure Diagnosis & Fix

## Failure summary

- Failing phase: application startup/runtime import.
- Classification: Python import failure, followed by container restart and healthcheck failure.
- Build, Docker image creation, and dependency installation were not the first failing phase in the supplied log.

## Evidence from the Railway log

The complete supplied log contains 506 lines and 41,394 bytes. The first meaningful traceback is repeated from `2026-09-16T19:24:03Z` onward:

```text
File "/app/main.py", line 2149, in <module>
  from telegram_bot import start_bot as _tg_start_bot, stop_bot as _tg_stop_bot
File "/app/telegram_bot.py", line 16, in <module>
  from main import (...)
ImportError: cannot import name 'set_link_sub' from '__main__' (/app/main.py)
```

The FastAPI `on_event` messages are deprecation warnings. The repeated volume mounts and repeated tracebacks are restart cascades. The application exits during module import, before it can serve `/health`.

## Root cause

The post-audit repository's `telegram_bot.py` imports `set_link_sub` from `main.py`, but the deployed `main.py` no longer defined that shared coroutine. Because Railway runs `python main.py`, the entry module is named `__main__`; the existing alias correctly maps `sys.modules["main"]` to that same module. That makes the missing symbol appear as coming from `__main__`, but the alias is not the defect.

The regression was caused by deleting the function while removing unrelated code. It was not caused by Railway variables, the port, Docker, the healthcheck path, or the removed transport.

## Fix applied

Restored `set_link_sub` immediately after `create_sub_group` in `main.py`.

The restored function preserves the existing behavior:

- Validates the link and target subscription group.
- Preserves raw-TCP multi-location compatibility checks.
- Removes the link from its previous group.
- Adds it to the target group without duplicates.
- Updates `LINKS[uid]["sub_id"]`.
- Persists state using the existing strict save path.
- Records the existing activity event.
- Returns `False` for missing links/groups or incompatible routes instead of faking success.

No removed transport, new dependency, secret, or Railway variable was added.

## Files changed

- `main.py` — restored the missing `set_link_sub` shared coroutine.
- `tests/startup_symbol_contract.py` — added a regression guard for the import surface and removed-transport boundary.
- `RAILWAY-DEPLOYMENT-FIX.md` — this diagnosis and verification report.

## Startup contract checked

- Docker command: `python main.py`.
- Working directory: `/app`.
- Railway-provided `PORT` remains used by the existing application configuration.
- Healthcheck: `/health`.
- Healthcheck timeout: 300 seconds.
- `requirements.txt` still includes `aiofiles`, which is imported by `main.py` and installed by the Docker build.
- Docker entrypoint only prepares `/data` and forwards the configured command.

## Verification

### PASS

- Entire Railway log read before diagnosis.
- `python -m compileall -q .`.
- Static startup-symbol contract: `set_link_sub` is defined in `main.py`.
- `tests/startup_symbol_contract.py`.
- Static Telegram-to-main import-surface check: all imported names exist in `main.py`.
- Static removed-transport contract: no runtime import or API route for the removed transport.
- `tests/store_bot_contract.py`.
- `tests/transport_registry_contract.py`.
- `tests/exact_routing_dashboard_contract.py`.
- `tests/ws_only_contract.py`.
- `tests/dashboard_resilience.mjs`.

### BLOCKED

- Full import smoke test could not run in the sandbox because the sandbox does not have the runtime packages installed (`fastapi` was missing). This is expected to be supplied by the Docker `pip install -r requirements.txt` step.
- Docker build was not available in the sandbox.

### NOT TESTED

- A new Railway deployment.
- Live `/health` response on Railway after redeployment.
- Live Telegram Bot API interaction.

## Railway configuration requirements

No new variable is required. Do not disable `/health`, change the start command, or add any removed-transport variables.

## Minimal next deployment step

Deploy the updated repository and verify that the service remains running and `GET /health` returns a 2xx response. Railway live verification is still pending; the repository fix itself is complete.

## Remaining risk

The local sandbox cannot reproduce the final import with installed FastAPI dependencies, so the first verification of the complete container image must occur through the next Railway deployment.
