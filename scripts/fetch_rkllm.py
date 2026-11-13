#!/usr/bin/env python3
"""Fetch only the pinned official RKNN/RKLLM headers, helpers and private runtimes."""
import hashlib, json, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REV = "f7390530443bf84f0394255a449d7cbe81e69d1c"
BASE = "https://raw.githubusercontent.com/airockchip/rknn-llm/" + REV + "/"
paths = {
    "include/rkllm.h": "rkllm-runtime/Linux/librkllm_api/include/rkllm.h",
    "include/rknn_api.h": "examples/multimodal_model_demo/deploy/3rdparty/librknnrt/Linux/librknn_api/include/rknn_api.h",
    "src/image_enc.cc": "examples/multimodal_model_demo/deploy/src/image_enc.cc",
    "include/image_enc.h": "examples/multimodal_model_demo/deploy/src/image_enc.h",
    "lib/librkllmrt.so": "rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so",
    "lib/librknnrt.so": "examples/multimodal_model_demo/deploy/3rdparty/librknnrt/Linux/librknn_api/aarch64/librknnrt.so",
}
root = ROOT / "build/rkllm"
lockpath = ROOT / "rkllm.lock.json"
lock = json.loads(lockpath.read_text()) if lockpath.exists() else {}
records = {}
for relative, source in paths.items():
    out = root / relative
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        temporary = out.with_suffix(out.suffix + ".part")
        urllib.request.urlretrieve(BASE + source, temporary)
        temporary.replace(out)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    if (
        relative in lock.get("files", {})
        and lock["files"][relative]["sha256"] != digest
    ):
        raise RuntimeError("SDK hash mismatch: " + relative)
    records[relative] = {
        "url": BASE + source,
        "sha256": digest,
        "size_bytes": out.stat().st_size,
    }
    print(relative, flush=True)
lockpath.write_text(
    json.dumps({"revision": REV, "tag": "release-v1.3.1", "files": records}, indent=2)
)
