#!/usr/bin/env python3
"""Evaluate genuine, labelled held-out sessions through the public query API."""
import argparse, json, statistics, time, urllib.request
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("questions", type=Path)
p.add_argument("--hub", default="http://127.0.0.1:18081")
p.add_argument("--token-file", type=Path)
p.add_argument("--output", type=Path, default=Path("runtime/evaluation.json"))
a = p.parse_args()
headers = {"Content-Type": "application/json"}
if a.token_file:
    headers["Authorization"] = "Bearer " + a.token_file.read_text().strip()
records = [json.loads(x) for x in a.questions.read_text().splitlines() if x.strip()]
results = []
positive = negative = correct = abstained = 0
errors = []
for item in records:
    if item.get("split") != "test":
        raise SystemExit(
            "only explicitly held-out test questions may enter this report"
        )
    if (
        "negative" not in item
        or not item["negative"]
        and not item.get("expected_anchor_ids")
    ):
        raise SystemExit("ground-truth labels are required")
    body = {
        key: item[key]
        for key in ("text", "project_id", "session_id", "semantic")
        if key in item
    }
    body["top_k"] = 5
    request = urllib.request.Request(
        a.hub + "/api/query",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    start = time.monotonic()
    with urllib.request.urlopen(request, timeout=120) as r:
        result = json.load(r)
    latency = time.monotonic() - start
    ids = [h["anchor"]["id"] for h in result["hits"]]
    found = bool(set(ids) & set(item.get("expected_anchor_ids", [])))
    if item["negative"]:
        negative += 1
        abstained += result["status"] == "insufficient"
    else:
        positive += 1
        correct += found
    if found and "expected_source_time_us" in item:
        selected = next(
            h["anchor"]
            for h in result["hits"]
            if h["anchor"]["id"] in item["expected_anchor_ids"]
        )
        errors.append(
            abs(selected["source_time_us"] - item["expected_source_time_us"]) / 1e6
        )
    results.append(
        {
            "question_id": item["id"],
            "status": result["status"],
            "retrieved_ids": ids,
            "matched": found,
            "retrieval_seconds": latency,
            "mode": result["mode"],
        }
    )


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * q + 0.5))]


report = {
    "questions": len(records),
    "positive": positive,
    "negative": negative,
    "recall_at_5": correct / positive if positive else None,
    "negative_abstention_rate": abstained / negative if negative else None,
    "retrieval_p95_seconds": percentile(
        [x["retrieval_seconds"] for x in results], 0.95
    ),
    "source_time_error_p95_seconds": percentile(errors, 0.95),
    "asr_and_generation_included": False,
    "results": results,
    "warning": "Confidence thresholds must be fixed before evaluating these held-out sessions.",
}
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(
    json.dumps(
        {k: v for k, v in report.items() if k != "results"},
        ensure_ascii=False,
        indent=2,
    )
)
