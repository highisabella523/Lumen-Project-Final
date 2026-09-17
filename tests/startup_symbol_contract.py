from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]
main_source = (ROOT / "main.py").read_text(encoding="utf-8")
telegram_source = (ROOT / "telegram_bot.py").read_text(encoding="utf-8")
main_tree = ast.parse(main_source)
module_symbols = set()
for node in main_tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        module_symbols.add(node.name)
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        module_symbols.update(t.id for t in targets if isinstance(t, ast.Name))
assert "set_link_sub" in module_symbols
telegram_imports = ast.parse(telegram_source).body
main_import = next(node for node in telegram_imports if isinstance(node, ast.ImportFrom) and node.module == "main")
assert "set_link_sub" in {alias.name for alias in main_import.names}
assert "from relay_psiphon" not in main_source
assert "/api/psiphon/" not in main_source
print("startup symbol + removed transport contract: PASS")
