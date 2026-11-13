#!/usr/bin/env python3
"""Export the pinned BAAI model on a build host, then dynamic-quantize linear layers."""
import argparse, hashlib, json
from pathlib import Path
import torch
from transformers import AutoModel, AutoTokenizer
from onnxruntime.quantization import quantize_dynamic, QuantType

p = argparse.ArgumentParser()
p.add_argument("--root", type=Path, default=Path("models"))
a = p.parse_args()
root = a.root.resolve()
src = root / "assets/bge-source"
out = root / "assets/bge"
out.mkdir(parents=True, exist_ok=True)
model = AutoModel.from_pretrained(src, local_files_only=True).eval()
tokenizer = AutoTokenizer.from_pretrained(src, local_files_only=True)


class Encoder(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, input_ids, attention_mask, token_type_ids):
        return self.m(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
        ).last_hidden_state


x = tokenizer(
    ["排线接触不良"],
    padding="max_length",
    max_length=128,
    truncation=True,
    return_tensors="pt",
)
plain = out / "model.onnx"
quant = out / "model.int8.onnx"
with torch.no_grad():
    torch.onnx.export(
        Encoder(model),
        (x["input_ids"], x["attention_mask"], x["token_type_ids"]),
        str(plain),
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["last_hidden_state"],
        dynamic_axes={
            n: {0: "batch"}
            for n in [
                "input_ids",
                "attention_mask",
                "token_type_ids",
                "last_hidden_state",
            ]
        },
        opset_version=17,
        dynamo=False,
    )
quantize_dynamic(
    str(plain),
    str(quant),
    weight_type=QuantType.QInt8,
    op_types_to_quantize=["MatMul", "Gemm"],
)
(out / "vocab.txt").write_bytes((src / "vocab.txt").read_bytes())
digest = hashlib.sha256(quant.read_bytes()).hexdigest()
configpath = root / "config.json"
config = json.loads(configpath.read_text()) if configpath.exists() else {}
config["embedding"] = {
    "model": "assets/bge/model.int8.onnx",
    "vocab": "assets/bge/vocab.txt",
    "revision": "BAAI-7999e1d3359715c523056ef9478215996d62a620-int8-" + digest[:12],
    "max_length": 128,
    "pooling": "CLS",
    "normalize": True,
}
configpath.write_text(json.dumps(config, ensure_ascii=False, indent=2))
(out / "conversion.json").write_text(
    json.dumps(
        {
            "source_revision": "7999e1d3359715c523056ef9478215996d62a620",
            "torch": torch.__version__,
            "sha256": digest,
            "quantization": "dynamic per-tensor signed INT8 MatMul/Gemm",
            "opset": 17,
        },
        indent=2,
    )
)
print(quant)
