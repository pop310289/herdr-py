#!/bin/bash
# usage: build-image.sh /path/to/opencode-binary   (OpenCode 1.18.32, linux, glibc build, same CPU architecture as Docker)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ctx="$(mktemp -d "$HOME/.cache/herdr-p23-image.XXXX")"
trap 'rm -rf "$ctx"' EXIT
cp "$here/Dockerfile" "$ctx/"
cp "$1" "$ctx/opencode"
chmod 755 "$ctx/opencode"
docker build -q -t herdr-py/p23:rhel8 "$ctx"
