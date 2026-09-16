# Lumen Telegram Bot Audit & Engineering Pass

Date: 2026-09-16

## Executive summary

The current repository is a Python/FastAPI relay with an optional Telegram polling sales bot. The bot persists users, orders, gift codes, discount codes, card settings, and pending conversational state in `telegram_store.json`. The primary web application is server-rendered HTML/CSS/JavaScript from `pages.py`.

This pass removed the optional circumvention transport and its build/runtime surface, tightened the store's redemption/payment invariants, added an account view to the Telegram IA, and replaced the active dashboard navigation presentation with a clean responsive baseline.

## Architecture and dependency map

- `main.py` is the FastAPI/Uvicorn entry point and startup/shutdown coordinator.
- `telegram_bot.py` is loaded by `main.py`; polling starts only when `TELEGRAM_BOT_TOKEN` is configured.
- Telegram updates enter `_poll_loop`, then dispatch to `_handle_message` or `_handle_callback`.
- User/order state is persisted atomically through a temporary file replacement at `DATA_DIR/telegram_store.json`.
- Service ownership is checked against `LINKS[uuid].owner_telegram_id` before config, subscription, and renewal actions.
- Service creation/renewal is delegated to the existing `main.py` link/subscription functions.
- The dashboard and public subscription page are rendered from `pages.py`; web navigation uses an inline sidebar/drawer and page sections.

## Telegram flows audited

### Entry and navigation

- `/start` and `/menu` clear pending conversational state and render the main menu.
- Main menu now exposes: My services, Buy service, Renew service, Wallet, My account, Connection guide, and admin tools for authorized admins.
- Inline keyboards are used for normal navigation; message editing is used for most menu transitions.
- Back/Home buttons are present on menu, plan, wallet, account, guide, service, and admin views.
- Unknown commands and unknown callbacks fall back to a safe menu rather than exposing an exception.
- Pending state is cleared on `/start`, `/menu`, and key cancellation paths.

### Account and service management

- Account view reports the Telegram user identifier, balance, and active/total owned services.
- Service lists are filtered by `owner_telegram_id`.
- Config and subscription callbacks repeat the ownership check server-side.
- Expired or exhausted services are shown as unavailable and cannot be treated as active by the existing `is_link_allowed` check.

### Purchases and renewals

- Plan selection only accepts an active server-side plan ID.
- Orders are created as `draft`, then move to `awaiting_receipt`, `pending_admin`, `processing`, and `approved`.
- Wallet payment is serialized under `_provision_lock` and is idempotent after approval.
- Card receipt approval is admin-only and idempotent.
- Renewal preserves remaining unused quota and extends from the later of now or the current expiry.
- Card-payment abandonment and cancellation release reserved promotion usage.

## Gift codes

Current storage: `STORE["gift_codes"]`, persisted in `telegram_store.json`.

Rules verified:

- Format: normalized uppercase ASCII/Arabic-digit-compatible code, `3..32` characters, `[A-Z0-9_-]`.
- Server-side validation is performed in `_redeem_gift`; client input is not trusted.
- Codes carry amount, active flag, expiration, `max_uses`, and `used_by`.
- Redemption is recorded in both the code's `used_by` list and the user's `gift_codes` list.
- Per-user reuse is rejected before crediting.
- Global usage limit is rejected before crediting.
- Missing user, malformed code, missing code, inactive/expired code, and non-positive value are deterministic user-safe errors.
- The mutation is performed while `_state_lock` is held, preventing duplicate in-process redemption.
- Internal persistence exceptions are logged server-side and are not sent as raw database errors to Telegram.

Known limitation: the persistence layer is JSON/file based and therefore does not provide a cross-process transactional primitive. A multi-replica deployment must remain single-writer or move these counters to a transactional database before horizontal scaling.

## Discounts / promo codes

Current storage: `STORE["discount_codes"]`.

