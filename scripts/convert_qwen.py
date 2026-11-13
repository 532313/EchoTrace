#!/usr/bin/env python3
"""Run the pinned vendor conversion recipes with explicit build environments."""
import argparse, hashlib, json, os, subprocess, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REV = "f7390530443bf84f0394255a449d7cbe81e69d1c"
BASE = (
    "https://raw.githubusercontent.com/airockchip/rknn-llm/"
    + REV
    + "/examples/multimodal_model_demo/"
)
p = argparse.ArgumentParser()
p.add_argument("stage", choices=["language", "vision-onnx", "vision"])
p.add_argument("--python", type=Path, required=True)
p.add_argument("--source", type=Path, default=ROOT / "models/assets/qwen-source")
a = p.parse_args()
python = a.python.resolve()
source = a.source.resolve()
work = ROOT / "build" / ("qwen-language" if a.stage == "language" else "qwen-vision")
work.mkdir(parents=True, exist_ok=True)
tools = ROOT / "build/qwen-tools"
tools.mkdir(parents=True, exist_ok=True)


def fetch(remote, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        part = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(BASE + remote, part)
        part.replace(dest)
    return dest


for name in ["export_vision.py", "export_vision_rknn.py", "export_rkllm.py"]:
    fetch("export/" + name, tools / name)
env = {
    **os.environ,
    "OMP_NUM_THREADS": "4",
    "MKL_NUM_THREADS": "4",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}


def run(args):
    subprocess.run([str(python), *map(str, args)], cwd=work, env=env, check=True)


if a.stage == "language":
    fetch("data/make_input_embeds_for_quantize.py", tools / "calibration.py")
    dataset = fetch("data/datasets.json", work / "data/datasets.json")
    items = json.loads(dataset.read_text())
    for item in items:
        fetch("data/datasets/" + item["image"], work / "data/datasets" / item["image"])
    run([tools / "calibration.py", "--path", source, "--model_type", "qwen3.5"])
    # The pinned example consumes inputs.json; the calibration helper writes llm_inputs.json.
    (work / "data/inputs.json").write_bytes(
        (work / "data/llm_inputs.json").read_bytes()
    )
    run(
        [
            tools / "export_rkllm.py",
            "--path",
            source,
            "--target-platform",
            "rk3576",
            "--num_npu_core",
            "2",
            "--quantized_dtype",
            "w4a16",
            "--device",
            "cpu",
        ]
    )
    artifact = next((work / "rkllm").glob("*_w4a16_rk3576.rkllm"))
    name = "qwen3.5-0.8b-w4a16-rk3576.rkllm"
elif a.stage == "vision-onnx":
    run(
        [
            tools / "export_vision.py",
            "--path",
            source,
            "--model_name",
            "qwen3.5",
            "--batch_size",
            "1",
            "--height",
            "448",
            "--width",
            "448",
            "--device",
            "cpu",
        ]
    )
    output = work / "onnx/qwen3.5_vision.onnx"
    if not output.is_file():
        raise SystemExit("ONNX export did not produce an artifact")
    print(output)
    sys.exit(0)
else:
    # ONNX export needs the language-tool environment (Torch 2.6/Transformers 5.8).
    # Reuse an existing verified export before invoking the RKNN/Torch 2.4 environment.
    onnx = work / "onnx/qwen3.5_vision.onnx"
    if not onnx.exists():
        raise SystemExit(
            "First export ONNX with the pinned export_vision.py in the Torch 2.6 environment; then run this stage with the RKNN environment"
        )
    run(
        [
            tools / "export_vision_rknn.py",
            "--path",
            onnx,
            "--model_name",
            "qwen3.5",
            "--target-platform",
            "rk3576",
            "--batch_size",
            "1",
            "--height",
            "448",
            "--width",
            "448",
        ]
    )
    artifact = work / "rknn/qwen3.5_vision_rk3576.rknn"
    name = "qwen3.5-0.8b-448-rk3576.rknn"
if not artifact.is_file() or artifact.stat().st_size < 1000000:
    raise SystemExit("conversion did not produce a valid-size artifact")
output = ROOT / "models/assets/qwen" / name
output.parent.mkdir(parents=True, exist_ok=True)
import shutil

shutil.copyfile(artifact, output)
sha = hashlib.sha256()
with output.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1048576), b""):
        sha.update(chunk)
versions = subprocess.check_output([str(python), "-m", "pip", "freeze"], text=True)
(output.with_suffix(output.suffix + ".conversion.json")).write_text(
    json.dumps(
        {
            "sdk_revision": REV,
            "source_revision": "2fc06364715b967f1860aea9cf38778875588b17",
            "stage": a.stage,
            "sha256": sha.hexdigest(),
            "python_packages": versions.splitlines(),
            "runtime_check_required": True,
        },
        indent=2,
    )
)
print(output)
