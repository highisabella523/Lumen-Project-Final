from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]
main = (ROOT / 'main.py').read_text(encoding='utf-8')
pages = (ROOT / 'pages.py').read_text(encoding='utf-8')

assert 'app.add_api_websocket_route("/ws/{uuid}", websocket_tunnel)' in main
assert 'from relay_psiphon' not in main.lower()
assert '/api/psiphon/' not in main.lower()
assert 'authRedirect(' in pages
assert 'authoritativeAuth===true' in pages
assert 'function dashboardNavigate' in pages
ast.parse(main)
ast.parse(pages)
print('routing + dashboard contract: PASS')
