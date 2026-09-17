# telegram_bot.py
# Telegram sales bot for the WS-only service store.

import asyncio
import copy
import html
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import proxy_repository

from main import (
    LINKS,
    LINKS_LOCK,
    SUBS,
    DEFAULT_FINGERPRINT,
    DEFAULT_PORT,
    create_sub_group,
    fmt_bytes,
    get_host,
    is_link_allowed,
    logger,
    make_link,
    save_state,
    set_link_sub,
    vless_link_for_link,
)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_admin_ids_raw = os.environ.get("TELEGRAM_ADMIN_IDS", "").strip()
ADMIN_IDS = {
    int(x) for x in _admin_ids_raw.replace(" ", "").split(",") if x.isdigit()
} if _admin_ids_raw else set()

API_BASE = "https:" + "//api.telegram.org/bot" + BOT_TOKEN
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STORE_FILE = DATA_DIR / "telegram_store.json"
STORE_NAME = os.environ.get("STORE_NAME", "فروشگاه اینترنت پرسرعت").strip() or "فروشگاه اینترنت پرسرعت"
STORE_IP_LIMIT = max(0, int(os.environ.get("STORE_IP_LIMIT", "2") or 2))
MIN_TOPUP = 20_000
MAX_TOPUP = 20_000_000
RECEIPT_TIMEOUT_HOURS = max(1, int(os.environ.get("STORE_RECEIPT_TIMEOUT_HOURS", "24") or 24))
PAGE_SIZE = 6

# Canonical customer catalogue. It is intentionally not environment-overridable:
# all Telegram checkout, renewals, admin views, tests, and provisioning use this one list.
DEFAULT_PLANS = [
    {"id": "economic", "name": "اقتصادی", "quota_bytes": 10 * 1024 ** 3, "quota_label": "10 GB", "days": 25, "price": 80_000, "trial": False, "active": True},
    {"id": "standard", "name": "استاندارد", "quota_bytes": 20 * 1024 ** 3, "quota_label": "20 GB", "days": 30, "price": 100_000, "trial": False, "active": True},
    {"id": "trial", "name": "تستی", "quota_bytes": 150 * 1024 ** 2, "quota_label": "150 MB", "days": 1, "price": 0, "trial": True, "active": True},
]
PLANS = copy.deepcopy(DEFAULT_PLANS)
PLAN_BY_ID = {p["id"]: p for p in PLANS}


def _default_store():
    return {
        "version": 2,
        "poll_offset": 0,
        "users": {},
        "orders": {},
        "gift_codes": {},
        "discount_codes": {},
        "redemption_audit": [],
        "admin_audit": [],
        "settings": {
            "store_name": STORE_NAME,
            "card_number": os.environ.get("STORE_CARD_NUMBER", "").strip(),
            "card_holder": os.environ.get("STORE_CARD_HOLDER", "").strip(),
            "support": os.environ.get("STORE_SUPPORT_USERNAME", "").strip().lstrip("@"),
            "currency": "تومان",
        },
    }


STORE = _default_store()
_state_lock = asyncio.Lock()
_save_lock = asyncio.Lock()
_provision_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None
_poll_task: asyncio.Task | None = None
_running = False
_pending: dict[int, dict] = {}

_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")


def _now() -> datetime:
    return datetime.now()


def _iso() -> str:
    return _now().isoformat()


def _e(value) -> str:
    return html.escape(str(value or ""), quote=False)


def _money(value: int) -> str:
    return f"{max(0, int(value or 0)):,} تومان"


def _parse_int(text: str, minimum=0, maximum=10**12):
    cleaned = str(text or "").translate(_FA_DIGITS).replace(",", "").replace("٬", "").strip()
    try:
        value = int(cleaned)
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None


def _normalize_code(text: str):
    code = str(text or "").translate(_FA_DIGITS).strip().upper().replace(" ", "")
    return code if _CODE_RE.fullmatch(code) else None


def _is_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_IDS


def _active_plan(plan_id: str):
    plan = PLAN_BY_ID.get(plan_id)
    return plan if plan and plan.get("active", True) else None


def _expired(iso_value: str | None) -> bool:
    if not iso_value:
        return False
    try:
        return _now() >= datetime.fromisoformat(iso_value)
    except Exception:
        return True


def _new_order_id() -> str:
    while True:
        oid = "O" + secrets.token_hex(6).upper()
        if oid not in STORE["orders"]:
            return oid


def _write_store_sync(snapshot: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STORE_FILE)


async def _save_store():
    async with _save_lock:
        async with _state_lock:
            snapshot = copy.deepcopy(STORE)
        try:
            await asyncio.to_thread(_write_store_sync, snapshot)
        except Exception as exc:
            logger.warning("Could not save Telegram store: %s", exc)


async def _load_store():
    global STORE
    try:
        if STORE_FILE.exists():
            raw = await asyncio.to_thread(STORE_FILE.read_text, encoding="utf-8")
            loaded = json.loads(raw)
            base = _default_store()
            for key in ("users", "orders", "gift_codes", "discount_codes"):
                # Persistent collections are schema-checked below.
                
                if isinstance(loaded.get(key), dict):
                    base[key] = loaded[key]
            if isinstance(loaded.get("redemption_audit"), list):
                base["redemption_audit"] = loaded["redemption_audit"][-5000:]
            if isinstance(loaded.get("admin_audit"), list):
                base["admin_audit"] = loaded["admin_audit"][-1000:]
            if isinstance(loaded.get("poll_offset"), int) and loaded["poll_offset"] >= 0:
                base["poll_offset"] = loaded["poll_offset"]
            if isinstance(loaded.get("settings"), dict):
                base["settings"].update(loaded["settings"])
            # Environment variables intentionally override persisted blank/old deployment values.
            if os.environ.get("STORE_CARD_NUMBER", "").strip():
                base["settings"]["card_number"] = os.environ["STORE_CARD_NUMBER"].strip()
            if os.environ.get("STORE_CARD_HOLDER", "").strip():
                base["settings"]["card_holder"] = os.environ["STORE_CARD_HOLDER"].strip()
            if os.environ.get("STORE_SUPPORT_USERNAME", "").strip():
                base["settings"]["support"] = os.environ["STORE_SUPPORT_USERNAME"].strip().lstrip("@")
            STORE = base
        logger.info(
            "Telegram store loaded: %s users, %s orders",
            len(STORE["users"]), len(STORE["orders"]),
        )
    except Exception as exc:
        STORE = _default_store()
        logger.warning("Could not load Telegram store: %s", exc)


async def _ensure_user(from_user: dict):
    user_id = int(from_user.get("id"))
    key = str(user_id)
    changed = False
    async with _state_lock:
        user = STORE["users"].get(key)
        if user is None:
            user = {
                "id": user_id,
                "username": from_user.get("username") or "",
                "first_name": from_user.get("first_name") or "کاربر",
                "balance": 0,
                "created_at": _iso(),
                "last_seen_at": _iso(),
                "sub_id": None,
                "gift_codes": [],
                "trial_used_at": None,
            }
            STORE["users"][key] = user
            changed = True
        else:
            for field in ("username", "first_name"):
                new = from_user.get(field) or ("کاربر" if field == "first_name" else "")
                if user.get(field) != new:
                    user[field] = new
                    changed = True
            user["last_seen_at"] = _iso()
    if changed:
        await _save_store()
    return STORE["users"][key]


# ── Telegram API ─────────────────────────────────────────────────────────────
async def _call(method: str, **params):
    if _client is None:
        return None
    try:
        response = await _client.post(f"{API_BASE}/{method}", json=params, timeout=45)
        data = response.json()
        if not data.get("ok"):
            logger.warning("Telegram API %s failed: %s", method, data)
        return data
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Telegram API %s error: %s", method, exc)
        return None


