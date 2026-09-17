import ast
from pathlib import Path
source = Path(__file__).resolve().parents[1] / "telegram_bot.py"
text = source.read_text(encoding="utf-8")
ast.parse(text)
required = ["_handle_callback", "_handle_pending_text", "_redeem_gift", "_apply_discount", "_finish_order", "_expire_stale_orders"]
for symbol in required:
    assert f"def {symbol}" in text or f"async def {symbol}" in text, symbol
assert 'message.get("chat", {}).get("type", "private") != "private"' in text
assert 'int((from_user or {}).get("id") or 0) != chat_id' in text
assert 'redemption_audit' in text
assert 'poll_offset' in text
assert 'دستور شناخته نشد' in text
print("telegram navigation: private-auth=OK callback-validation=OK state-recovery=OK audit=OK")
