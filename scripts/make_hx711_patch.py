#!/usr/bin/env python3
"""Generate a small reviewable patch against the exact existing Echo kernel."""
import argparse, difflib, hashlib, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument(
    "--kernel",
    type=Path,
    default=Path(
        "/Users/gsy/Documents/echo-mate/Echo-Mate/SDK/rv1106-sdk/sysdrv/source/kernel"
    ),
)
a = p.parse_args()
name = "drivers/iio/adc/hx711.c"
original = (a.kernel / name).read_bytes()
before = original.decode().replace("\r\n", "\n")
start = before.index("static irqreturn_t hx711_trigger(")
end = before.index("\nstatic ssize_t hx711_scale_available_show", start)
function = before[start:end]
function = function.replace("int i, j = 0;", "int i, ret, j = 0;")
function = function.replace(
    "if (!test_bit(i, indio_dev->active_scan_mask))",
    "if (!test_bit(i, indio_dev->active_scan_mask) ||\n\t\t    indio_dev->channels[i].type != IIO_VOLTAGE)",
)
old = "\t\thx711_data->buffer[j] = hx711_reset_read(hx711_data,\n\t\t\t\t\tindio_dev->channels[i].channel);\n\t\tj++;"
new = "\t\tret = hx711_reset_read(hx711_data,\n\t\t\t\t       indio_dev->channels[i].channel);\n\t\tif (ret < 0)\n\t\t\tgoto out_unlock;\n\t\thx711_data->buffer[j++] = ret;"
if old not in function:
    raise SystemExit("upstream changed; review the trigger before applying")
function = function.replace(old, new).replace(
    "\n\tmutex_unlock(&hx711_data->lock);",
    "\nout_unlock:\n\tmutex_unlock(&hx711_data->lock);",
)
after = before[:start] + function + before[end:]
patch = "".join(
    difflib.unified_diff(
        before.splitlines(True),
        after.splitlines(True),
        fromfile="a/" + name,
        tofile="b/" + name,
    )
)
out = ROOT / "bsp/rv1106/0001-hx711-reject-error-samples.patch"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(patch)
(ROOT / "bsp/rv1106/hx711-baseline.json").write_text(
    json.dumps(
        {
            "path": name,
            "source_sha256": hashlib.sha256(original).hexdigest(),
            "patched_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "kernel": "5.10.110",
            "upstream_function": before[start:end],
            "patched_function": function,
        },
        indent=2,
    )
)
print(out)
