from pathlib import Path
import re

root = Path(__file__).resolve().parents[1] / "papers"
files = sorted(root.glob("*.md"))
invalid = [p.name for p in files if not re.fullmatch(r"[0-9a-f]{40}\.md", p.name)]
empty = [p.name for p in files if not p.read_text(encoding="utf-8").strip()]
print(f"Markdown files: {len(files)}")
print(f"Invalid names: {len(invalid)}; empty files: {len(empty)}")
if invalid or empty:
    raise SystemExit(1)
