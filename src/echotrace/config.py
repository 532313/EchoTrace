from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    token: str = ""
    host: str = "127.0.0.1"
    port: int = 18081
    ffmpeg: str = "ffmpeg"
    model_config: Path = Path("models/config.json")
    max_jobs: int = 32
    min_free_bytes: int = 256 * 1024 * 1024
    cache_limit_bytes: int = 2 * 1024 * 1024 * 1024
    frame_worker: str = os.environ.get(
        "ECHOTRACE_FRAME_WORKER", "build/rk3576/echotrace-frame"
    )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            root=Path(os.environ.get("ECHOTRACE_DATA", "runtime")).resolve(),
            token=os.environ.get("ECHOTRACE_TOKEN", ""),
            host=os.environ.get("ECHOTRACE_HOST", "127.0.0.1"),
            port=int(os.environ.get("ECHOTRACE_PORT", "18081")),
            ffmpeg=os.environ.get("ECHOTRACE_FFMPEG", "ffmpeg"),
            model_config=Path(
                os.environ.get("ECHOTRACE_MODELS", "models/config.json")
            ).resolve(),
        )

    def prepare(self) -> None:
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.token:
            raise ValueError(
                "a nonempty ECHOTRACE_TOKEN is required on a network interface"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("media", "frames", "exports", "staging"):
            (self.root / name).mkdir(exist_ok=True)

    def models(self) -> dict:
        if not self.model_config.exists():
            return {}
        data = json.loads(self.model_config.read_text(encoding="utf-8"))
        for role, options in data.items():
            if isinstance(options, dict):
                for key, value in list(options.items()):
                    if (
                        key
                        in (
                            "model",
                            "tokens",
                            "vocab",
                            "lexicon",
                            "vocoder",
                            "worker",
                            "vision",
                            "language",
                            "data_dir",
                            "dict_dir",
                            "test_audio",
                        )
                        and value
                    ):
                        path = Path(value)
                        options[key] = str(
                            path
                            if path.is_absolute()
                            else (self.model_config.parent / path).resolve()
                        )
                if options.get("rule_fsts"):
                    options["rule_fsts"] = ",".join(
                        str(
                            Path(v)
                            if Path(v).is_absolute()
                            else (self.model_config.parent / v).resolve()
                        )
                        for v in options["rule_fsts"].split(",")
                    )
        return data
