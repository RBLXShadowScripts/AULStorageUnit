import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
d = json.load(open(root / "mods.json", encoding="utf-8"))
have = 0
for m in d["mods"]:
    if "image" in m:
        have += 1
    else:
        print(f"{m['id']:28s} {m.get('github', '')}")
print(f"--- {have} with image, {len(d['mods']) - have} without")
