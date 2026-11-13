#!/usr/bin/env python3
"""Inspect Git index bytes before commits and in CI; never print credential values."""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import PurePosixPath

FORBIDDEN_ROOTS = (
    "build/", "dist/", "runtime/", "models/assets/", "models/downloads/",
    ".venv/", ".convert-venv/", ".rk-convert/", ".rknn-convert/", "scripts/work/",
)
FORBIDDEN_SUFFIXES = (
    ".token", ".key", ".pem", ".rknn", ".rkllm", ".onnx", ".pt", ".pth",
    ".mp4", ".wav", ".pcm", ".raw", ".sqlite3", ".db", ".o", ".a",
    ".ko", ".dtb", ".img", ".bin", ".bundle", ".tar.gz", ".zip",
)
SECRET_PATTERNS = {
    "private key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "GitHub token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})",
    "API token": rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}",
}


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    command = (
        ("diff", "--cached", "--name-only", "--no-renames", "--diff-filter=ACMR", "-z")
        if args.staged else ("ls-files", "-z")
    )
    paths = [p.decode("utf-8") for p in git(*command).split(b"\0") if p]
    errors = []
    for path in paths:
        name = PurePosixPath(path).name
        if (path.startswith(FORBIDDEN_ROOTS) or path.endswith(FORBIDDEN_SUFFIXES)
                or (name.startswith(".env") and name != ".env.example")
                or path == "models/config.json" or path == "sources.lock.json"):
            errors.append((path, "local/generated/credential file must stay outside Git"))
            continue
        data = git("show", ":" + path)
        if len(data) > 5 * 1024 * 1024:
            errors.append((path, "file exceeds the 5 MiB source-file limit"))
        for label, pattern in SECRET_PATTERNS.items():
            if re.search(pattern, data):
                errors.append((path, label + " detected"))
    for path, reason in errors:
        print(path + ": " + reason)
    if errors:
        raise SystemExit(1)
    print(f"Repository policy passed for {len(paths)} indexed files.")


if __name__ == "__main__":
    main()
