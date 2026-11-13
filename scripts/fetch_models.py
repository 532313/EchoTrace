#!/usr/bin/env python3
"""Build-time downloads from pinned primary sources. Runtime never downloads models."""
import argparse, hashlib, json, os, tarfile, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument("roles", nargs="+", choices=["asr", "bge-source", "tts", "vad"])
p.add_argument("--root", type=Path, default=ROOT / "models")
args = p.parse_args()
base = args.root.resolve()
base.mkdir(parents=True, exist_ok=True)
lockpath = base / "artifacts.lock.json"
lock = json.loads(lockpath.read_text()) if lockpath.exists() else {}
cfgpath = base / "config.json"
config = json.loads(cfgpath.read_text()) if cfgpath.exists() else {}


def fetch(url, relative):
    out = base / relative
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        tmp = out.with_suffix(out.suffix + ".part")
        digest = hashlib.sha256()
        print("fetch", relative, flush=True)
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "EchoTrace-build/0.1"}),
            timeout=120,
        ) as src, tmp.open("wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)
                digest.update(chunk)
        os.replace(tmp, out)
    digest = hashlib.sha256()
    with out.open("rb") as stream:
        for c in iter(lambda: stream.read(65536), b""):
            digest.update(c)
    value = digest.hexdigest()
    if relative in lock and lock[relative]["sha256"] != value:
        raise RuntimeError("asset differs from its sealed hash: " + relative)
    lock[relative] = {"url": url, "sha256": value, "size_bytes": out.stat().st_size}
    return str(out.relative_to(base))


for role in args.roles:
    if role == "asr":
        rev = "2365baeacb507f821a0c8120fcee3d484dba7a07"
        url = (
            "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/"
            + rev
            + "/"
        )
        config["asr"] = {
            "model": fetch(
                url + "model.int8.onnx", "assets/sensevoice/model.int8.onnx"
            ),
            "tokens": fetch(url + "tokens.txt", "assets/sensevoice/tokens.txt"),
            "revision": rev,
            "test_audio": "assets/sensevoice/test-zh.wav",
            "license": "Apache-2.0 (see bundled model LICENSE)",
        }
        fetch(url + "LICENSE", "assets/sensevoice/LICENSE")
        fetch(url + "test_wavs/zh.wav", "assets/sensevoice/test-zh.wav")
    elif role == "bge-source":
        rev = "7999e1d3359715c523056ef9478215996d62a620"
        url = "https://huggingface.co/BAAI/bge-small-zh-v1.5/resolve/" + rev + "/"
        for file in [
            "model.safetensors",
            "config.json",
            "vocab.txt",
            "tokenizer_config.json",
            "tokenizer.json",
            "special_tokens_map.json",
        ]:
            fetch(url + file, "assets/bge-source/" + file)
    elif role == "vad":
        rev = "066409f3d9db45a30d21dfcbf9147826ffdd7052"
        url = (
            "https://raw.githubusercontent.com/snakers4/silero-vad/"
            + rev
            + "/src/silero_vad/data/silero_vad.onnx"
        )
        config["vad"] = {
            "model": fetch(url, "assets/vad/silero_vad.onnx"),
            "revision": rev,
            "context_samples": 64,
        }
    elif role == "tts":
        archive = fetch(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/matcha-icefall-zh-baker.tar.bz2",
            "downloads/matcha-icefall-zh-baker.tar.bz2",
        )
        target = base / "assets/tts"
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(base / archive) as bundle:
            for entry in bundle:
                if not entry.isfile():
                    continue
                name = Path(*Path(entry.name).parts[1:])
                dest = (target / name).resolve()
                if not dest.is_relative_to(target.resolve()):
                    raise ValueError("unsafe archive")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(entry) as src, dest.open("wb") as dst:
                    import shutil

                    shutil.copyfileobj(src, dst)
        vocoder = fetch(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/vocos-22khz-univ.onnx",
            "assets/tts/vocos-22khz-univ.onnx",
        )
        model = next(target.glob("model*.onnx"))
        config["tts"] = {
            "model": str(model.relative_to(base)),
            "tokens": "assets/tts/tokens.txt",
            "lexicon": "assets/tts/lexicon.txt",
            "vocoder": vocoder,
            "revision": lock[archive]["sha256"],
            "rule_fsts": ",".join(
                str(x.relative_to(base)) for x in sorted(target.glob("*.fst"))
            ),
            "dict_dir": "assets/tts/dict",
            "license": "Baker dataset: non-commercial only; see model README",
        }
existing_lock = json.loads(lockpath.read_text()) if lockpath.exists() else {}
existing_lock.update(lock)
lockpath.write_text(json.dumps(existing_lock, ensure_ascii=False, indent=2))
existing = json.loads(cfgpath.read_text()) if cfgpath.exists() else {}
existing.update({role: config[role] for role in args.roles if role in config})
cfgpath.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
print("sealed", len(lock), "artifacts", flush=True)
