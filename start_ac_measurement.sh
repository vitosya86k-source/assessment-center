#!/bin/bash
set -e
cd "$(dirname "$0")"
exec ./venv_new/bin/python rppg_screen.py --exercise "${1:-чай}"
