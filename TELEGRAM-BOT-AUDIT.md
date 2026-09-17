# Lumen Telegram Bot Audit — 2026-09-17

## Scope and dependency map

The active application starts in `main.py`. Its FastAPI lifespan loads JSON state, starts the optional Telegram polling task (`telegram_bot.start_bot`), and registers the protected WebSocket relay. The bot receives Telegram long-poll updates, dispatches `message` and `callback_query` payloads, persists commerce state in `${DATA_DIR}/telegram_store.json`, and provisions user-owned VLESS links through the imported `main` service functions.

The web panel is served from `pages.py`; it calls authenticated API endpoints in `main.py`. The current panel was based on Material 3 tokens but a right-side “Command Rail” composition. This pass restores a conventional labelled navigation hierarchy while retaining the Material 3 Expressive token system and its responsive bottom navigation.

## Telegram architecture

### Existing behavior discovered

- Entry point: FastAPI startup imports and calls `start_bot`; shutdown calls `stop_bot`.
- Transport: Telegram Bot API long polling via `getUpdates`; no webhook implementation.
- Commands: `/start`, `/menu`, and admin-only `/admin`. Reply keyboards are not used; all navigation uses inline keyboards.
- User records include Telegram ID, display metadata, wallet balance, owned-service subscription group, and redeemed gifts.
- Commerce records are JSON-persisted order snapshots. Card receipts are stored by Telegram file ID and must be approved by a configured admin ID.
- Provisioning creates or renews a service through the existing `main.py` link/subscription functions. Ownership is recorded server-side as `owner_telegram_id`.

### Changes made

- Removed the duplicated, unreachable admin gift-wizard branch. The normal conversation dispatcher now uses the single authoritative gift/promo wizard implementation.
- Added callback-chat validation: callbacks must come from a private chat and the sender ID must match that private chat’s user ID.
- Added callback byte-length validation and safe parsing for malformed renewal/admin-page callbacks.
- Added a clear unknown-command response rather than silently treating a command as a menu reset.
- Made polling restart-safe: the next Telegram update offset is persisted after every received update, preventing replay after a normal restart.
- Kept the existing inline-keyboard information architecture: **My services**, **Buy service**, **Renew**, **Wallet**, **Connection guide**, and an admin-only store panel. Each customer detail screen retains a predictable back path; cancellation returns to the home menu.

### Callback and state findings

Order and service callbacks include only short IDs and every state-changing handler checks order/service ownership against the authenticated Telegram update sender. Admin callbacks are checked against `TELEGRAM_ADMIN_IDS`. The current payload vocabulary is compact legacy shorthand (`buy:`, `ren:`, `payw:`, etc.); it is bounded below Telegram’s 64-byte callback limit and now validated at the ingress boundary. A future non-breaking rename can introduce namespaced payloads while retaining aliases for already-sent buttons.

## Gift codes

### Current behavior and rules

- Codes are admin-created; format is normalized server-side to uppercase `A-Z`, `0-9`, `_`, and `-`, 3–32 characters.
- Storage: `telegram_store.json` → `gift_codes`.
- Values are wallet credits, not direct subscriptions.
- Codes support expiration, activation state, global usage limits, and one redemption per user.
- Redemption checks and balance credit run under the bot state lock, so concurrent polling tasks cannot redeem the same code twice in-process.
- Invalid, expired, duplicate, exhausted, malformed, and missing-user cases receive safe user-facing errors with no internal exception disclosure.

### Changes and tests

- Added bounded `redemption_audit` persistence with code, user, amount, and timestamp; it is written atomically with the wallet credit.
- Existing atomic temp-file replacement and restrictive file permissions remain in use.
- Regression coverage verifies valid and duplicate redemption, persistence, and the wallet result. Expiration/limit conditions are validated by the same guarded redemption path.

### Limitation

JSON plus an in-process lock is safe for this single-process deployment model, not multi-replica distributed redemption. Deploying multiple application replicas against the same JSON volume remains unsupported for commerce writes.

## Discounts / promos

The bot supports percent and fixed-amount codes. Server-side validation covers active/expiration status, global cap, per-user cap, order ownership, draft state, and excludes wallet top-ups. Calculation is centralized in `_apply_discount`; the final discount is capped so a purchase never becomes zero/negative (a 1,000-toman minimum remains). Reservations are made exactly once when a payment flow begins and released on rejected, cancelled, failed, or stale card-payment flows. Tests cover percentage calculation, wallet use, card approval idempotency, cancellation, rejection, and expiry release.

The repository does not define plan-specific, first-purchase, referral, stacking, or renewal-specific promotion rules. None were invented in this pass.

## Referral / rewards / trials

No referral link/code generation, attribution, reward ledger, bonus, campaign, or trial implementation was found outside generic keyword hits. No financial/reward behavior was added because the repository does not define the business rules.

## Main menu and web navigation

### Previous structure

The web panel had a dense right-side icon-first command rail and a mobile horizontal rail. It used Material 3 color/type tokens but did not match the requested normal application-navigation direction.

### New structure

Desktop now uses a conventional left, labelled Material 3 Expressive sidebar:

1. **Workspace:** Control room, Route studio, Subscriptions, Sub groups
2. **Observe:** Connections, Traffic, Activity log, Errors
3. **Tools:** Security, WebSocket test, Settings

The active state uses Material 3 containers; badges remain visible; controls keep focus-visible treatment. On small screens the same destinations become a labelled, horizontally scrollable bottom navigation, preserving the reading order and avoiding a hidden drawer dependency. Keyboard activation is supported for navigation items.

## Removed transport integration

