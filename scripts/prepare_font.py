#!/usr/bin/env python3
"""Build a complete Chinese LVGL font; the bundled 1000-glyph font omits UI words."""
import hashlib, json, subprocess, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "build/vendor/fonts"
target.mkdir(parents=True, exist_ok=True)
lockpath = ROOT / "fonts.lock.json"
if lockpath.exists():
    lock = json.loads(lockpath.read_text())
    revision = lock["revision"]
else:
    revision = json.load(
        urllib.request.urlopen(
            "https://api.github.com/repos/notofonts/noto-cjk/commits/main"
        )
    )["sha"]
    lock = {"revision": revision, "license": "SIL Open Font License 1.1"}
for file, source in [
    ("NotoSansSC-Regular.otf", "Sans/SubsetOTF/SC/NotoSansSC-Regular.otf"),
    ("LICENSE", "Sans/LICENSE"),
]:
    out = target / file
    if not out.exists():
        urllib.request.urlretrieve(
            "https://raw.githubusercontent.com/notofonts/noto-cjk/"
            + revision
            + "/"
            + source,
            out,
        )
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    if file in lock and lock[file] != sha:
        raise SystemExit("font checksum changed")
    lock[file] = sha
subprocess.run(
    [
        "npx",
        "--yes",
        "--cache",
        str(ROOT / "build/npm-cache"),
        "lv_font_conv@1.5.3",
        "--font",
        str(target / "NotoSansSC-Regular.otf"),
        "--size",
        "16",
        "--bpp",
        "4",
        "--format",
        "lvgl",
        "--range",
        "0x20-0x7f,0x2000-0x206f,0x3000-0x303f,0x4e00-0x9fff,0xff00-0xffef",
        "--symbols",
        "●Ⅱ·…",
        "--no-compress",
        "--lv-font-name",
        "echotrace_font",
        "--lv-fallback",
        "lv_font_montserrat_14",
        "--output",
        str(target / "echotrace_font.c"),
    ],
    check=True,
    cwd=ROOT,
)
lock["converter"] = "lv_font_conv@1.5.3"
lock["output_sha256"] = hashlib.sha256(
    (target / "echotrace_font.c").read_bytes()
).hexdigest()
lockpath.write_text(json.dumps(lock, indent=2))
print(target / "echotrace_font.c")
