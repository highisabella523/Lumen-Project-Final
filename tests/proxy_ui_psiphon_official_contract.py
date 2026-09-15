#!/usr/bin/env python3
from pathlib import Path
import ast, sys
r=Path(__file__).resolve().parents[1]
pages=(r/'pages.py').read_text(); main=(r/'main.py').read_text(); docker=(r/'Dockerfile').read_text(); cfg=(r/'psiphon/config.py').read_text(); mgr=(r/'psiphon/manager.py').read_text(); relay=(r/'relay_vless.py').read_text(); psi=(r/'relay_psiphon.py').read_text()
checks={
'original selected-only UI': all(x in pages for x in ['id="nl-proxy-test"', "testSelectedProxy('nl')", 'No recorded test for this exact proxy']) and 'id="proxy-test-all-btn"' not in pages and 'id="proxy-performance-list"' not in pages,
'no dashboard test hydration': 'managedProxyTestResults={};' in pages and 'managedProxyTestResults=d.proxy_test_results||{}' not in pages,
'no automatic performance startup': 'start_proxy_performance_refresh()' not in main[main.index('async def startup'):main.index('async def shutdown')],
'exact one proxy endpoint': 'record = proxy_repository.get_record(proxy_id)' in main and 'outbound.test_proxy_record(record)' in main,
'failure has no fabricated timing': '"status": None' in main and '"ok": False' in main,
'official source vendored': (r/'third_party/psiphon-tunnel-core/ConsoleClient/main.go').exists() and (r/'third_party/psiphon-tunnel-core/LICENSE').exists(),
'official console built': 'go build -mod=vendor' in docker and './ConsoleClient' in docker,
'railway secret config': 'PSIPHON_CONFIG_B64' in cfg and 'b64decode' in cfg,
'truthful states': all(x in mgr for x in ['"UNAVAILABLE"','"ACTIVE"','process_running','vless_transport_available']),
'explicit lifecycle API': all(x in main for x in ['/api/psiphon/start','/api/psiphon/stop','/api/psiphon/status']),
'primary relay isolated': 'psiphon' not in relay.lower() and 'open_outbound(' not in psi.lower(),
'udp truthful': 'NOT_SUPPORTED' in (r/'psiphon/models.py').read_text(),
'no shell subprocess': 'create_subprocess_exec' in mgr and 'create_subprocess_shell' not in mgr,
}
for k,v in checks.items(): print(('PASS ' if v else 'FAIL ')+k)
if not all(checks.values()): sys.exit(1)
ast.parse(main); ast.parse(pages); print('official Psiphon + proxy UI contract: PASS')
