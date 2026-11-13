#!/usr/bin/env python3
"""Version, package and identify committed EchoTrace sources and build artifacts."""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = "src/echotrace/__init__.py"
VERSION_PATTERN = r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|\.dev\d+)?"
LOCKS = (
    "dependencies.lock.json", "fonts.lock.json", "rkllm.lock.json",
    "models/models.lock.json", "models/qwen-source.lock.json",
    "config/requirements-ci.lock.txt", "integration/deskbot/baseline.json",
)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def version_of(source):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(VERSION_PATTERN, value):
                return value
    raise ValueError("Invalid or missing __version__ in " + VERSION_FILE)


def working_version():
    return version_of((ROOT / VERSION_FILE).read_text(encoding="utf-8"))


def require_clean():
    if git("status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError("Commit the source changes before packaging a release")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check(tag=None):
    version = working_version()
    if tag:
        if tag != "v" + version:
            raise ValueError(f"Tag {tag} does not match product version {version}")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        if f"## [{version}]" not in changelog:
            raise ValueError("Add this version's entry to CHANGELOG.md before tagging")
    print("EchoTrace", version)


def source_archive(ref, output):
    require_clean()
    commit = git("rev-parse", ref + "^{commit}").decode().strip()
    version = version_of(git("show", commit + ":" + VERSION_FILE).decode("utf-8"))
    output = output or ROOT / "build/releases" / (
        "echotrace-" + version + "-" + commit[:12] + ".tar.gz")
    output = output.resolve()
    manifest_path = output.with_name(output.name + ".manifest.json")
    checksum_path = output.with_name(output.name + ".sha256")
    if any(path.exists() for path in (output, manifest_path, checksum_path)):
        raise ValueError("Refusing to overwrite an existing release artifact: " + str(output))
    epoch = int(git("show", "-s", "--format=%ct", commit).strip())
    prefix = "echotrace-" + version + "/"
    manifest = {"schema": 1, "project": "EchoTrace", "kind": "source",
                "version": version, "source_commit": commit,
                "source_ref": ref, "source_date_epoch": epoch, "dirty": False}
    archive = git("archive", "--format=tar", "--prefix=" + prefix, commit)
    buffer = io.BytesIO(archive)
    with tarfile.open(fileobj=buffer, mode="a") as bundle:
        metadata = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        item = tarfile.TarInfo(prefix + "release-manifest.json")
        item.size, item.mtime, item.mode = len(metadata), epoch, 0o644
        bundle.addfile(item, io.BytesIO(metadata))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
            compressed.write(buffer.getvalue())
    manifest.update(archive=output.name, size_bytes=output.stat().st_size,
                    sha256=digest(output))
    write_json(manifest_path, manifest)
    checksum_path.write_text(
        manifest["sha256"] + "  " + output.name + "\n", encoding="utf-8")
    print(output)
    print(manifest_path)


def artifact_manifest(args):
    dirty = bool(git("status", "--porcelain", "--untracked-files=normal").strip())
    if dirty and not args.allow_dirty:
        raise ValueError("Artifact manifests require committed sources; use --allow-dirty only for development")
    if args.output.exists():
        raise ValueError("Refusing to overwrite an existing artifact manifest")
    files = []
    for path in args.file:
        if not path.is_file():
            raise ValueError("Artifact is missing: " + str(path))
        files.append({"name": path.name, "size_bytes": path.stat().st_size,
                      "sha256": digest(path)})
    write_json(args.output, {
        "schema": 1, "project": "EchoTrace", "kind": "build-artifacts",
        "version": working_version(), "source_commit": git("rev-parse", "HEAD").decode().strip(),
        "dirty": dirty, "target": args.target, "component": args.component,
        "toolchain": args.toolchain, "artifacts": files,
        "input_locks": {name: digest(ROOT / name) for name in LOCKS if (ROOT / name).is_file()},
    })
    print(args.output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("check")
    verify.add_argument("--tag")
    bump = commands.add_parser("bump")
    bump.add_argument("version")
    source = commands.add_parser("source")
    source.add_argument("--ref", default="HEAD")
    source.add_argument("--output", type=Path)
    artifact = commands.add_parser("artifact")
    artifact.add_argument("--target", choices=("rv1106", "rk3576", "host"), required=True)
    artifact.add_argument("--component", required=True)
    artifact.add_argument("--file", type=Path, action="append", required=True)
    artifact.add_argument("--toolchain", default="unspecified")
    artifact.add_argument("--output", type=Path, required=True)
    artifact.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "check":
            check(args.tag)
        elif args.command == "bump":
            if not re.fullmatch(VERSION_PATTERN, args.version):
                raise ValueError("Use a version such as 0.2.0.dev1, 0.2.0rc1 or 0.2.0")
            path = ROOT / VERSION_FILE
            value = re.sub(r'^__version__ = "[^"]+"$',
                           '__version__ = "' + args.version + '"',
                           path.read_text(encoding="utf-8"), count=1, flags=re.M)
            path.write_text(value, encoding="utf-8")
            check()
            print("Update CHANGELOG.md and commit before creating a tag.")
        elif args.command == "source":
            source_archive(args.ref, args.output)
        else:
            artifact_manifest(args)
    except (ValueError, subprocess.CalledProcessError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