async def _send(chat_id: int, text: str, kb: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if kb:
        payload["reply_markup"] = kb
    return await _call("sendMessage", **payload)


async def _edit(chat_id: int, message_id: int, text: str, kb: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if kb:
        payload["reply_markup"] = kb
    result = await _call("editMessageText", **payload)
    if not result or not result.get("ok"):
        await _send(chat_id, text, kb)


async def _answer(callback_id: str, text: str = "", alert: bool = False):
    await _call("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)


async def _send_receipt(admin_id: int, order: dict):
    caption = _admin_order_text(order)
    kb = _admin_review_kb(order["id"])
    receipt = order.get("receipt") or {}
    if receipt.get("type") == "photo":
        return await _call(
            "sendPhoto", chat_id=admin_id, photo=receipt.get("file_id"), caption=caption,
            parse_mode="HTML", reply_markup=kb,
        )
    if receipt.get("type") == "document":
        return await _call(
            "sendDocument", chat_id=admin_id, document=receipt.get("file_id"), caption=caption,
            parse_mode="HTML", reply_markup=kb,
        )
    return await _send(admin_id, caption, kb)


# ── Keyboards and views ──────────────────────────────────────────────────────
def _main_kb(user_id: int):
    rows = [
        [{"text": "📦 سرویس‌های من", "callback_data": "services"},
         {"text": "🛒 خرید سرویس", "callback_data": "plans"}],
        [{"text": "🔄 تمدید سرویس", "callback_data": "renew"},
         {"text": "💰 کیف پول", "callback_data": "wallet"}],
        [{"text": "📖 راهنمای اتصال", "callback_data": "guide"}],
    ]
    if _is_admin(user_id):
        rows.append([{"text": "🛠 پنل مدیریت فروش", "callback_data": "admin"}])
    return {"inline_keyboard": rows}


def _back_main_kb(user_id: int):
    return {"inline_keyboard": [[{"text": "⬅ بازگشت به منو", "callback_data": "menu"}]]}


def _main_text(user: dict):
    return (
        f"⚡️ <b>{_e(STORE['settings'].get('store_name') or STORE_NAME)}</b>\n\n"
        f"سلام {_e(user.get('first_name') or 'کاربر')} 👋\n"
        f"موجودی کیف پول: <b>{_money(user.get('balance', 0))}</b>\n\n"
        "همه سرویس‌ها به‌صورت خودکار با پروتکل پرسرعت <b>VLESS + WS</b> ساخته می‌شوند؛ "
        "بعد از خرید فقط لینک را وارد برنامه کن و هیچ تنظیم دستی لازم نیست."
    )


def _plans_kb(prefix="buy", service_id: str | None = None, user: dict | None = None):
    rows = []
    for p in PLANS:
        if not p.get("active", True) or (p.get("trial") and (prefix != "buy" or (user or {}).get("trial_used_at"))):
            continue
        data = f"{prefix}:{p['id']}" if not service_id else f"{prefix}:{service_id}:{p['id']}"
        price_label = "رایگان" if int(p["price"]) == 0 else _money(p["price"])
        rows.append([{"text": f"{p['name']} · {p['quota_label']} · {p['days']} روز — {price_label}", "callback_data": data}])
    rows.append([{"text": "⬅ بازگشت", "callback_data": "menu" if prefix == "buy" else "renew"}])
    return {"inline_keyboard": rows}


def _plans_text(title="خرید سرویس جدید", user: dict | None = None, include_trial: bool = True):
    lines = [f"🛒 <b>{title}</b>", "", "همه پلن‌ها WS، بدون محدودیت سرعت و آماده استفاده هستند:"]
    for p in PLANS:
        if p.get("active", True) and (include_trial or not p.get("trial")) and not (p.get("trial") and (user or {}).get("trial_used_at")):
            price_label = "رایگان" if int(p["price"]) == 0 else _money(p["price"])
            lines.append(f"• <b>{_e(p['name'])}</b>: <b>{p['quota_label']}</b> / {p['days']} روز — <b>{price_label}</b>")
    return "\n".join(lines)


def _order_text(order: dict):
    p = order.get("plan") or {}
    kind = {"new": "خرید سرویس جدید", "renew": "تمدید سرویس", "wallet": "شارژ کیف پول"}.get(order.get("kind"), "سفارش")
    out = [f"🧾 <b>{kind}</b>", f"کد سفارش: <code>{order['id']}</code>"]
    if p:
        out.extend([
            f"پلن: {_e(p.get('name'))}",
            f"حجم: {p.get('quota_label', '—')}",
            f"اعتبار: {p.get('days')} روز",
        ])
    out.append(f"مبلغ اولیه: {_money(order.get('base_amount', order.get('amount', 0)))}")
    if order.get("discount_amount"):
        out.append(f"تخفیف: −{_money(order['discount_amount'])} ({_e(order.get('discount_code'))})")
    out.append(f"<b>مبلغ قابل پرداخت: {_money(order.get('amount', 0))}</b>")
    return "\n".join(out)


def _order_pay_kb(order: dict, balance: int):
    oid = order["id"]
    if int(order.get("amount", 0)) == 0:
        return {"inline_keyboard": [
            [{"text": "✅ فعال‌سازی رایگان", "callback_data": f"free:{oid}"}],
            [{"text": "❌ لغو سفارش", "callback_data": f"cancel:{oid}"}],
        ]}
    return {"inline_keyboard": [
        [{"text": f"💰 پرداخت از کیف پول ({_money(balance)})", "callback_data": f"payw:{oid}"}],
        [{"text": "💳 کارت‌به‌کارت و ارسال رسید", "callback_data": f"payc:{oid}"}],
        [{"text": "🏷 وارد کردن کد تخفیف", "callback_data": f"disc:{oid}"}],
        [{"text": "❌ لغو سفارش", "callback_data": f"cancel:{oid}"}],
    ]}


def _wallet_kb():
    return {"inline_keyboard": [
        [{"text": "➕ شارژ با کارت‌به‌کارت", "callback_data": "topup"}],
        [{"text": "🎁 استفاده از کد هدیه", "callback_data": "gift"}],
        [{"text": "⬅ بازگشت به منو", "callback_data": "menu"}],
    ]}


def _service_rows(user_id: int, action="svc"):
    rows = []
    services = _user_services(user_id)
    for uid, link in services[:30]:
        icon = "🟢" if is_link_allowed(link) else "🔴"
        rows.append([{"text": f"{icon} {link.get('label', uid)[:28]}", "callback_data": f"{action}:{uid}"}])
    rows.append([{"text": "⬅ بازگشت", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def _service_kb(uid: str):
    return {"inline_keyboard": [
        [{"text": "🧾 دریافت کانفیگ تکی", "callback_data": f"cfg:{uid}"}],
        [{"text": "🔗 دریافت لینک اشتراک", "callback_data": f"sub:{uid}"}],
        [{"text": "🔄 تمدید این سرویس", "callback_data": f"ren:{uid}"}],
        [{"text": "⬅ سرویس‌های من", "callback_data": "services"}],
    ]}


def _service_text(uid: str, link: dict):
    limit = int(link.get("limit_bytes", 0) or 0)
    used = int(link.get("used_bytes", 0) or 0)
    remain = max(0, limit - used)
    exp = link.get("expires_at")
    exp_text = exp.split("T")[0] if exp else "بدون انقضا"
    status = "🟢 فعال" if is_link_allowed(link) else "🔴 غیرفعال یا تمام‌شده"
    return (
        f"📦 <b>{_e(link.get('label') or 'سرویس')}</b>\n"
        f"وضعیت: {status}\n"
        f"مصرف: {fmt_bytes(used)} از {fmt_bytes(limit)}\n"
        f"حجم باقی‌مانده: <b>{fmt_bytes(remain)}</b>\n"
        f"تاریخ پایان: <b>{_e(exp_text)}</b>\n"
        "پروتکل: <b>VLESS + WebSocket</b>\n"
        f"شناسه سرویس: <code>{uid}</code>"
    )


def _admin_kb():
    return {"inline_keyboard": [
        [{"text": "🧾 پرداخت‌های در انتظار", "callback_data": "ap:0"}],
        [{"text": "🎁 ساخت کد هدیه", "callback_data": "agift"},
         {"text": "🏷 ساخت کد تخفیف", "callback_data": "apromo"}],
        [{"text": "🌍 تخصیص پراکسی", "callback_data": "assign"}],
        [{"text": "💳 تنظیم کارت بانکی", "callback_data": "acard"}],
        [{"text": "📋 مشاهده پلن‌ها", "callback_data": "aplans"},
         {"text": "📊 آمار فروشگاه", "callback_data": "astats"}],
        [{"text": "⬅ منوی کاربران", "callback_data": "menu"}],
    ]}


async def _admin_gift_text(chat_id: int, text: str, pending: dict):
    action = pending["action"]
    data = pending.setdefault("data", {})
    if action == "gift_admin_code":
        code = _normalize_code(text)
        if not code or code in STORE["gift_codes"]:
            await _send(chat_id, "کد باید ۳ تا ۳۲ کاراکتر انگلیسی/عدد و یکتا باشد. دوباره بفرست:")
            return True
        data["code"] = code
        _pending[chat_id] = {"action": "gift_admin_amount", "data": data}
        await _send(chat_id, "مبلغ شارژ کیف پول را به تومان بفرست؛ مثلاً <code>50000</code>:")
    elif action == "gift_admin_amount":
        value = _parse_int(text, 1_000, 20_000_000)
        if value is None:
            await _send(chat_id, "مبلغ نامعتبر است. عددی بین ۱٬۰۰۰ تا ۲۰٬۰۰۰٬۰۰۰ بفرست:")
            return True
        data["amount"] = value
        _pending[chat_id] = {"action": "gift_admin_uses", "data": data}
        await _send(chat_id, "حداکثر تعداد استفاده را بفرست:")
    elif action == "gift_admin_uses":
        value = _parse_int(text, 1, 100_000)
        if value is None:
            await _send(chat_id, "تعداد استفاده نامعتبر است. دوباره بفرست:")
            return True
        data["max_uses"] = value
        _pending[chat_id] = {"action": "gift_admin_days", "data": data}
        await _send(chat_id, "اعتبار کد چند روز باشد؟ عدد ۰ یعنی بدون انقضا:")
    elif action == "gift_admin_days":
        days = _parse_int(text, 0, 3650)
        if days is None:
            await _send(chat_id, "روز اعتبار نامعتبر است. دوباره بفرست:")
            return True
        expires = (_now() + timedelta(days=days)).isoformat() if days else None
        async with _state_lock:
            STORE["gift_codes"][data["code"]] = {
                "code": data["code"], "amount": data["amount"], "max_uses": data["max_uses"],
                "used_by": [], "active": True, "created_at": _iso(), "created_by": chat_id,
                "expires_at": expires,
            }
        await _save_store()
        _pending.pop(chat_id, None)
        await _send(chat_id, f"✅ کد هدیه <code>{data['code']}</code> با مبلغ {_money(data['amount'])} ساخته شد.", _admin_kb())
    return True


def _admin_review_kb(order_id: str):
    return {"inline_keyboard": [[
        {"text": "✅ تایید پرداخت", "callback_data": f"aok:{order_id}"},
        {"text": "❌ رد پرداخت", "callback_data": f"ano:{order_id}"},
    ]]}


def _pending_orders_kb(page=0):
    orders = [o for o in STORE["orders"].values() if o.get("status") == "pending_admin"]
    orders.sort(key=lambda o: o.get("receipt_at", o.get("created_at", "")), reverse=True)
    start = max(0, page) * PAGE_SIZE
    rows = []
    for o in orders[start:start + PAGE_SIZE]:
        rows.append([{"text": f"🧾 {o['id']} · {_money(o.get('amount', 0))}", "callback_data": f"aord:{o['id']}"}])
    nav = []
    if start:
        nav.append({"text": "◀ قبلی", "callback_data": f"ap:{page-1}"})
    if start + PAGE_SIZE < len(orders):
        nav.append({"text": "بعدی ▶", "callback_data": f"ap:{page+1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "⬅ پنل مدیریت", "callback_data": "admin"}])
    return {"inline_keyboard": rows}


def _admin_order_text(order: dict):
    user = STORE["users"].get(str(order.get("user_id")), {})
    username = f"@{user.get('username')}" if user.get("username") else "بدون یوزرنیم"
    return (
        f"🧾 <b>رسید پرداخت جدید</b>\n"
        f"سفارش: <code>{order.get('id')}</code>\n"
        f"کاربر: {_e(user.get('first_name') or 'کاربر')} · {_e(username)}\n"
        f"Telegram ID: <code>{order.get('user_id')}</code>\n"
        f"نوع: {_e(order.get('kind'))}\n"
        f"مبلغ: <b>{_money(order.get('amount', 0))}</b>\n"
        f"وضعیت: {_e(order.get('status'))}"
    )


# ── Store business logic ─────────────────────────────────────────────────────
def _user_services(user_id: int):
    result = []
    sid = str(user_id)
    for uid, link in LINKS.items():
        if str(link.get("owner_telegram_id", "")) == sid:
            result.append((uid, link))
    result.sort(key=lambda x: x[1].get("created_at", ""), reverse=True)
    return result


async def _create_order(user_id: int, kind: str, plan: dict | None = None, service_id: str | None = None, amount: int | None = None):
    async with _state_lock:
        oid = _new_order_id()
        base_amount = max(0, int(amount if amount is not None else plan["price"]))
        order = {
            "id": oid,
            "user_id": int(user_id),
            "kind": kind,
            "plan_id": plan.get("id") if plan else None,
            "plan": copy.deepcopy(plan) if plan else None,
            "service_id": service_id,
            "base_amount": base_amount,
            "discount_amount": 0,
            "discount_code": None,
            "discount_reserved": False,
            "amount": base_amount,
            "payment_method": None,
            "status": "draft",
            "created_at": _iso(),
        }
        STORE["orders"][oid] = order
    await _save_store()
    return order


async def _cancel_order(order_id: str, user_id: int):
    async with _state_lock:
        order = STORE["orders"].get(order_id)
        if not order or order.get("user_id") != user_id or order.get("status") != "draft":
            return False
        order["status"] = "cancelled"
        order["cancelled_at"] = _iso()
    await _save_store()
    return True


def _discount_valid(code_entry: dict, user_id: int):
    if not code_entry or not code_entry.get("active", True) or _expired(code_entry.get("expires_at")):
        return False, "کد غیرفعال یا منقضی شده است."
    if int(code_entry.get("uses", 0)) >= int(code_entry.get("max_uses", 1)):
        return False, "ظرفیت استفاده از این کد تمام شده است."
    if str(user_id) in [str(x) for x in code_entry.get("used_by", [])]:
        return False, "این کد قبلاً توسط شما استفاده شده است."
    return True, ""

def _calculate_payable(base_amount: object, code_entry: dict | None) -> tuple[int, int]:
    """The only commerce price calculation: non-negative base, discount, and payable."""
    base = max(0, int(base_amount or 0))
    if not code_entry:
        return base, 0
    value = max(0, int(code_entry.get("value", 0) or 0))
    if code_entry.get("type") == "percent":
        discount = (base * min(100, value)) // 100
    else:
        discount = min(base, value)
    discount = min(base, max(0, discount))
    return base - discount, discount


async def _apply_discount(order_id: str, user_id: int, code_text: str):
    code = _normalize_code(code_text)
    if not code:
        return None, "فرمت کد تخفیف معتبر نیست."
    async with _state_lock:
        order = STORE["orders"].get(order_id)
        if not order or order.get("user_id") != user_id or order.get("status") != "draft" or order.get("kind") == "wallet":
            return None, "این سفارش برای تخفیف معتبر نیست."
        entry = STORE["discount_codes"].get(code)
        ok, error = _discount_valid(entry, user_id)
        if not ok:
            return None, error
        amount, discount = _calculate_payable(order.get("base_amount", 0), entry)
        order["discount_code"] = code
        order["discount_amount"] = discount
        order["amount"] = amount
    await _save_store()
    return STORE["orders"][order_id], None


def _reserve_discount_locked(order: dict):
    code = order.get("discount_code")
    if not code or order.get("discount_reserved"):
        return True, ""
    entry = STORE["discount_codes"].get(code)
    ok, error = _discount_valid(entry, order["user_id"])
    if not ok:
        return False, error
    entry["uses"] = int(entry.get("uses", 0)) + 1
    entry.setdefault("used_by", []).append(str(order["user_id"]))
    entry.setdefault("orders", []).append(order["id"])
    order["discount_reserved"] = True
    return True, ""


def _release_discount_locked(order: dict):
    code = order.get("discount_code")
    if not code or not order.get("discount_reserved"):
        return
    entry = STORE["discount_codes"].get(code)
    if entry:
        entry["uses"] = max(0, int(entry.get("uses", 0)) - 1)
        uid = str(order["user_id"])
        if uid in entry.get("used_by", []):
            entry["used_by"].remove(uid)
        if order["id"] in entry.get("orders", []):
            entry["orders"].remove(order["id"])
    order["discount_reserved"] = False


async def _cancel_waiting_order(order_id: str, user_id: int, reason="cancelled"):
    """Cancel an unpaid card order and release any reserved promo exactly once."""
    changed = False
    async with _state_lock:
        order = STORE["orders"].get(order_id)
        if (
            order
            and int(order.get("user_id", 0)) == int(user_id)
            and order.get("status") == "awaiting_receipt"
        ):
            _release_discount_locked(order)
            order["status"] = reason
            order["cancelled_at"] = _iso()
            changed = True
    if changed:
        await _save_store()
    return changed


async def _expire_stale_orders():
    """Free promo reservations for abandoned receipt flows."""
    cutoff = _now() - timedelta(hours=RECEIPT_TIMEOUT_HOURS)
    changed = 0
    async with _state_lock:
        for order in STORE["orders"].values():
            if order.get("status") != "awaiting_receipt":
                continue
            try:
                started = datetime.fromisoformat(order.get("payment_started_at") or order.get("created_at"))
            except Exception:
                started = datetime.min
            if started <= cutoff:
                _release_discount_locked(order)
                order["status"] = "expired"
                order["cancelled_at"] = _iso()
                changed += 1
    if changed:
        await _save_store()
    return changed


async def _start_card_payment(order_id: str, user_id: int):
    await _expire_stale_orders()
    if not ADMIN_IDS:
        return None, "ادمین پرداخت در سیستم تنظیم نشده است."
    settings = STORE.get("settings", {})
    if not settings.get("card_number") or not settings.get("card_holder"):
        return None, "اطلاعات کارت هنوز توسط ادمین تنظیم نشده است."
    async with _state_lock:
        order = STORE["orders"].get(order_id)
        if not order or order.get("user_id") != user_id or order.get("status") != "draft":
            return None, "سفارش معتبر نیست یا قبلاً پرداخت شده است."
        if int(order.get("amount", 0)) <= 0:
            return None, "این سفارش نیاز به پرداخت ندارد؛ فعال‌سازی رایگان را انتخاب کن."
        ok, error = _reserve_discount_locked(order)
        if not ok:
            return None, error
        order["payment_method"] = "card"
        order["status"] = "awaiting_receipt"
        order["payment_started_at"] = _iso()
    await _save_store()
    return STORE["orders"][order_id], None


async def _submit_receipt(user_id: int, receipt: dict, order_id: str | None = None):
    async with _state_lock:
        if order_id:
            order = STORE["orders"].get(order_id)
        else:
            candidates = [
                o for o in STORE["orders"].values()
                if o.get("user_id") == user_id and o.get("status") == "awaiting_receipt"
            ]
            candidates.sort(key=lambda o: o.get("payment_started_at", ""), reverse=True)
            order = candidates[0] if candidates else None
        if not order or order.get("user_id") != user_id or order.get("status") != "awaiting_receipt":
            return None, "سفارش منتظر رسید پیدا نشد."
        order["receipt"] = receipt
        order["receipt_at"] = _iso()
        order["status"] = "pending_admin"
    await _save_store()
    return order, None


async def _ensure_customer_sub(user_id: int):
    user_key = str(user_id)
    async with _state_lock:
        user = STORE["users"].get(user_key, {})
        sub_id = user.get("sub_id")
    if sub_id and sub_id in SUBS:
        return sub_id
    sub_id, _ = await create_sub_group(name=f"سرویس‌های کاربر {user_id}")
    async with _state_lock:
        if user_key in STORE["users"]:
            STORE["users"][user_key]["sub_id"] = sub_id
    await _save_store()
    return sub_id


async def _provision_new(order: dict):
    plan = order["plan"]
    user_id = int(order["user_id"])
    user = STORE["users"].get(str(user_id), {})
    expires_at = (_now() + timedelta(days=int(plan["days"]))).isoformat()
    label_name = user.get("first_name") or str(user_id)
    sub_id = await _ensure_customer_sub(user_id)
    uid, _ = await make_link(
        label=f"{label_name} · {plan['quota_label']}/{plan['days']}روز",
        limit_bytes=int(plan["quota_bytes"]),
        expires_at=expires_at,
        note=f"فروش خودکار تلگرام · سفارش {order['id']}",
        sub_id=sub_id,
        protocol="vless-ws",
        fingerprint=DEFAULT_FINGERPRINT,
        alpn="http/1.1",
        port=DEFAULT_PORT,
        ip_limit=STORE_IP_LIMIT,
        speed_limit_bytes=0,
    )
    async with LINKS_LOCK:
        link = LINKS[uid]
        link.update({
            "owner_telegram_id": user_id,
            "store_order_id": order["id"],
            "store_plan_id": plan["id"],
            "store_managed": True,
        })
    await set_link_sub(uid, sub_id)
    await save_state()
    return uid


async def _provision_renew(order: dict):
    uid = order.get("service_id")
    plan = order["plan"]
    user_id = int(order["user_id"])
    async with LINKS_LOCK:
        link = LINKS.get(uid)
        if not link or str(link.get("owner_telegram_id", "")) != str(user_id):
            raise ValueError("service not found or ownership mismatch")
        old_limit = int(link.get("limit_bytes", 0) or 0)
        old_used = int(link.get("used_bytes", 0) or 0)
        remaining = max(0, old_limit - old_used)
        try:
            old_exp = datetime.fromisoformat(link.get("expires_at")) if link.get("expires_at") else _now()
        except Exception:
            old_exp = _now()
        base = max(_now(), old_exp)
        link.update({
            "limit_bytes": remaining + int(plan["quota_bytes"]),
            "used_bytes": 0,
            "expires_at": (base + timedelta(days=int(plan["days"]))).isoformat(),
            "active": True,
            "protocol": "vless-ws",
            "fingerprint": DEFAULT_FINGERPRINT,
            "alpn": "http/1.1",
            "port": DEFAULT_PORT,
            "store_order_id": order["id"],
            "store_plan_id": plan["id"],
            "store_managed": True,
        })
    await save_state()
    return uid


async def _finish_order(order_id: str, reviewer_id: int | None, source: str):
    async with _provision_lock:
        async with _state_lock:
            order = STORE["orders"].get(order_id)
            allowed = {"pending_admin"} if source == "card" else {"draft"}
            if not order:
                return None, "سفارش پیدا نشد."
            if order.get("status") == "approved":
                return order, "قبلاً تایید شده است."
            if order.get("status") not in allowed:
                return None, "وضعیت سفارش اجازه تایید نمی‌دهد."
            user = STORE["users"].get(str(order["user_id"]))
            if not user:
                return None, "کاربر پیدا نشد."
            is_trial = bool((order.get("plan") or {}).get("trial"))
            if is_trial and user.get("trial_used_at"):
                return None, "پلن تستی قبلاً برای این حساب استفاده شده است."
            if source == "free" and int(order.get("amount", 0)) != 0:
                return None, "فعال‌سازی رایگان برای این سفارش معتبر نیست."
            if source == "card" and int(order.get("amount", 0)) <= 0:
                return None, "تایید پرداخت برای سفارش رایگان معتبر نیست."
            if source in {"wallet", "free"}:
                ok, error = _reserve_discount_locked(order)
                if not ok:
                    return None, error
                if source == "wallet":
                    if int(user.get("balance", 0)) < int(order.get("amount", 0)):
                        _release_discount_locked(order)
                        return None, "موجودی کیف پول کافی نیست."
                    user["balance"] = int(user.get("balance", 0)) - int(order["amount"])
                    order["payment_method"] = "wallet"
                else:
                    order["payment_method"] = "free"
            order["status"] = "processing"
            order["reviewer_id"] = reviewer_id
            order["processing_at"] = _iso()
        await _save_store()

        service_id = None
        try:
            if order["kind"] == "wallet":
                async with _state_lock:
                    user = STORE["users"][str(order["user_id"])]
                    user["balance"] = int(user.get("balance", 0)) + int(order["amount"])
            elif order["kind"] == "new":
                service_id = await _provision_new(order)
            elif order["kind"] == "renew":
                service_id = await _provision_renew(order)
            else:
                raise ValueError("unknown order kind")
        except Exception as exc:
            async with _state_lock:
                current = STORE["orders"][order_id]
                if source == "wallet":
                    user = STORE["users"][str(current["user_id"])]
                    user["balance"] = int(user.get("balance", 0)) + int(current["amount"])
                    _release_discount_locked(current)
                    current["status"] = "draft"
                elif source == "free":
                    _release_discount_locked(current)
                    current["status"] = "draft"
                else:
                    current["status"] = "pending_admin"
                current["last_error"] = str(exc)[:300]
            await _save_store()
            logger.exception("Order provisioning failed: %s", order_id)
            return None, "ساخت یا تمدید سرویس ناموفق بود؛ مبلغ کیف پول برگشت داده شد."

        async with _state_lock:
            current = STORE["orders"][order_id]
            current["status"] = "approved"
            current["approved_at"] = _iso()
            if (current.get("plan") or {}).get("trial"):
                STORE["users"][str(current["user_id"])]["trial_used_at"] = current["approved_at"]
            current["service_id"] = service_id or current.get("service_id")
        await _save_store()
        return STORE["orders"][order_id], None


async def _reject_order(order_id: str, admin_id: int):
    async with _state_lock:
        order = STORE["orders"].get(order_id)
        if not order or order.get("status") != "pending_admin":
            return None, "سفارش در انتظار تایید نیست."
        _release_discount_locked(order)
        order["status"] = "rejected"
        order["reviewer_id"] = admin_id
        order["rejected_at"] = _iso()
    await _save_store()
    return order, None


async def _redeem_gift(user_id: int, text: str):
    code = _normalize_code(text)
    if not code:
        return None, "فرمت کد هدیه معتبر نیست."
    async with _state_lock:
        user = STORE["users"].get(str(user_id))
        entry = STORE["gift_codes"].get(code)
        if not user or not entry or not entry.get("active", True) or _expired(entry.get("expires_at")):
            return None, "کد هدیه نامعتبر یا منقضی است."
        if str(user_id) in [str(x) for x in entry.get("used_by", [])]:
            return None, "این کد را قبلاً استفاده کرده‌اید."
        if len(entry.get("used_by", [])) >= int(entry.get("max_uses", 1)):
            return None, "ظرفیت این کد هدیه تمام شده است."
        amount = int(entry.get("amount", 0))
        if amount <= 0:
            return None, "مبلغ کد هدیه نامعتبر است."
        entry.setdefault("used_by", []).append(str(user_id))
        user["balance"] = int(user.get("balance", 0)) + amount
        user.setdefault("gift_codes", []).append(code)
        STORE.setdefault("redemption_audit", []).append({"kind": "gift", "code": code, "user_id": int(user_id), "amount": amount, "at": _iso()})
        STORE["redemption_audit"] = STORE["redemption_audit"][-5000:]
    await _save_store()
    return amount, None


# ── Message flow ─────────────────────────────────────────────────────────────
async def _show_menu(chat_id: int, user: dict, message_id: int | None = None):
    text = _main_text(user)
    if message_id:
        await _edit(chat_id, message_id, text, _main_kb(chat_id))
    else:
        await _send(chat_id, text, _main_kb(chat_id))


async def _handle_receipt_message(chat_id: int, msg: dict):
    receipt = None
    if msg.get("photo"):
        photo = msg["photo"][-1]
        receipt = {"type": "photo", "file_id": photo.get("file_id"), "file_unique_id": photo.get("file_unique_id")}
    elif msg.get("document"):
        doc = msg["document"]
        mime = str(doc.get("mime_type") or "")
        if not (mime.startswith("image/") or mime == "application/pdf"):
            return False
        receipt = {"type": "document", "file_id": doc.get("file_id"), "file_unique_id": doc.get("file_unique_id"), "mime_type": mime}
    if not receipt:
        return False
    pending = _pending.get(chat_id) or {}
    order_id = pending.get("order_id") if pending.get("action") == "receipt" else None
    order, error = await _submit_receipt(chat_id, receipt, order_id)
    if error:
        await _send(chat_id, f"❗️ {error}", _main_kb(chat_id))
        return True
    _pending.pop(chat_id, None)
    await _send(
        chat_id,
        f"✅ رسید سفارش <code>{order['id']}</code> دریافت شد. پس از بررسی ادمین نتیجه برایت ارسال می‌شود.",
        _main_kb(chat_id),
    )
    for admin_id in ADMIN_IDS:
        await _send_receipt(admin_id, order)
    return True


async def _handle_pending_text(chat_id: int, text: str, pending: dict):
    action = pending.get("action")
    # Keep each admin wizard in one implementation; the former duplicated branch
    # was unreachable through the normal dispatcher and could reference stale data.
    if action.startswith("gift_admin_"):
        return await _admin_gift_text(chat_id, text, pending)
    if action.startswith("promo_admin_"):
        return await _admin_promo_text(chat_id, text, pending)
    if text in ("لغو", "/cancel"):
        if action == "receipt" and pending.get("order_id"):
            await _cancel_waiting_order(pending["order_id"], chat_id)
        _pending.pop(chat_id, None)
        await _send(chat_id, "عملیات لغو شد.", _main_kb(chat_id))
        return True

    if action == "discount":
        order, error = await _apply_discount(pending["order_id"], chat_id, text)
        if error:
            await _send(chat_id, f"❗️ {error}\nکد دیگری بفرست یا /cancel را ارسال کن.")
            return True
        _pending.pop(chat_id, None)
        user = STORE["users"][str(chat_id)]
        await _send(chat_id, _order_text(order), _order_pay_kb(order, user.get("balance", 0)))
        return True

    if action == "gift_redeem":
        amount, error = await _redeem_gift(chat_id, text)
        if error:
            await _send(chat_id, f"❗️ {error}\nکد دیگری بفرست یا /cancel را ارسال کن.")
            return True
        _pending.pop(chat_id, None)
        balance = STORE["users"][str(chat_id)].get("balance", 0)
        await _send(chat_id, f"🎉 کیف پولت {_money(amount)} شارژ شد.\nموجودی جدید: <b>{_money(balance)}</b>", _main_kb(chat_id))
        return True

    if action == "topup_amount":
        amount = _parse_int(text, MIN_TOPUP, MAX_TOPUP)
        if amount is None:
            await _send(chat_id, f"مبلغی بین {_money(MIN_TOPUP)} تا {_money(MAX_TOPUP)} بفرست یا /cancel را ارسال کن.")
            return True
        order = await _create_order(chat_id, "wallet", amount=amount)
        _pending.pop(chat_id, None)
        paid, error = await _start_card_payment(order["id"], chat_id)
        if error:
            await _send(chat_id, f"❗️ {error}", _main_kb(chat_id))
            return True
        _pending[chat_id] = {"action": "receipt", "order_id": paid["id"]}
        await _send(chat_id, _card_instructions(paid), {"inline_keyboard": [[{"text": "❌ لغو", "callback_data": f"cancelpay:{paid['id']}"}]]})
        return True

    if action == "card_number" and _is_admin(chat_id):
        digits = re.sub(r"\D", "", text.translate(_FA_DIGITS))
        if len(digits) != 16:
            await _send(chat_id, "شماره کارت باید دقیقاً ۱۶ رقم باشد. دوباره بفرست یا /cancel را ارسال کن.")
            return True
        _pending[chat_id] = {"action": "card_holder", "card_number": digits}
        await _send(chat_id, "نام صاحب کارت را بفرست:")
        return True

    if action == "card_holder" and _is_admin(chat_id):
        holder = text.strip()[:80]
        if len(holder) < 3:
            await _send(chat_id, "نام صاحب کارت معتبر نیست. دوباره بفرست:")
            return True
        async with _state_lock:
            STORE["settings"]["card_number"] = pending["card_number"]
            STORE["settings"]["card_holder"] = holder
        await _save_store()
        _pending.pop(chat_id, None)
        await _send(chat_id, "✅ اطلاعات کارت ذخیره شد.", _admin_kb())
        return True


    return True


async def _admin_promo_text(chat_id: int, text: str, pending: dict):
    action = pending["action"]
    data = pending.setdefault("data", {})
    if action == "promo_admin_code":
        code = _normalize_code(text)
        if not code or code in STORE["discount_codes"]:
            await _send(chat_id, "کد باید ۳ تا ۳۲ کاراکتر انگلیسی/عدد و یکتا باشد. دوباره بفرست:")
            return True
        data["code"] = code
        _pending[chat_id] = {"action": "promo_admin_type", "data": data}
        await _send(chat_id, "نوع تخفیف را انتخاب کن:", {"inline_keyboard": [[
            {"text": "درصدی", "callback_data": "ptype:percent"},
            {"text": "مبلغ ثابت", "callback_data": "ptype:fixed"},
        ]]})
    elif action == "promo_admin_value":
        maximum = 100 if data.get("type") == "percent" else 20_000_000
        value = _parse_int(text, 1, maximum)
        if value is None:
            await _send(chat_id, "مقدار تخفیف نامعتبر است. دوباره بفرست:")
            return True
        data["value"] = value
        _pending[chat_id] = {"action": "promo_admin_uses", "data": data}
        await _send(chat_id, "حداکثر تعداد استفاده را بفرست:")
    elif action == "promo_admin_uses":
        value = _parse_int(text, 1, 100_000)
        if value is None:
            await _send(chat_id, "تعداد استفاده نامعتبر است. دوباره بفرست:")
            return True
        data["max_uses"] = value
        _pending[chat_id] = {"action": "promo_admin_days", "data": data}
        await _send(chat_id, "اعتبار کد چند روز باشد؟ عدد ۰ یعنی بدون انقضا:")
    elif action == "promo_admin_days":
        days = _parse_int(text, 0, 3650)
        if days is None:
            await _send(chat_id, "روز اعتبار نامعتبر است. دوباره بفرست:")
            return True
        expires = (_now() + timedelta(days=days)).isoformat() if days else None
        async with _state_lock:
            STORE["discount_codes"][data["code"]] = {
                "code": data["code"], "type": data["type"], "value": data["value"],
                "max_uses": data["max_uses"], "uses": 0, "used_by": [], "orders": [],
                "active": True, "created_at": _iso(), "created_by": chat_id, "expires_at": expires,
            }
        await _save_store()
        _pending.pop(chat_id, None)
        display = f"{data['value']}٪" if data["type"] == "percent" else _money(data["value"])
        await _send(chat_id, f"✅ کد تخفیف <code>{data['code']}</code> با مقدار {display} ساخته شد.", _admin_kb())
    return True


def _card_instructions(order: dict):
    settings = STORE["settings"]
    card = re.sub(r"\D", "", settings.get("card_number", ""))
    shown = "-".join(card[i:i+4] for i in range(0, len(card), 4))
    return (
        f"💳 <b>پرداخت کارت‌به‌کارت</b>\n\n"
        f"مبلغ دقیق: <b>{_money(order['amount'])}</b>\n"
        f"شماره کارت: <code>{shown}</code>\n"
        f"به نام: <b>{_e(settings.get('card_holder'))}</b>\n"
        f"کد سفارش: <code>{order['id']}</code>\n\n"
        "بعد از پرداخت، تصویر یا فایل PDF رسید را همین‌جا ارسال کن. "
        "سرویس فقط پس از تایید ادمین فعال می‌شود."
    )


async def _handle_message(msg: dict):
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    from_user = msg.get("from") or {}
    if chat_id is None or chat.get("type", "private") != "private":
        return
    chat_id = int(chat_id)
    user = await _ensure_user(from_user or {"id": chat_id})

    if await _handle_receipt_message(chat_id, msg):
        return

    text = str(msg.get("text") or "").strip()
    if text in ("/start", "/menu"):
        _pending.pop(chat_id, None)
        await _show_menu(chat_id, user)
        return
    if text == "/admin" and _is_admin(chat_id):
        _pending.pop(chat_id, None)
        await _send(chat_id, "🛠 <b>پنل مدیریت فروش</b>", _admin_kb())
        return

    if text.startswith("/"):
        await _send(chat_id, "دستور شناخته نشد. از منوی زیر استفاده کن.", _main_kb(chat_id))
        return

    pending = _pending.get(chat_id)
    if pending and text and await _handle_pending_text(chat_id, text, pending):
        return
    if pending and pending.get("action") == "receipt":
        await _send(chat_id, "لطفاً تصویر یا فایل PDF رسید را ارسال کن؛ برای لغو /cancel را بفرست.")
        return
    await _show_menu(chat_id, user)



def _assignment_targets() -> list[dict]:
    by_code = {}
    for record in proxy_repository.records_for_country():
        current = by_code.get(record.code)
        if current is None or (record.health, record.id) > (current.health, current.id):
            by_code[record.code] = record
    return [{"code": r.code, "country": r.country, "flag": r.flag} for r in sorted(by_code.values(), key=lambda r: (r.country, r.code))[:30]]


def _eligible_assignment_links() -> list[tuple[str, dict]]:
    return [(uid, link) for uid, link in LINKS.items() if link.get("store_managed") and str(link.get("owner_telegram_id", "")) in STORE["users"]]


async def _apply_proxy_assignment(admin_id: int, country_code: str) -> tuple[dict | None, str | None]:
    if not _is_admin(admin_id):
        return None, "دسترسی ادمین لازم است"
    target = next((x for x in _assignment_targets() if x["code"] == country_code), None)
    if not target:
        return None, "موقعیت انتخاب‌شده دیگر در دسترس نیست."
    records = proxy_repository.records_for_country(country_code)
    if not records:
        return None, "موقعیت انتخاب‌شده دیگر در دسترس نیست."
    record = max(records, key=lambda r: (r.health, r.id))
    changed, skipped = [], 0
    async with LINKS_LOCK:
        eligible = _eligible_assignment_links()
        for uid, link in eligible:
            if link.get("exit_proxy_mode") == "repository" and link.get("proxy_id") == record.id:
                skipped += 1
                continue
            changed.append((uid, link.get("exit_proxy_mode"), link.get("proxy_id"), link.get("custom_proxy")))
            link.update({"exit_proxy_mode": "repository", "proxy_id": record.id, "custom_proxy": ""})
    try:
        await save_state()
    except Exception:
        async with LINKS_LOCK:
            for uid, mode, proxy_id, custom_proxy in changed:
                if uid in LINKS:
                    LINKS[uid].update({"exit_proxy_mode": mode, "proxy_id": proxy_id, "custom_proxy": custom_proxy})
        return None, "ذخیره‌سازی انجام نشد؛ تغییری اعمال نشد."
    result = {"target": target, "eligible": len(eligible), "updated": len(changed), "skipped": skipped, "failed": 0}
    async with _state_lock:
        STORE.setdefault("admin_audit", []).append({"kind": "proxy_assignment", "admin_id": int(admin_id), "country_code": country_code, **{k: result[k] for k in ("eligible", "updated", "skipped", "failed")}, "at": _iso()})
        STORE["admin_audit"] = STORE["admin_audit"][-1000:]
    await _save_store()
    return result, None

# ── Callback flow ────────────────────────────────────────────────────────────
async def _handle_callback(cb: dict):
    message = cb.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    callback_id = cb.get("id", "")
    data = str(cb.get("data") or "")
    from_user = cb.get("from") or {}
    if chat_id is None or message.get("chat", {}).get("type", "private") != "private":
        await _answer(callback_id, "این عمل فقط در چت خصوصی در دسترس است", True)
        return
    chat_id = int(chat_id)
    if int((from_user or {}).get("id") or 0) != chat_id:
        await _answer(callback_id, "این دکمه برای حساب دیگری است", True)
        return
    if not data or len(data.encode("utf-8")) > 64:
        await _answer(callback_id, "درخواست نامعتبر است", True)
        return
    user = await _ensure_user(from_user)
    await _answer(callback_id)

    if data == "menu":
        _pending.pop(chat_id, None)
        await _show_menu(chat_id, user, message_id)
        return
    if data == "plans":
        await _edit(chat_id, message_id, _plans_text(user=user), _plans_kb(user=user))
        return
    if data.startswith("buy:"):
        plan = _active_plan(data.split(":", 1)[1])
        if plan and plan.get("trial") and user.get("trial_used_at"):
            await _edit(chat_id, message_id, "پلن تستی قبلاً برای این حساب استفاده شده است.", _main_kb(chat_id))
            return
        if not plan:
            await _edit(chat_id, message_id, "این پلن فعال نیست.", _main_kb(chat_id))
            return
        order = await _create_order(chat_id, "new", plan)
        await _edit(chat_id, message_id, _order_text(order), _order_pay_kb(order, user.get("balance", 0)))
        return
    if data.startswith("disc:"):
        oid = data.split(":", 1)[1]
        order = STORE["orders"].get(oid)
        if not order or order.get("user_id") != chat_id or order.get("status") != "draft":
            await _answer(callback_id, "سفارش معتبر نیست", True)
            return
        _pending[chat_id] = {"action": "discount", "order_id": oid}
        await _edit(chat_id, message_id, "🏷 کد تخفیف را بفرست؛ برای انصراف /cancel را ارسال کن.")
        return
    if data.startswith("payw:"):
        oid = data.split(":", 1)[1]
        order = STORE["orders"].get(oid)
        if not order or order.get("user_id") != chat_id:
            await _answer(callback_id, "سفارش متعلق به شما نیست", True)
            return
        done, error = await _finish_order(oid, None, "wallet")
        if error:
            await _edit(chat_id, message_id, f"❗️ {error}", _main_kb(chat_id))
            return
        await _edit(chat_id, message_id, "✅ پرداخت از کیف پول انجام شد و سرویس آماده است.", _main_kb(chat_id))
        return
    if data.startswith("free:"):
        oid = data.split(":", 1)[1]
        order = STORE["orders"].get(oid)
        if not order or order.get("user_id") != chat_id:
            await _answer(callback_id, "سفارش متعلق به شما نیست", True)
            return
        done, error = await _finish_order(oid, None, "free")
        if error:
            await _edit(chat_id, message_id, f"❗️ {error}", _main_kb(chat_id))
            return
        await _edit(chat_id, message_id, "✅ سرویس رایگان با موفقیت فعال شد.", _main_kb(chat_id))
        return
    if data.startswith("payc:"):
        oid = data.split(":", 1)[1]
        order, error = await _start_card_payment(oid, chat_id)
        if error:
            await _edit(chat_id, message_id, f"❗️ {error}", _main_kb(chat_id))
            return
        _pending[chat_id] = {"action": "receipt", "order_id": oid}
        await _edit(chat_id, message_id, _card_instructions(order), {"inline_keyboard": [[{"text": "❌ لغو پرداخت", "callback_data": f"cancelpay:{oid}"}]]})
        return
    if data.startswith("cancel:"):
        oid = data.split(":", 1)[1]
        await _cancel_order(oid, chat_id)
        await _edit(chat_id, message_id, "سفارش لغو شد.", _main_kb(chat_id))
        return
    if data.startswith("cancelpay:"):
        oid = data.split(":", 1)[1]
        await _cancel_waiting_order(oid, chat_id)
        _pending.pop(chat_id, None)
        await _edit(chat_id, message_id, "پرداخت لغو شد.", _main_kb(chat_id))
        return

    if data == "services":
        services = _user_services(chat_id)
        if not services:
            await _edit(chat_id, message_id, "هنوز سرویسی نداری. از بخش خرید سرویس شروع کن.", _main_kb(chat_id))
            return
        sub_id = user.get("sub_id")
        combined = ""
        if sub_id and sub_id in SUBS:
            combined_url = "https:" + "//" + get_host() + "/sub-group/" + SUBS[sub_id]["uuid_key"]
            combined = f"\n\n🔗 اشتراک همه سرویس‌ها:\n<code>{_e(combined_url)}</code>"
        await _edit(chat_id, message_id, f"📦 <b>سرویس‌های من</b>{combined}\n\nیک سرویس را انتخاب کن:", _service_rows(chat_id))
        return
    if data.startswith("svc:"):
        uid = data.split(":", 1)[1]
        link = LINKS.get(uid)
        if not link or str(link.get("owner_telegram_id", "")) != str(chat_id):
            await _answer(callback_id, "سرویس پیدا نشد", True)
            return
        await _edit(chat_id, message_id, _service_text(uid, link), _service_kb(uid))
        return
    if data.startswith("cfg:"):
        uid = data.split(":", 1)[1]
        link = LINKS.get(uid)
        if not link or str(link.get("owner_telegram_id", "")) != str(chat_id):
            await _answer(callback_id, "سرویس پیدا نشد", True)
            return
        config = vless_link_for_link(link, uid, get_host())
        await _send(chat_id, f"🧾 کانفیگ آماده؛ فقط کپی و Import کن:\n\n<code>{_e(config)}</code>", _service_kb(uid))
        return
    if data.startswith("sub:"):
        uid = data.split(":", 1)[1]
        link = LINKS.get(uid)
        if not link or str(link.get("owner_telegram_id", "")) != str(chat_id):
            await _answer(callback_id, "سرویس پیدا نشد", True)
            return
        sub_url = "https:" + "//" + get_host() + "/sub/" + uid
        await _send(chat_id, f"🔗 لینک اشتراک این سرویس:\n\n<code>{_e(sub_url)}</code>\n\nآن را در بخش Subscription برنامه وارد کن.", _service_kb(uid))
        return

    if data == "renew":
        services = _user_services(chat_id)
        if not services:
            await _edit(chat_id, message_id, "سرویسی برای تمدید وجود ندارد.", _main_kb(chat_id))
            return
        await _edit(chat_id, message_id, "🔄 سرویس موردنظر برای تمدید را انتخاب کن:", _service_rows(chat_id, "ren"))
        return
    if data.startswith("ren:"):
        uid = data.split(":", 1)[1]
        link = LINKS.get(uid)
        if not link or str(link.get("owner_telegram_id", "")) != str(chat_id):
            await _answer(callback_id, "سرویس پیدا نشد", True)
            return
        await _edit(chat_id, message_id, _plans_text(f"تمدید {_e(link.get('label'))}", include_trial=False), _plans_kb("rplan", uid))
        return
    if data.startswith("rplan:"):
        try:
            _, uid, plan_id = data.split(":", 2)
        except ValueError:
            await _answer(callback_id, "انتخاب نامعتبر است", True)
            return
        link, plan = LINKS.get(uid), _active_plan(plan_id)
        if not link or str(link.get("owner_telegram_id", "")) != str(chat_id) or not plan or plan.get("trial"):
            await _answer(callback_id, "انتخاب نامعتبر است", True)
            return
        order = await _create_order(chat_id, "renew", plan, service_id=uid)
        await _edit(chat_id, message_id, _order_text(order), _order_pay_kb(order, user.get("balance", 0)))
        return

    if data == "wallet":
        await _edit(chat_id, message_id, f"💰 <b>کیف پول</b>\n\nموجودی: <b>{_money(user.get('balance', 0))}</b>", _wallet_kb())
        return
    if data == "topup":
        _pending[chat_id] = {"action": "topup_amount"}
        await _edit(chat_id, message_id, f"مبلغ شارژ را به تومان بفرست.\nحداقل: {_money(MIN_TOPUP)}\nبرای انصراف /cancel را ارسال کن.")
        return
    if data == "gift":
        _pending[chat_id] = {"action": "gift_redeem"}
        await _edit(chat_id, message_id, "🎁 کد هدیه را ارسال کن؛ برای انصراف /cancel را بفرست.")
        return
    if data == "guide":
        support = STORE["settings"].get("support")
        support_line = f"\n\nپشتیبانی: @{_e(support)}" if support else ""
        text = (
            "📖 <b>راهنمای اتصال</b>\n\n"
            "1️⃣ از «سرویس‌های من» کانفیگ تکی یا لینک اشتراک را کپی کن.\n"
            "2️⃣ در Android داخل v2rayNG گزینه Import from clipboard یا Subscription را بزن.\n"
            "3️⃣ در iOS داخل Streisand/Shadowrocket گزینه Import یا Add Subscription را بزن.\n"
            "4️⃣ در Windows داخل Nekoray/Hiddify گزینه Import from clipboard را انتخاب کن.\n\n"
            "پروتکل، TLS، دامنه، پورت و مسیر WS از قبل داخل لینک قرار دارند؛ چیزی را دستی تغییر نده."
            + support_line
        )
        await _edit(chat_id, message_id, text, _back_main_kb(chat_id))
        return

    # Admin callbacks
    if data.startswith(("admin", "ap:", "aord:", "areceipt:", "aok:", "ano:", "agift", "apromo", "ptype:", "acard", "aplans", "astats", "assign", "asgn:", "asgnok:", "asgncancel")):
        if not _is_admin(chat_id):
            await _answer(callback_id, "دسترسی ادمین لازم است", True)
            return
    if data == "admin":
        await _edit(chat_id, message_id, "🛠 <b>پنل مدیریت فروش</b>", _admin_kb())
        return
    if data == "assign":
        targets = _assignment_targets()
        if not targets:
            await _edit(chat_id, message_id, "موقعیت قابل تخصیصی در حال حاضر وجود ندارد.", _admin_kb())
            return
        rows = [[{"text": f"{x['flag']} {x['country']}", "callback_data": f"asgn:{x['code']}"}] for x in targets]
        rows.append([{"text": "⬅ پنل مدیریت", "callback_data": "admin"}])
        await _edit(chat_id, message_id, "🌍 <b>تخصیص پراکسی</b>\n\nیک موقعیت پشتیبانی‌شده را انتخاب کن:", {"inline_keyboard": rows})
        return
    if data.startswith("asgn:"):
        code = data.split(":", 1)[1].upper()
        target = next((x for x in _assignment_targets() if x["code"] == code), None)
        if not target:
            await _answer(callback_id, "موقعیت نامعتبر است", True)
            return
        eligible = _eligible_assignment_links()
        _pending[chat_id] = {"action": "proxy_assignment", "code": code}
        kb = {"inline_keyboard": [[{"text": "✅ تایید تخصیص", "callback_data": f"asgnok:{code}"}, {"text": "❌ لغو", "callback_data": "asgncancel"}], [{"text": "⬅ انتخاب موقعیت", "callback_data": "assign"}]]}
        await _edit(chat_id, message_id, f"🌍 <b>تایید تخصیص</b>\n\nمقصد: {target['flag']} {_e(target['country'])}\nکاربران دارای سرویس: <b>{len({str(link.get('owner_telegram_id')) for _, link in eligible}):,}</b>\nسرویس‌های واجد شرایط: <b>{len(eligible):,}</b>\n\nاین تغییر پس از تایید اعمال می‌شود.", kb)
        return
    if data == "asgncancel":
        _pending.pop(chat_id, None)
        await _edit(chat_id, message_id, "تخصیص لغو شد.", _admin_kb())
        return
    if data.startswith("asgnok:"):
        code = data.split(":", 1)[1].upper()
        pending = _pending.get(chat_id) or {}
        if pending.get("action") != "proxy_assignment" or pending.get("code") != code:
            await _answer(callback_id, "تایید منقضی شده است", True)
            return
        result, error = await _apply_proxy_assignment(chat_id, code)
        _pending.pop(chat_id, None)
        if error:
            await _edit(chat_id, message_id, f"❗️ {error}", _admin_kb())
            return
        target = result["target"]
        await _edit(chat_id, message_id, f"✅ <b>تخصیص انجام شد</b>\n\nمقصد: {target['flag']} {_e(target['country'])}\nواجد شرایط: {result['eligible']:,}\nبه‌روزرسانی‌شده: {result['updated']:,}\nردشده (همان مقصد): {result['skipped']:,}\nناموفق: {result['failed']:,}", _admin_kb())
        return
    if data.startswith("ap:"):
        try:
            page = max(0, int(data.split(":", 1)[1] or 0))
        except ValueError:
            await _answer(callback_id, "صفحه نامعتبر است", True)
            return
        count = sum(1 for o in STORE["orders"].values() if o.get("status") == "pending_admin")
        await _edit(chat_id, message_id, f"🧾 پرداخت‌های در انتظار: <b>{count}</b>", _pending_orders_kb(page))
        return
    if data.startswith("aord:"):
        oid = data.split(":", 1)[1]
        order = STORE["orders"].get(oid)
        if not order:
            await _answer(callback_id, "سفارش پیدا نشد", True)
            return
        await _edit(chat_id, message_id, _admin_order_text(order), {"inline_keyboard": [
            [{"text": "🖼 نمایش رسید", "callback_data": f"areceipt:{oid}"}],
            *_admin_review_kb(oid)["inline_keyboard"],
            [{"text": "⬅ پرداخت‌ها", "callback_data": "ap:0"}],
        ]})
        return
    if data.startswith("areceipt:"):
        oid = data.split(":", 1)[1]
        order = STORE["orders"].get(oid)
        if order:
            await _send_receipt(chat_id, order)
        return
    if data.startswith("aok:"):
        oid = data.split(":", 1)[1]
        order, error = await _finish_order(oid, chat_id, "card")
        if error:
            if order:
                await _edit(chat_id, message_id, f"ℹ️ سفارش <code>{oid}</code> قبلاً تایید شده است.", _admin_kb())
            else:
                await _edit(chat_id, message_id, f"❗️ {_e(error)}", _admin_kb())
            return
        await _edit(chat_id, message_id, f"✅ سفارش <code>{oid}</code> تایید و اعمال شد.", _admin_kb())
        if order:
            await _send(order["user_id"], f"✅ پرداخت سفارش <code>{oid}</code> تایید شد. سرویس یا موجودی کیف پول آماده استفاده است.", _main_kb(order["user_id"]))
        return
    if data.startswith("ano:"):
        oid = data.split(":", 1)[1]
        order, error = await _reject_order(oid, chat_id)
        if error:
            await _answer(callback_id, error, True)
            return
        await _edit(chat_id, message_id, f"❌ سفارش <code>{oid}</code> رد شد.", _admin_kb())
        await _send(order["user_id"], f"❌ رسید سفارش <code>{oid}</code> تایید نشد. در صورت نیاز با پشتیبانی تماس بگیر.", _main_kb(order["user_id"]))
        return
    if data == "agift":
        _pending[chat_id] = {"action": "gift_admin_code", "data": {}}
        await _edit(chat_id, message_id, "🎁 کد هدیه جدید را بفرست؛ مثال: <code>WELCOME50</code>\nبرای لغو /cancel را ارسال کن.")
        return
    if data == "apromo":
        _pending[chat_id] = {"action": "promo_admin_code", "data": {}}
        await _edit(chat_id, message_id, "🏷 کد تخفیف جدید را بفرست؛ مثال: <code>OFF20</code>\nبرای لغو /cancel را ارسال کن.")
        return
    if data.startswith("ptype:"):
        pending = _pending.get(chat_id) or {}
        if pending.get("action") != "promo_admin_type":
            await _answer(callback_id, "عملیات منقضی شده", True)
            return
        typ = data.split(":", 1)[1]
        if typ not in ("percent", "fixed"):
            return
        info = pending["data"]
        info["type"] = typ
        _pending[chat_id] = {"action": "promo_admin_value", "data": info}
        prompt = "درصد تخفیف (۱ تا ۱۰۰) را بفرست:" if typ == "percent" else "مبلغ ثابت تخفیف به تومان را بفرست:"
        await _edit(chat_id, message_id, prompt)
        return
    if data == "acard":
        _pending[chat_id] = {"action": "card_number"}
        await _edit(chat_id, message_id, "💳 شماره کارت ۱۶ رقمی را بدون فاصله بفرست؛ برای لغو /cancel را ارسال کن.")
        return
    if data == "aplans":
        await _edit(chat_id, message_id, _plans_text("فهرست قیمت فعلی"), _admin_kb())
        return
    if data == "astats":
        orders = STORE["orders"].values()
        approved = [o for o in orders if o.get("status") == "approved"]
        revenue = sum(int(o.get("amount", 0)) for o in approved if o.get("payment_method") == "card" and o.get("kind") != "wallet")
        wallet_topups = sum(int(o.get("amount", 0)) for o in approved if o.get("kind") == "wallet")
        pending_count = sum(1 for o in STORE["orders"].values() if o.get("status") == "pending_admin")
        managed = sum(1 for l in LINKS.values() if l.get("store_managed"))
        await _edit(chat_id, message_id, (
            "📊 <b>آمار فروشگاه</b>\n\n"
            f"کاربران: {len(STORE['users']):,}\n"
            f"سرویس‌های فروخته‌شده: {managed:,}\n"
            f"سفارش‌های تاییدشده: {len(approved):,}\n"
            f"پرداخت در انتظار: {pending_count:,}\n"
            f"فروش مستقیم کارت: {_money(revenue)}\n"
            f"شارژ کیف پول تاییدشده: {_money(wallet_topups)}"
        ), _admin_kb())
        return

    await _show_menu(chat_id, user, message_id)


# ── Polling lifecycle ────────────────────────────────────────────────────────
async def _poll_loop():
    offset = max(0, int(STORE.get("poll_offset", 0) or 0))
    logger.info("Telegram sales bot polling started (admins: %s)", len(ADMIN_IDS))
    while _running:
        try:
            result = await _call(
                "getUpdates", offset=offset, timeout=30,
                allowed_updates=["message", "callback_query"],
            )
            if not result or not result.get("ok"):
                await asyncio.sleep(3)
                continue
            for update in result.get("result", []):
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                STORE["poll_offset"] = offset
                try:
                    if update.get("message"):
                        await _handle_message(update["message"])
                    elif update.get("callback_query"):
                        await _handle_callback(update["callback_query"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Telegram update handler failed")
                finally:
                    # Offset persistence is independent of a malformed update; it prevents replay storms.
                    await _save_store()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Telegram poll failed")
            await asyncio.sleep(3)


async def start_bot():
    global _client, _poll_task, _running
    if not BOT_TOKEN:
        logger.info("Telegram bot disabled: TELEGRAM_BOT_TOKEN is empty")
        return
    if _running:
        return
    await _load_store()
    await _expire_stale_orders()
    _client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        headers={"User-Agent": "EMS11-Telegram-Store/1.0"},
    )
    _running = True
    _poll_task = asyncio.create_task(_poll_loop(), name="telegram-sales-bot")
    if not ADMIN_IDS:
        logger.warning("TELEGRAM_ADMIN_IDS تنظیم نشده؛ تایید پرداخت کارت‌به‌کارت غیرفعال است.")


async def stop_bot():
    global _running, _client, _poll_task
    _running = False
    if _poll_task:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
        _poll_task = None
    await _save_store()
    if _client:
        await _client.aclose()
        _client = None
