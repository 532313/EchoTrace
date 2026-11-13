from __future__ import annotations


def make_playback_plan(evidence):
    anchor = evidence["anchor"]
    begin, end = evidence["range_start_us"], evidence["range_end_us"]
    if anchor.get("ended_us") is not None:
        end = min(end, anchor["ended_us"])
    plan = {
        "format": "echotrace-playback-v1",
        "session_id": anchor["session_id"],
        "boot_id": anchor["boot_id"],
        "begin_us": begin,
        "end_us": end,
        "anchor_us": anchor["source_time_us"],
        "continuity_tolerance_us": 50000,
        "video": [],
        "audio": [],
        "gaps": [],
    }
    for kind in ("video", "audio"):
        segments = sorted(
            (
                media
                for media in evidence["media"]
                if media["kind"] == kind
                and media["start_us"] < end
                and media["end_us"] > begin
            ),
            key=lambda media: (media["start_us"], media["id"]),
        )
        plan[kind] = [
            {
                key: media[key]
                for key in (
                    "id",
                    "start_us",
                    "end_us",
                    "available",
                    "url",
                    "size_bytes",
                    "sha256",
                    "discontinuity",
                )
            }
            for media in segments
        ]
        boundaries = sorted(
            {begin, end}
            | {max(begin, media["start_us"]) for media in segments}
            | {min(end, media["end_us"]) for media in segments}
        )
        gaps = []
        for start, stop in zip(boundaries, boundaries[1:]):
            covering = [
                media
                for media in segments
                if media["start_us"] < stop and media["end_us"] > start
            ]
            if any(media["available"] for media in covering):
                continue
            gap = {
                "kind": kind,
                "start_us": start,
                "end_us": stop,
                "reason": "uncached" if covering else "unrecorded",
            }
            if covering:
                gap["media_id"] = covering[-1]["id"]
            if (
                gaps
                and gaps[-1]["end_us"] == start
                and gaps[-1]["reason"] == gap["reason"]
                and gaps[-1].get("media_id") == gap.get("media_id")
            ):
                gaps[-1]["end_us"] = stop
            else:
                gaps.append(gap)
        plan["gaps"].extend(gaps)
    return plan
