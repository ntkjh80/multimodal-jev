#!/bin/sh
# 파일 위치로 이동하므로 Finder에서도 같은 경로를 사용합니다.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || exit 1
fi
exec .venv/bin/python run.py --open
