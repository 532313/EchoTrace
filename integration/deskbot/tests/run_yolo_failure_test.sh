#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 /path/to/isolated/yolov5_demo/cpp" >&2
    exit 2
fi

cpp_dir=$1
test_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM

"${CXX:-g++}" -std=c++11 -DRV1106_1103 -g -O1 \
    -fsanitize=address,undefined -fno-omit-frame-pointer \
    -I"$cpp_dir" -I"$cpp_dir/utils" \
    -I"$cpp_dir/3rdparty/rknpu2/include" \
    "$test_dir/yolo_failure_test.cpp" \
    "$cpp_dir/rknpu2/yolov5_rv1106_1103.cc" \
    -o "$build_dir/yolo_failure_test"
ASAN_OPTIONS=detect_leaks=1 "$build_dir/yolo_failure_test"
