"""Find site-packages dist-info with missing/invalid Version (causes pip resolver TypeError)."""
import importlib.metadata as m
from pathlib import Path

sp = Path(r"E:\Conda\envs\learnagent312\Lib\site-packages")
bad = []
for dist in m.distributions(path=[sp]):
    name = dist.metadata.get("Name", dist.name)
    meta_path = Path(dist._path) / "METADATA" if hasattr(dist, "_path") else None
    try:
        v = dist.version
        if v is None or str(v).strip() == "":
            bad.append((name, "version is empty", dist._path))
    except Exception as e:
        bad.append((name, str(e), getattr(dist, "_path", "")))

# Also scan *.dist-info METADATA files
for meta in sp.glob("*.dist-info/METADATA"):
    text = meta.read_text(encoding="utf-8", errors="replace")
    version_lines = [ln for ln in text.splitlines() if ln.startswith("Version:")]
    if not version_lines or version_lines[0].strip() == "Version:":
        bad.append((meta.parent.name, "no Version in METADATA", meta.parent))

for egg in sp.glob("*.egg-info"):
    pkg_info = egg / "PKG-INFO"
    if pkg_info.is_file():
        text = pkg_info.read_text(encoding="utf-8", errors="replace")
        if "Version:" not in text:
            bad.append((egg.name, "no Version in PKG-INFO", egg))

seen = set()
for row in bad:
    key = row[2]
    if key in seen:
        continue
    seen.add(key)
    print("\t".join(str(x) for x in row))
print("total bad:", len(seen))
