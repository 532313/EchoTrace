#!/usr/bin/env python3
"""Package only source + sealed build dependencies; never recordings, tokens or models."""
import argparse
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument(
    "--output", type=Path, default=ROOT / "build/echotrace-sources.tar.gz"
)
args = parser.parse_args()
args.output.parent.mkdir(parents=True, exist_ok=True)
paths = [
    "CMakeLists.txt",
    "README.md",
    ".gitignore",
    "pyproject.toml",
    "src",
    "native",
    "cmake",
    "scripts",
    "tests",
    "config",
    "bsp",
    "docs",
    "models/config.example.json",
    "models/models.lock.json",
    "models/qwen-source.lock.json",
    "dependencies.lock.json",
    "fonts.lock.json",
    "rkllm.lock.json",
    "sources.lock.json",
    "build/deps",
    "build/vendor",
]


def clean(info):
    if any(
        p in (".git", "__pycache__", ".DS_Store", "work") for p in Path(info.name).parts
    ):
        return None
    if info.issym() or info.islnk():
        return None
    return info


with tarfile.open(args.output, "w:gz") as bundle:
    for name in paths:
        if (ROOT / name).exists():
            bundle.add(ROOT / name, arcname=name, filter=clean)
print(args.output)
