# Production deployment

This deployment uses the root `Dockerfile` and `railway.json`. The container
starts the application with:

```sh
python main.py
```

Do not replace this with `uvicorn main:app`: `main.py` deliberately prepares
the relay's tuned dual-stack listening socket before starting Uvicorn.
Railway supplies `PORT`; it must not be set to `443`. Railway terminates public
HTTPS/WSS on the service domain and forwards traffic to this HTTP listener.

## Railway deployment steps

1. Deploy this repository with **Root Directory** set to `/`. Railway reads
   `railway.json`, builds the root `Dockerfile`, and starts its Docker `CMD`.
2. Add a persistent Railway Volume mounted at `/data`.
3. Set `DATA_DIR=/data`,
   `LUMEN_REQUIRE_PERSISTENT_STORAGE=true`, and
   `RAILWAY_VOLUME_MOUNT_PATH=/data`.
4. Configure a secure `ADMIN_PASSWORD`; set a stable `SECRET_KEY` or allow the
   application to create one inside the persistent `/data` volume.
5. Generate the service's public domain in Railway. Railway provides the
   public `PORT`; set `RAILWAY_PUBLIC_DOMAIN` only when an explicit domain
   override is needed.
6. Configure a health check for `GET /health` with a 300-second deployment
   grace period. This is already declared in `railway.json`.
   leaves the primary VLESS WebSocket route unchanged.

The image's entrypoint creates/chowns `/data` and then starts the application
as the unprivileged `lumen` user. Keep operator-provided Core binaries,
configuration, certificates, and all secrets outside the Git checkout and
outside the Docker build context.

## Transport paths

| Client profile | Public path | Backend |
|---|---|---|
| Existing VLESS WebSocket | `/ws/{UUID}` | Existing exact outbound proxy relay |
| Optional Raw TCP | Railway TCP Proxy | Separate TLS listener, only when its deployment variables validate |

The first two profiles use the **same account UUID**. The path is the only
and fails closed when Core is unavailable. The base service needs no extra
public port for this route.

## Core service variables

| Variable | Production requirement | Safe format/example | Secret? |
|---|---|---|---|
| `PORT` | Railway-provided; do not set manually in normal Railway deployments | `8080` | No |
| `DATA_DIR` | Required for durable state | `/data` | No |
| `LUMEN_REQUIRE_PERSISTENT_STORAGE` | Required in production | `true` | No |
| `RAILWAY_VOLUME_MOUNT_PATH` | Required when persistent storage is enforced | `/data` | No |
| `ADMIN_PASSWORD` | Required in production; the source fallback is not safe for production | long random value stored as a Railway secret | **Yes** |
| `SECRET_KEY` | Recommended; required if `/data` cannot retain the generated key across restarts | long random value stored as a Railway secret | **Yes** |
| `RAILWAY_PUBLIC_DOMAIN` | Usually injected/detected by Railway; set only for an explicit public-domain override | `relay.example.com` | No |
| `APP_BUILD_ID` | Optional deployment traceability | Git commit SHA | No |
| `LUMEN_STATE_SNAPSHOT_B64` | Optional signed recovery snapshot managed by the update workflow | opaque base64 value | **Yes** |
| `VLESS_ADDRESSES` | Optional validated client address choices | `edge.example.com,203.0.113.10` | No |
| `VLESS_SNI_NAMES` | Optional validated client SNI choices | `relay.example.com` | No |
| `HTTP_PROXY_TOKEN` | Optional authentication for the app's separate HTTP proxy endpoint | long random value | **Yes** |

## Optional managed proxy repository

Leave these unset when the managed catalog is not used. The application
continues to start without them, but managed catalog refresh is unavailable.

| Variable | Requirement | Safe format/example | Secret? |
|---|---|---|---|
| `LUMEN_S3_ACCESS_KEY_ID` | Required only for private S3 catalog access | provider-issued access-key ID | **Yes** |
| `LUMEN_S3_SECRET_ACCESS_KEY` | Required only for private S3 catalog access | provider-issued secret | **Yes** |
| `PROXY_REPOSITORY_MANUAL_REFRESH_KEY` | Optional; enables the protected manual-refresh control when at least 24 characters | long random value | **Yes** |
| `ENV_SECRET_KEY_TO_BUTTON_ON_N` | Legacy alias for the preceding refresh key; do not set both | long random value | **Yes** |


## Optional Raw TCP transport

Raw TCP is deployment-gated and does not affect HTTPS/WSS. All fields below
must validate together before its listener starts:

