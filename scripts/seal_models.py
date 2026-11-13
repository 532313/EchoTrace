#!/usr/bin/env python3
"""Seal model and tokenizer bytes after conversion. Use --check before deployment."""
import argparse, hashlib, json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--config", type=Path, default=Path("models/config.json"))
p.add_argument("--check", action="store_true")
a = p.parse_args()
config = json.loads(a.config.read_text())
records = {}
for role, values in config.items():
    if not isinstance(values, dict):
        continue
    for key in (
        "model",
        "tokens",
        "vocab",
        "lexicon",
        "vocoder",
        "worker",
        "vision",
        "language",
        "test_audio",
    ):
        if not values.get(key):
            continue
        path = Path(values[key])
        path = path if path.is_absolute() else (a.config.parent / path).resolve()
        if not path.is_file():
            raise SystemExit(f"{role}.{key}: missing {path}")
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                sha.update(chunk)
        digest = sha.hexdigest()
        records[f"{role}.{key}"] = {"sha256": digest, "size_bytes": path.stat().st_size}
        if a.check and values.get("sha256", {}).get(key) != digest:
            raise SystemExit("model hash mismatch: " + role + "." + key)
        values.setdefault("sha256", {})[key] = digest
if not a.check:
    a.config.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    a.config.with_name("models.lock.json").write_text(json.dumps(records, indent=2))
print(
    json.dumps({"verified_files": len(records), "mode": "check" if a.check else "seal"})
)