- Supports percentage and fixed discounts.
- Codes have active/expiration state, global `max_uses`, `used_by`, order reservations, and an order list.
- Eligibility is centralized in `_discount_valid`.
- Calculation is centralized in `_apply_discount`.
- Percentage values are bounded to 1..100; fixed discounts are bounded to the order base amount.
- Payable amount is clamped so it can never be negative.
- A code is reserved atomically when wallet/card payment starts, and released for cancellation, rejection, expiration, or failed wallet provisioning.
- Stacking is not supported: an order stores one `discount_code`.
- Plan-specific, first-purchase, user-specific, minimum-purchase, referral, and renewal-specific promotion rules were not present in the repository and were not invented.

## Referral / invite / rewards audit

No complete referral attribution, invite ledger, or reward-granting implementation was found in the current application path. Keyword hits were limited to incidental terminology/configuration rather than an executable referral subsystem. No new financial rule was invented. This is documented as a product gap, not silently represented as implemented.

## Callback architecture

The existing compact callback format was retained to avoid breaking deployed buttons and stale messages. All state-changing callbacks now route through server-side order, ownership, admin, and status checks. Sensitive data is not placed in callback payloads; payloads contain short opaque order/service identifiers. Stale callbacks return a safe alert or menu state.

## Psiphon removal

Removed:

- `psiphon/` package and runtime manager/backend/config/state/health modules.
- `relay_psiphon.py` and its WebSocket route.
- Vendored `third_party/psiphon-tunnel-core/` source.
- Docker builder stage, binary copy, and runtime environment setting.
- Dashboard status panel, status polling, and translation/UI references.
- Psiphon deployment documentation, transport documentation, and obsolete tests.
- Psiphon environment/configuration ignore rules.

Verification: repository-wide case-insensitive search returns no `psiphon` references.

## Main menu and web navigation

### Telegram IA

1. My services — inspect/configure owned services.
2. Buy service — choose a plan.
3. Renew service — choose an owned service, then a plan.
4. Wallet — balance, top-up, and gift redemption.
5. My account — identity and service summary.
6. Connection guide — concise client setup instructions.
7. Admin tools — shown only to configured admin IDs.

### Web IA

The dashboard keeps the existing destinations and API contracts but the active navigation layer now uses a normal system sans stack, light surfaces, clear selected state, restrained borders/shadows, accessible focus rings, and a responsive drawer/bottom dock at narrow widths. Essential destinations remain available on mobile.

## Testing

### PASS

- Python bytecode compilation for the repository after the removal.
- `tests/store_bot_contract.py` — gift, discount, wallet, idempotent card approval, renewal carryover, stale reservation release, admin code creation, ownership, and persistence.
- `tests/panel_v28_contract.py` — existing dashboard/deployment contract.
- `tests/dashboard_resilience.mjs` — dashboard request/navigation resilience.
- Repository-wide Psiphon search — zero matches.

### BLOCKED / NOT TESTED

- Live Telegram Bot API callbacks were not run because no live bot token/webhook environment was available.
- Live payment-provider/card settlement was not tested; the repository uses admin-reviewed card receipts.
- Cross-process concurrent redemption was not tested against a transactional database because the current implementation is JSON/file based.
- Browser screenshot QA against a running authenticated dashboard was not available without a running deployment/session. Static CSS/HTML compilation and contract checks passed.
- A full legacy suite was not treated as authoritative where tests explicitly depended on the removed transport; those obsolete tests were removed rather than made to pass against deleted behavior.

## Known limitations and follow-up

1. Move Telegram store counters/orders to SQLite/Postgres with unique constraints and transactions before multi-replica deployment.
2. Add a real referral model only after product rules are specified.
3. Add a translation catalog if bilingual Telegram operation becomes a requirement; the current bot is Persian-first and contains hardcoded user-facing strings.
4. Add an authenticated browser E2E job for desktop, 390px mobile, RTL, and LTR screenshots.
