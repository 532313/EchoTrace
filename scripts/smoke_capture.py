#!/usr/bin/env python3
"""Bounded hardware smoke test; marks are labelled automation, never user evaluation."""
import argparse, json, time, urllib.request, subprocess
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--hub", default="http://127.0.0.1:18081")
p.add_argument("--token-file", type=Path, required=True)
p.add_argument("--seconds", type=int, default=6)
p.add_argument("--out", type=Path, default=Path("runtime/hardware-smoke.json"))
a = p.parse_args()
if not 2 <= a.seconds <= 15:
    raise SystemExit("without SD, the smoke test is bounded to 2..15 seconds")
token = a.token_file.read_text().strip()


def api(method, path, data=None):
    body = None if data is None else json.dumps(data).encode()
    r = urllib.request.Request(
        a.hub + path,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(r, timeout=30) as response:
        return json.loads(response.read())


status = api("GET", "/api/status")
device = next(d for d in status["devices"] if d["device_id"] == "echo-mate")
project = api("POST", "/api/projects", {"name": "实板短时验证 · 自动化标记"})
session = None
result = {
    "synthetic": False,
    "automated_test": True,
    "performance_acceptance": False,
    "device": device,
}
try:
    session = api(
        "POST",
        "/api/sessions",
        {
            "project_id": project["id"],
            "device_id": device["device_id"],
            "boot_id": device["boot_id"],
            "source_time_us": device["source_time_us"],
            "title": "RV1106 短时摄像头与麦克风验证",
        },
    )
    print("capture started", flush=True)
    time.sleep(a.seconds / 2)
    result["mark_ack"] = api(
        "POST",
        "/api/sessions/" + session["id"] + "/mark",
        {"note": "自动化测试标记：核验摄像头、声音与来源时间；不代表真实操作评测。"},
    )
    time.sleep(a.seconds / 2)
finally:
    if session:
        result["stopped"] = api(
            "PATCH",
            "/api/sessions/" + session["id"] + "/state",
            {"state": "stopped", "source_time_us": device["source_time_us"]},
        )
if session:
    time.sleep(1)
    timeline = api("GET", "/api/sessions/" + session["id"] + "/timeline")
    result["timeline"] = timeline
    for m in timeline["media"]:
        api("POST", "/api/media/" + m["id"] + "/fetch", {})
    for _ in range(120):
        timeline = api("GET", "/api/sessions/" + session["id"] + "/timeline")
        if timeline["media"] and all(m["status"] == "ready" for m in timeline["media"]):
            break
        time.sleep(0.2)
    result["timeline"] = timeline
    if timeline["anchors"]:
        chosen = next(
            (x for x in timeline["anchors"] if x["note"]), timeline["anchors"][0]
        )
        try:
            result["frame"] = api("POST", "/api/anchors/" + chosen["id"] + "/frame", {})
        except Exception as e:
            result["frame_error"] = str(e)
a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(a.out)