The removed optional transport was deleted end-to-end: Python package, relay module, WebSocket route, subscription-profile generation, lifecycle manager, proxy retest paths, API endpoints, dashboard status card/scripts, Docker builder stage/binary/configuration, documentation, notices, vendored source, and dedicated tests. The final repository scan found no remaining references outside the self-check’s split-string detector.

## Verification

### PASS

- Python compilation of app, panel, bot, and tests.
- Store bot contract: gift redemption, promo calculation/reservation release, wallet/card idempotency, provisioning, renewal, ownership, and persistence.
- Telegram navigation contract: private-chat authorization, callback validation, state recovery, and redemption audit presence.
- Removed-transport source scan and no-remnant regression contract.
- Existing API route, address/SNI, countries, panel contract, protocol, state persistence, transport registry, and WS-only contracts.
- Diff whitespace check.

### BLOCKED

- Live FastAPI/browser rendering: the provided sandbox lacks the repository runtime dependency `aiofiles`; `python main.py` exits before binding. No live server, Telegram API, payment review, mobile browser rendering, or deployed Railway verification was claimed.
- Telegram Bot API interaction cannot be performed safely without a real configured test bot/token and isolated payment account.

### NOT TESTED

- A true concurrent multi-process gift redemption race (not supported by the JSON persistence architecture).
- Real card transfer confirmation, real Telegram receipt download, and external deployment behavior.
- Referral/trial/reward flows: no implementation exists to test.

## Recommended follow-up

Run this exact archive in a dependency-complete staging environment; then execute the full browser/mobile matrix and a dedicated Bot API test chat. Before multi-replica deployment, migrate the bot store and commerce state from JSON to a transactional database with unique redemption constraints.

---

# Commerce, Trial, Plan & Admin Assignment Pass — 2026-09-17

## Audit result and price-pipeline root cause

The Telegram store remains the single commerce implementation. Plans, order snapshots, discount application/reservation, wallet deduction, card receipt approval, and durable bot data are all in `telegram_bot.py`; service ownership and link persistence remain in `main.py` state. No separate web checkout catalogue was found.

The prior defect was the discount floor: after calculating a percent/fixed discount, the code capped it to `base - 1,000`, deliberately preventing a free order. This conflicted with 100% promotions and made the price rules non-canonical. The pass replaces that branch with `_calculate_payable(base_amount, code_entry)`, the single price calculator. It clamps the base and discount safely, caps percent discounts at 100%, and returns `(payable, discount)` where both are always non-negative and `payable + discount == base`.

## Discount and zero-cost behavior

- `100,000` at 0%, 10%, 50%, 100%, and 125% produces `100,000`, `90,000`, `50,000`, `0`, and `0` respectively.
- A zero-price plan remains zero under every valid promotion.
- The order stores the canonical payable value only; the wallet path cannot deduct a negative value.
- Zero-cost orders have a dedicated **فعال‌سازی رایگان** action. They never create a card-payment/receipt request and are rejected if a forged card-payment path is attempted.
- Promo reservation remains exactly-once at completion. Successful zero-cost orders legitimately consume an applied promotion; cancellation, rejected receipt, stale receipt, and failed free/wallet provisioning release reservations.

## Trial

The canonical Trial is `150 MB` (binary `150 * 1024²` bytes), `1 day`, and `0 تومان`. `trial_used_at` is a durable field on the existing Telegram user record. The Trial is hidden after use, stale purchase callbacks are rejected server-side, and renewal does not offer Trial.

The marker is written only after provisioning succeeds. `_finish_order` is protected by the existing provisioning lock, so concurrent free Trial completions can yield only one success. Failed provisioning returns the order to a safe draft state and leaves the user eligible.

## Canonical user-facing plans

The old environment-overridable six-plan catalogue was removed. The one canonical `DEFAULT_PLANS` list now contains exactly:

| Plan | Traffic | Duration | Price |
|---|---:|---:|---:|
| اقتصادی | 10 GB | 25 days | 80,000 تومان |
| استاندارد | 20 GB | 30 days | 100,000 تومان |
| تستی | 150 MB | 1 day | رایگان |

All plan menus, checkout orders, provisioning, renewal, admin plan view, and tests use that list. The Trial is excluded from renewal choices.

## Admin global proxy assignment

A new **🌍 تخصیص پراکسی** item is available only in the Telegram admin panel. It lists only current, already-supported locations derived from `proxy_repository.records_for_country()` and exposes country/flag only—never endpoints, IDs, or credentials. The flow requires target selection, shows server-calculated affected user/service counts, and requires an explicit confirm/cancel action.

Eligibility is each persisted service link with `store_managed` and an existing Telegram owner record; users who merely opened the bot are excluded. On confirmation the target is revalidated against the live repository, eligible links are updated to the selected repository proxy, the main state is saved, and only then a bounded safe `admin_audit` entry is persisted. Repeating the same assignment is idempotent: already-targeted links are skipped. Authorization, private-chat sender binding, callback length, workflow state, and selected country are all verified server-side.

## Verification

### PASS

- `python -m compileall -q .`
- `tests/store_bot_contract.py`: canonical plan values; 0–100%+ pricing; zero amount; free path; Trial concurrency/lifetime; gift, promo, wallet, card, renewal, ownership, persistence; admin assignment authorization, idempotency, and audit logging.
- Telegram callback/navigation, removed-transport, WS-only, transport registry, API route, address/SNI, countries, dashboard contract, protocol, and persistence contracts.
- Final scans returned no old plan IDs/prices and no removed-transport references.

### BLOCKED

- Live Telegram Bot API, real card-transfer review, and deployed-environment validation require a configured isolated test bot, payment account, and deployment credentials; they were not invoked.

### NOT TESTED

- Full browser/mobile UI interaction matrix (no panel plan catalogue is present).
- Distributed multi-replica JSON-store writes; the existing architecture is single-process for bot-commerce mutations.
