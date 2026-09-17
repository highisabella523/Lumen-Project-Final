from pathlib import Path
root = Path(__file__).resolve().parents[1]
for path in root.rglob('*'):
    if path.is_file() and '.git' not in path.parts and '__pycache__' not in path.parts:
        assert ('psi' + 'phon') not in path.read_text(encoding='utf-8', errors='ignore').lower(), path
print('Removed transport remnants: none')
