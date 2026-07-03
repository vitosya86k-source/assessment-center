#!/bin/bash
set -e
cd "$(dirname "$0")"
exec ./venv_new/bin/python pose_live.py --exercise "${1:-чай}"
