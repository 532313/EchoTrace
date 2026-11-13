#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import secrets
import stat
import time
from pathlib import Path


def mounted_storage(mount, device):
    mount, device = mount.resolve(), device.resolve()
    block = device.stat()
    if not stat.S_ISBLK(block.st_mode):
        raise ValueError("expected a block device")
    if not mount.is_mount() or mount.stat().st_dev != block.st_rdev:
        raise ValueError("mount does not refer to the expected block device")
    if mount.stat().st_dev == Path("/").stat().st_dev:
        raise ValueError("refuse to use the root filesystem as SD storage")
    filesystem = os.statvfs(mount)
    if filesystem.f_flag & os.ST_RDONLY:
        raise ValueError("SD filesystem is read-only")
    records = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        fields = before.split()
        if fields[4] == str(mount):
            records.append(
                {
                    "source": after.split()[1],
                    "filesystem": after.split()[0],
                    "options": fields[5],
                }
            )
    if len(records) != 1:
        raise ValueError("ambiguous SD mount record")
    return {
        **records[0],
        "mount": str(mount),
        "device": str(device),
        "total_bytes": filesystem.f_blocks * filesystem.f_frsize,
        "available_bytes": filesystem.f_bavail * filesystem.f_frsize,
        "root_device_distinct": True,
    }


def write_probe(mount, size_bytes):
    temporary = mount / (".echotrace-probe-" + secrets.token_hex(8) + ".part")
    complete = temporary.with_suffix(".bin")
    nonce = secrets.token_bytes(32)
    digest = hashlib.sha256()
    started = time.monotonic()
    try:
        with temporary.open("xb", buffering=0) as stream:
            written = 0
            counter = 0
            while written < size_bytes:
                block = (
                    hashlib.sha256(nonce + counter.to_bytes(8, "big")).digest() * 8192
                )[: min(262144, size_bytes - written)]
                stream.write(block)
                digest.update(block)
                written += len(block)
                counter += 1
            os.fsync(stream.fileno())
            advice = False
            if hasattr(os, "posix_fadvise"):
                try:
                    os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                    advice = True
                except OSError:
                    pass
        write_seconds = time.monotonic() - started
        temporary.rename(complete)
        directory = os.open(mount, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        read_start = time.monotonic()
        actual = hashlib.sha256()
        with complete.open("rb", buffering=0) as stream:
            for block in iter(lambda: stream.read(262144), b""):
                actual.update(block)
        if complete.stat().st_size != size_bytes or actual.digest() != digest.digest():
            raise ValueError("SD file readback checksum differs")
        return {
            "passed": True,
            "bytes": size_bytes,
            "write_and_fsync_seconds": write_seconds,
            "read_seconds": time.monotonic() - read_start,
            "sha256": actual.hexdigest(),
            "fadvise_dontneed_requested": advice,
            "directory_fsync": True,
            "scope": "bounded file integrity check; not a full-card capacity test",
        }
    finally:
        temporary.unlink(missing_ok=True)
        complete.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mount", type=Path, required=True)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--write-mib", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 <= args.write_mib <= 256:
        parser.error("write probe must be between 0 and 256 MiB")
    report = {
        "storage": mounted_storage(args.mount, args.device),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "source_monotonic_us": time.monotonic_ns() // 1000,
    }
    size = args.write_mib * 1024 * 1024
    if size:
        if report["storage"]["available_bytes"] < size + 256 * 1024 * 1024:
            raise ValueError("SD free space is below the probe and recording reserve")
        report["write_probe"] = write_probe(args.mount, size)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output)
    print(output)


if __name__ == "__main__":
    main()
