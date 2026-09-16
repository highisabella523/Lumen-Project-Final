from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]
main = (ROOT / 'main.py').read_text(encoding='utf-8')
relay = (ROOT / 'relay_vless.py').read_text(encoding='utf-8')
assert 'app.add_api_websocket_route("/ws/{uuid}", websocket_tunnel)' in main
assert 'relay_psiphon' not in main.lower()
assert 'psiphon' not in main.lower()
assert 'psiphon' not in relay.lower()
ast.parse(main)
ast.parse(relay)
print('WS-only contract: PASS')
