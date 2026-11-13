#!/usr/bin/env python3
"""Build-host source weights for the exact Qwen3.5 model in the implementation plan."""
import hashlib, json, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REV = "2fc06364715b967f1860aea9cf38778875588b17"
out = ROOT / "models/assets/qwen-source"
out.mkdir(parents=True, exist_ok=True)
files = [
    "LICENSE",
    "config.json",
    "model.safetensors-00001-of-00001.safetensors",
    "model.safetensors.index.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "chat_template.jinja",
    "video_preprocessor_config.json",
]
lock = {}
for name in files:
    path = out / name
    url = "https://huggingface.co/Qwen/Qwen3.5-0.8B/resolve/" + REV + "/" + name
    if not path.exists():
        print("fetch", name, flush=True)
        temporary = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, temporary)
        temporary.replace(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    lock[name] = {
        "sha256": digest.hexdigest(),
        "url": url,
        "size_bytes": path.stat().st_size,
    }
(ROOT / "models/qwen-source.lock.json").write_text(
    json.dumps({"revision": REV, "files": lock}, indent=2)
)
print("Qwen source sealed", flush=True)