| Variable | Requirement | Safe format/example | Secret? |
|---|---|---|---|
| `RAILWAY_TCP_APPLICATION_PORT` | Required for Raw TCP | `7000` | No |
| `RAILWAY_TCP_PROXY_DOMAIN` | Required for Raw TCP | `tcp.example.com` | No |
| `RAILWAY_TCP_PROXY_PORT` | Required for Raw TCP | `443` | No |
| `VLESS_TCP_LISTEN_HOST` | Optional; bind address for dedicated listener | `0.0.0.0` | No |
| `VLESS_TCP_LISTEN_PORT` | Required; must equal application TCP port | `7000` | No |
| `VLESS_TCP_TLS_CERT_FILE` | Required; secure TLS certificate path | `/run/tls/cert.pem` | **Yes** |
| `VLESS_TCP_TLS_KEY_FILE` | Required; secure TLS private-key path | `/run/tls/key.pem` | **Yes** |
| `VLESS_TCP_URI_NETWORK` | Required; client-validated spelling | `raw` or `tcp` | No |
| `VLESS_TCP_DEFAULT_SNI` | Required | `tcp.example.com` | No |
| `VLESS_TCP_SNI_MAP` | Required validated JSON hostname-to-location map | `{"tcp.example.com":""}` | No |
| `VLESS_TCP_MAX_CONNECTIONS` | Optional, 1–1024 | `128` | No |

The Docker image deliberately excludes `*.pem` and `*.key`; mount this
material securely at deployment time. Do not enable Raw TCP unless its
Railway TCP Proxy and TLS/SNI setup have passed staging validation.

## Optional Telegram sales bot

| Variable | Requirement | Safe format/example | Secret? |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Required only to enable the bot | provider-issued bot token | **Yes** |
| `TELEGRAM_ADMIN_IDS` | Optional; comma-separated numeric administrator IDs | `123456789,987654321` | Sensitive |
| `STORE_NAME` | Optional | `Example Service` | No |
| `STORE_IP_LIMIT` | Optional non-negative integer | `2` | No |
| `STORE_RECEIPT_TIMEOUT_HOURS` | Optional positive integer | `24` | No |
| `STORE_PLANS_JSON` | Optional validated JSON plan list | `[{"id":"basic","gb":10,"days":30,"price":1}]` | No |
| `STORE_CARD_NUMBER` | Optional payment information | stored as a Railway secret | **Yes** |
| `STORE_CARD_HOLDER` | Optional payment information | stored as a Railway secret | Sensitive |
| `STORE_SUPPORT_USERNAME` | Optional support username without `@` | `support_account` | No |

## Optional updater

These are needed only for the authenticated in-app GitHub/Railway update
workflow—not for normal relay startup.

| Variable | Requirement | Safe format/example | Secret? |
|---|---|---|---|
| `LUMEN_GITHUB_TOKEN` | Optional updater token | GitHub fine-grained token | **Yes** |
| `LUMEN_RAILWAY_TOKEN` | Optional updater token | Railway project token | **Yes** |
| `LUMEN_FORK_REPO` | Optional fork override | `owner/repository` | No |
| `LUMEN_GIT_BRANCH` | Optional branch fallback | `main` | No |
| `RAILWAY_GIT_BRANCH` | Railway-provided/optional branch | `main` | No |
| `LUMEN_CREDENTIAL_SOURCE` | Optional audit label | `installer` or `manual` | No |
| `RAILWAY_PROJECT_ID` | Railway-provided for updater | opaque Railway ID | No |
| `RAILWAY_SERVICE_ID` | Railway-provided for updater | opaque Railway ID | No |
| `RAILWAY_ENVIRONMENT_ID` | Railway-provided for updater | opaque Railway ID | No |
| `RAILWAY_GIT_REPO_OWNER` | Railway-provided repository metadata | GitHub owner | No |
| `RAILWAY_GIT_REPO_NAME` | Railway-provided repository metadata | repository name | No |

## Smoke checks after deployment

```sh
# Railway's health check target
curl -fsS https://YOUR_PUBLIC_DOMAIN/health

# Authenticated, safe operational status; do not expose this endpoint publicly
# without the existing session authentication.
```

staging, verify the manager reports `HEALTHY`, then confirm the extra
standard VLESS/WS subscription profile uses the exact `/ws/p-core/cq/{UUID}`
path. A Core failure must leave the existing WebSocket service operational.
