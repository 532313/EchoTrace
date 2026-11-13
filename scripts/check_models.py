#!/usr/bin/env python3
"""Run the service's real model instances and record system memory while they coexist."""
import argparse, json, urllib.request
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--hub", default="http://127.0.0.1:18081")
p.add_argument("--anchor-id")
p.add_argument("--token-file", type=Path)
p.add_argument("--out", type=Path, default=Path("runtime/model-check.json"))
a = p.parse_args()
headers = {"Content-Type": "application/json"}
if a.token_file:
    headers["Authorization"] = "Bearer " + a.token_file.read_text().strip()
req = urllib.request.Request(
    a.hub + "/api/models/check",
    data=json.dumps({"anchor_id": a.anchor_id}).encode(),
    headers=headers,
    method="POST",
)
with urllib.request.urlopen(req, timeout=180) as response:
    data = json.load(response)
a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(json.dumps(data, ensure_ascii=False, indent=2))
