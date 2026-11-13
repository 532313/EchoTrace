#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
git rev-parse --show-toplevel >/dev/null
git config --local core.hooksPath .githooks
git config --local core.autocrlf false
git config --local pull.ff only
git config --local fetch.prune true
echo 'EchoTrace repository hooks and fast-forward pull policy installed locally.'
git status --short --branch
