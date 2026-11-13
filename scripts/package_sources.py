#!/usr/bin/env python3
"""Compatibility entrypoint: package committed Git sources and a revision manifest."""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument(
    "--output", type=Path, default=ROOT / "build/echotrace-sources.tar.gz"
)
args = parser.parse_args()
subprocess.run([sys.executable, str(ROOT / "scripts/release.py"),
                "source", "--output", str(args.output)], check=True)
