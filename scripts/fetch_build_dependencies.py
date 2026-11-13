#!/usr/bin/env python3
"""Fetch fixed upstream releases for builds. No network is used by the app."""
import argparse
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES = {
    "picosha2": (
        "1.0.0",
        "https://codeload.github.com/okdshin/PicoSHA2/tar.gz/refs/tags/v1.0.0",
    ),
    "jsoncpp": (
        "1.9.5",
        "https://codeload.github.com/open-source-parsers/jsoncpp/tar.gz/refs/tags/1.9.5",
    ),
    "websocketpp": (
        "0.8.2",
        "https://codeload.github.com/zaphoyd/websocketpp/tar.gz/refs/tags/0.8.2",
    ),
    "asio": (
        "1.28.0",
        "https://codeload.github.com/chriskohlhoff/asio/tar.gz/refs/tags/asio-1-28-0",
    ),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "build/deps")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    lock_path = ROOT / "dependencies.lock.json"
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    for name, (version, url) in RELEASES.items():
        target = args.output / name
        if target.is_dir() and (target / ".echotrace-sha256").exists():
            print(name, "cached")
            continue
        data = urllib.request.urlopen(url, timeout=60).read()
        digest = hashlib.sha256(data).hexdigest()
        if name in lock and lock[name]["sha256"] != digest:
            raise RuntimeError(name + " upstream archive checksum changed")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            prefix = members[0].name.split("/")[0] + "/"
            for member in members:
                relative = (
                    member.name[len(prefix) :] if member.name.startswith(prefix) else ""
                )
                if not relative:
                    continue
                path = (target / relative).resolve()
                if not path.is_relative_to(target.resolve()):
                    raise RuntimeError("archive path escapes destination")
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.extractfile(member).read())
        (target / ".echotrace-sha256").write_text(digest)
        lock[name] = {"version": version, "url": url, "sha256": digest}
        print(name, digest)
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")


if __name__ == "__main__":
    main()
