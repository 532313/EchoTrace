#!/usr/bin/env python3
"""Synthetic capacity/latency workload. It does not measure retrieval accuracy."""
import argparse, json, statistics, time
from pathlib import Path
import numpy as np
from echotrace.config import Settings
from echotrace.store import Store, canonical
from echotrace.protocol import EventIn, SessionCreate, QueryIn
from echotrace.memory import MemoryService

p = argparse.ArgumentParser()
p.add_argument("--data", type=Path, default=Path("runtime/benchmark-index"))
p.add_argument("--models", type=Path, default=Path("models/config.json"))
p.add_argument("--anchors", type=int, default=10000)
p.add_argument("--queries", type=int, default=100)
p.add_argument("--out", type=Path, default=Path("runtime/index-benchmark.json"))
a = p.parse_args()
if a.data.exists() and list(a.data.iterdir()):
    raise SystemExit("use an empty benchmark directory; existing data is never erased")
s = Settings(a.data.resolve(), model_config=a.models.resolve(), min_free_bytes=0)
s.prepare()
store = Store(s.root)
service = MemoryService(store, s)
project = store.create_project("SYNTHETIC CAPACITY WORKLOAD; NOT USER EVALUATION")
session = store.create_session(
    SessionCreate(
        project_id=project["id"],
        device_id="synthetic-capacity",
        boot_id="synthetic",
        source_time_us=1,
    )
)
rng = np.random.default_rng(42)
began = time.monotonic()
for i in range(a.anchors):
    event = EventIn(
        id=f"synthetic-{i}",
        session_id=session["id"],
        device_id="synthetic-capacity",
        boot_id="synthetic",
        seq=i,
        source_time_us=i * 1000000 + 1,
        kind="mark",
        note=f"测试步骤编号 {i}：因为排线接触不良而更换线缆。",
        payload={"synthetic": True, "not_quality_evaluation": True},
    )
    store.ingest(event)
    # Random normalized vectors exercise flat-search RAM and compute, not semantics.
    vector = rng.normal(size=512).astype("<f4")
    vector /= np.linalg.norm(vector)
    with store.transaction():
        store.db.execute(
            "INSERT INTO embeddings VALUES(?,?,?,?,?)",
            (
                event.id,
                service.ai.options.get("embedding", {}).get(
                    "revision", "local-unsealed"
                ),
                "synthetic-vector",
                vector.tobytes(),
                512,
            ),
        )
seed_seconds = time.monotonic() - began
service.query(QueryIn(text="为什么更换排线", project_id=project["id"]))
latencies = []
modes = set()
for i in range(a.queries):
    start = time.monotonic()
    answer = service.query(
        QueryIn(
            text=f"测试步骤编号 {i%a.anchors} 为什么更换排线", project_id=project["id"]
        )
    )
    latencies.append(time.monotonic() - start)
    modes.add(answer["mode"])
ordered = sorted(latencies)
report = {
    "synthetic_workload": True,
    "quality_evaluation": False,
    "anchors": a.anchors,
    "vector_dimensions": 512,
    "queries": a.queries,
    "modes": sorted(modes),
    "seed_seconds": seed_seconds,
    "p50_seconds": statistics.median(latencies),
    "p95_seconds": ordered[int(0.95 * (len(ordered) - 1))],
    "max_seconds": max(latencies),
    "notes": "Vectors are synthetic. No Recall/precision claims may be derived from this report.",
}
a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_text(json.dumps(report, indent=2))
store.close()
print(json.dumps(report, indent=2))
