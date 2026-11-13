"""Local-only model adapters. Missing models are reported, never synthesized."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np


class ModelUnavailable(RuntimeError):
    pass


class LocalAI:
    def __init__(self, options):
        self.options = options
        self._asr = self._tts = self._embed = self._tokenizer = None
        self.asr_lock = threading.RLock()
        self.embedding_lock = threading.RLock()
        self.tts_lock = threading.RLock()
        self.vlm_lock = threading.RLock()
        self.verified = set()
        self._vad = None
        self._hashes = {}

    def _files(self, role, keys, verify=True):
        config = self.options.get(role, {})
        missing = [
            key
            for key in keys
            if not config.get(key) or not Path(config[key]).is_file()
        ]
        if missing:
            raise ModelUnavailable(f"{role}: local files missing: {', '.join(missing)}")
        if verify:
            for key, expected in config.get("sha256", {}).items():
                if key not in keys:
                    continue
                path = Path(config[key])
                stat = path.stat()
                fingerprint = (str(path), stat.st_size, stat.st_mtime_ns)
                if self._hashes.get((role, key)) != fingerprint:
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(65536), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != expected:
                        self.verified.discard(role)
                        raise ModelUnavailable(
                            f"{role}.{key}: sealed model checksum changed"
                        )
                    self._hashes[(role, key)] = fingerprint
        return config

    def status(self):
        requirements = {
            "asr": ("model", "tokens"),
            "embedding": ("model", "vocab"),
            "tts": ("model", "vocoder", "tokens", "lexicon"),
            "vad": ("model",),
            "vlm": ("worker", "vision", "language"),
        }
        result = {}
        for role, keys in requirements.items():
            try:
                self._files(role, keys, verify=False)
                result[role] = {
                    "status": "files_present",
                    "verified_runtime": role in self.verified,
                }
            except ModelUnavailable as e:
                result[role] = {"status": "unavailable", "reason": str(e)}
        return result

    def transcribe(self, path, start_sample=0, end_sample=None):
        config = self._files("asr", ("model", "tokens"))
        try:
            import sherpa_onnx
        except ImportError as e:
            raise ModelUnavailable("install the local sherpa-onnx runtime") from e
        with self.asr_lock:
            if self._asr is None:
                self._asr = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=config["model"],
                    tokens=config["tokens"],
                    num_threads=2,
                    use_itn=True,
                    language="zh",
                    debug=False,
                    provider="cpu",
                )
            with wave.open(str(path), "rb") as f:
                if (f.getnchannels(), f.getsampwidth(), f.getframerate()) != (
                    1,
                    2,
                    16000,
                ):
                    raise ValueError("ASR requires 16kHz mono signed 16-bit WAV")
                if f.getnframes() > 320000:
                    raise ValueError(
                        "ASR utterance exceeds the bounded 20-second window"
                    )
                audio = (
                    np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype(
                        np.float32
                    )
                    / 32768.0
                )
            if len(audio) > 16000 * 20:
                raise ValueError("ASR utterance exceeds the bounded 20-second window")
            audio = audio[start_sample:end_sample]
            if not len(audio):
                return "", config.get("revision", "local-unsealed")
            stream = self._asr.create_stream()
            stream.accept_waveform(16000, audio)
            self._asr.decode_stream(stream)
            self.verified.add("asr")
            return stream.result.text.strip(), config.get("revision", "local-unsealed")

    def speech_regions(self, path):
        config = self._files("vad", ("model",))
        with self.asr_lock:
            if self._vad is None:
                self._vad = SileroVAD(config["model"])
            self._vad.reset()
            with wave.open(str(path), "rb") as stream:
                if (
                    stream.getnchannels(),
                    stream.getsampwidth(),
                    stream.getframerate(),
                ) != (1, 2, 16000):
                    raise ValueError("VAD requires 16kHz mono PCM")
                if stream.getnframes() > 320000:
                    raise ValueError("audio exceeds the bounded 20-second window")
                audio = (
                    np.frombuffer(
                        stream.readframes(stream.getnframes()), dtype="<i2"
                    ).astype(np.float32)
                    / 32768
                )
            regions, start, last_voice = [], None, 0
            for offset in range(0, len(audio), 512):
                chunk = np.pad(
                    audio[offset : offset + 512], (0, max(0, offset + 512 - len(audio)))
                )
                if self._vad.probability(chunk) >= 0.5:
                    if start is None:
                        start = max(0, offset - 2400)
                    last_voice = offset + 512
                elif start is not None and offset - last_voice >= 4800:
                    if last_voice - start >= 4000:
                        regions.append((start, min(len(audio), last_voice + 2400)))
                    start = None
            if start is not None and last_voice - start >= 4000:
                regions.append((start, min(len(audio), last_voice + 2400)))
            self.verified.add("vad")
            return regions

    def embed(self, texts):
        config = self._files("embedding", ("model", "vocab"))
        try:
            import onnxruntime as ort
            from tokenizers import BertWordPieceTokenizer
        except ImportError as e:
            raise ModelUnavailable(
                "embedding needs local onnxruntime and tokenizers"
            ) from e
        with self.embedding_lock:
            if self._embed is None:
                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                self._embed = ort.InferenceSession(
                    config["model"], options, providers=["CPUExecutionProvider"]
                )
                self._tokenizer = BertWordPieceTokenizer(
                    config["vocab"], lowercase=True
                )
                self._tokenizer.enable_truncation(max_length=128)
                self._tokenizer.enable_padding(length=128)
            encoded = self._tokenizer.encode_batch(texts)
            ids = np.array([x.ids for x in encoded], dtype=np.int64)
            masks = np.array([x.attention_mask for x in encoded], dtype=np.int64)
            inputs = {
                "input_ids": ids,
                "attention_mask": masks,
                "token_type_ids": np.zeros_like(ids),
            }
            outputs = self._embed.run(
                None, {i.name: inputs[i.name] for i in self._embed.get_inputs()}
            )
            vectors = outputs[0]
            if vectors.ndim == 3:
                vectors = vectors[:, 0, :]
            if vectors.ndim != 2 or vectors.shape[1] != 512:
                raise ValueError(
                    "expected BGE-small-zh CLS embeddings with dimension 512"
                )
            self.verified.add("embedding")
            return vectors / np.maximum(
                np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12
            )

    def caption(
        self,
        image_path,
        prompt="只描述图像中最明显且可见的场景或物体，用一句不超过30字的中文。不要预设这是工作台，不猜用途、型号或操作原因；无法确认就说明无法确认。",
    ):
        config = self._files("vlm", ("worker", "vision", "language"))
        command = [
            config["worker"],
            "--vision",
            config["vision"],
            "--language",
            config["language"],
            "--platform",
            "rk3576",
            "--max-context",
            "2048",
            "--max-tokens",
            "64",
        ]
        # Each worker is a process boundary. Killing it never stops the edge recorder.
        with self.vlm_lock:
            try:
                result = subprocess.run(
                    command,
                    input=json.dumps(
                        {"image": str(image_path), "prompt": prompt}, ensure_ascii=False
                    )
                    + "\n",
                    text=True,
                    capture_output=True,
                    timeout=90,
                    check=True,
                    env={**os.environ, "OMP_NUM_THREADS": "2"},
                )
            except (OSError, subprocess.SubprocessError) as e:
                raise ModelUnavailable(f"VLM worker failed: {str(e)[:200]}") from e
            lines = [
                line
                for line in result.stdout.splitlines()
                if line.startswith('{"text":')
            ]
            if not lines:
                raise RuntimeError("worker did not return a JSON result")
            self.verified.add("vlm")
            return json.loads(lines[-1])["text"][:2000], config.get(
                "revision", "local-unsealed"
            )

    def speak(self, text, destination):
        config = self._files("tts", ("model", "vocoder", "tokens", "lexicon"))
        try:
            import sherpa_onnx
        except ImportError as e:
            raise ModelUnavailable("install the local sherpa-onnx runtime") from e
        with self.tts_lock:
            if self._tts is None:
                matcha = sherpa_onnx.OfflineTtsMatchaModelConfig(
                    acoustic_model=config["model"],
                    vocoder=config["vocoder"],
                    tokens=config["tokens"],
                    lexicon=config["lexicon"],
                    data_dir=config.get("data_dir", ""),
                    dict_dir=config.get("dict_dir", ""),
                )
                model = sherpa_onnx.OfflineTtsModelConfig(
                    matcha=matcha, num_threads=2, debug=False, provider="cpu"
                )
                self._tts = sherpa_onnx.OfflineTts(
                    sherpa_onnx.OfflineTtsConfig(
                        model=model, rule_fsts=config.get("rule_fsts", "")
                    )
                )
            audio = self._tts.generate(text[:400], sid=0, speed=1.0)
            # Resample the local synthesis to the negotiated 16kHz edge format.
            target = int(len(audio.samples) * 16000 / audio.sample_rate)
            pcm = np.interp(
                np.arange(target) * audio.sample_rate / 16000,
                np.arange(len(audio.samples)),
                audio.samples,
            )
            with wave.open(str(destination), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(
                    (np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes()
                )
            self.verified.add("tts")
        return destination


class SileroVAD:
    """Silero v5/v6 ONNX state. Validates the actual graph interface first."""

    def __init__(self, path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self.model = ort.InferenceSession(
            str(path), options, providers=["CPUExecutionProvider"]
        )
        if {x.name for x in self.model.get_inputs()} != {"input", "state", "sr"}:
            raise ValueError(
                "this adapter requires Silero's state/input/sr ONNX interface"
            )
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def reset(self):
        self.state.fill(0)
        self.context.fill(0)

    def probability(self, samples):
        if len(samples) != 512:
            raise ValueError("Silero requires exactly 512 samples at 16kHz")
        combined = np.concatenate(
            [self.context, samples[np.newaxis].astype(np.float32)], axis=1
        )
        result, self.state = self.model.run(
            None,
            {
                "input": combined,
                "state": self.state,
                "sr": np.array(16000, dtype=np.int64),
            },
        )
        self.context = combined[:, -64:]
        return float(result.reshape(-1)[0])
