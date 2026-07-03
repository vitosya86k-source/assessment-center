#!/bin/bash
set -e
cd "$(dirname "$0")"
exec ./emo_venv/bin/python emotion_live.py --exercise "${1:-чай}"
