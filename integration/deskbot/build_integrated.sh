#!/bin/sh
# Build EchoTrace inside an isolated copy of the actual WSL DeskBot sources.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 NEW_EMPTY_WORK_DIRECTORY" >&2
    exit 2
fi

work=$1
demo_root=${ECHOMATE_DEMO_ROOT:-/home/guan/Echo-Mate/Demo}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
trace_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
baseline=$script_dir/baseline.json

if [ -e "$work" ]; then
    echo "refusing to overwrite existing work directory: $work" >&2
    exit 2
fi
if [ ! -f "$trace_root/build/vendor/fonts/echotrace_font.c" ]; then
    echo "prepare EchoTrace's Chinese font before building" >&2
    exit 2
fi

python3 - "$baseline" "$demo_root" <<'PY'
import hashlib, json, pathlib, sys
baseline = json.loads(pathlib.Path(sys.argv[1]).read_text())
demo = pathlib.Path(sys.argv[2])
for key, directory in (("files", demo / "DeskBot_demo"),
                       ("yolov5_demo_files", demo / "yolov5_demo")):
    for relative, expected in baseline[key].items():
        source = directory / relative
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"source changed: {source}\n"
                             f"expected {expected}, got {actual}")
print("original DeskBot and YOLO sources match baseline")
PY

mkdir -p "$work"
rsync -a --exclude '/build/' --exclude '/bin/' --exclude '/.git/' \
    "$demo_root/DeskBot_demo/" "$work/DeskBot_demo/"
rsync -a --exclude '/model/' --exclude '/cpp/3rdparty/' \
    --exclude '/cpp/lib/' --exclude '/.git/' \
    "$demo_root/yolov5_demo/" "$work/yolov5_demo/"
ln -s "$demo_root/AIChat_demo" "$work/AIChat_demo"
ln -s "$demo_root/yolov5_demo/model" "$work/yolov5_demo/model"
ln -s "$demo_root/yolov5_demo/cpp/3rdparty" "$work/yolov5_demo/cpp/3rdparty"
ln -s "$demo_root/yolov5_demo/cpp/lib" "$work/yolov5_demo/cpp/lib"

patch -d "$work/DeskBot_demo" -p1 < "$script_dir/0001-echotrace-app.patch"
patch -d "$work" -p1 < "$script_dir/0002-ai-camera-backend.patch"
patch -d "$work/DeskBot_demo" -p1 < "$script_dir/0003-ai-camera-page.patch"
patch -d "$work/DeskBot_demo" -p1 < "$script_dir/0004-app-launch.patch"
patch -d "$work/DeskBot_demo" -p1 < "$script_dir/0005-partial-display.patch"

sh "$script_dir/tests/run_yolo_failure_test.sh" \
    "$work/yolov5_demo/cpp" > "$work/yolo-failure-test.log" 2>&1

cmake -S "$work/DeskBot_demo" -B "$work/DeskBot_demo/build-echotrace" \
    -DTARGET_ARM=ON -DECHOTRACE_APP_ROOT="$trace_root" \
    -DCMAKE_BUILD_TYPE=Release > "$work/configure.log" 2>&1
if ! cmake --build "$work/DeskBot_demo/build-echotrace" \
        -j "${ECHOTRACE_BUILD_JOBS:-4}" > "$work/build.log" 2>&1; then
    tail -n 80 "$work/build.log" >&2
    exit 1
fi
cp "$demo_root/DeskBot_demo/bin/IU.jpg" \
    "$work/DeskBot_demo/build-echotrace/bin/IU.jpg"
sha256sum "$work/DeskBot_demo/build-echotrace/bin/main"
echo "isolated build: $work/DeskBot_demo/build-echotrace/bin"
