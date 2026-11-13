#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from check_storage import mounted_storage


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(262144), b""):
            result.update(block)
    return result.hexdigest()


def migrate(source, destination, mount, device):
    source, destination, mount = (
        source.resolve(),
        destination.resolve(),
        mount.resolve(),
    )
    storage = mounted_storage(mount, device)
    if (
        destination.exists()
        or not destination.is_relative_to(mount)
        or source.is_relative_to(destination)
    ):
        raise ValueError("destination must be a new directory on the checked SD mount")
    if not source.is_dir():
        raise ValueError("source data directory does not exist")
    state = json.loads((source / "state.json").read_text())
    if state.get("state") != "stopped" or state.get("playback_active"):
        raise ValueError("stop capture and playback before migration")
    for command in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            arguments = command.read_bytes().split(b"\0")
            if (
                arguments
                and Path(os.fsdecode(arguments[0])).name == "echotrace-edge"
                and b"--data" in arguments
            ):
                if (
                    Path(
                        os.fsdecode(arguments[arguments.index(b"--data") + 1])
                    ).resolve()
                    == source
                ):
                    raise ValueError("source capture daemon is still running")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    paths = list(source.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("source contains symlinks; review paths before migrating")
    total = sum(path.stat().st_size for path in paths if path.is_file())
    if storage["available_bytes"] < total + 256 * 1024 * 1024:
        raise ValueError("insufficient SD reserve for migration")
    temporary = Path(tempfile.mkdtemp(prefix=".echotrace-migrate-", dir=mount))
    published = False
    try:
        for path in paths:
            target = temporary / path.relative_to(source)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                if digest(target) != digest(path):
                    raise ValueError("migration copy checksum mismatch")
        media_count = 0
        for manifest in (temporary / "manifests").glob("*.json"):
            metadata = json.loads(manifest.read_text())
            old = Path(metadata["path"]).resolve()
            relative = old.relative_to(source)
            target = temporary / relative
            if (
                target.stat().st_size != metadata["media"]["size_bytes"]
                or digest(target) != metadata["media"]["sha256"]
            ):
                raise ValueError("original media differs from its immutable manifest")
            metadata["path"] = str(destination / relative)
            manifest.write_text(json.dumps(metadata, ensure_ascii=False))
            media_count += 1
        for path in temporary.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        temporary.rename(destination)
        directory = os.open(mount, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        published = True
        return {
            "source": str(source),
            "destination": str(destination),
            "copied_bytes": total,
            "verified_original_media": media_count,
            "source_preserved": True,
            "journal_bytes_preserved": True,
            "private_media_paths_rebased": True,
            "storage": storage,
        }
    finally:
        if not published and temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser()
    for name in ("source", "destination", "mount", "device", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = migrate(args.source, args.destination, args.mount, args.device)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.write_text(output)
    print(output)


if __name__ == "__main__":
    main()
