#!/usr/bin/env bash
# assessment-center: FastAPI + Telegram webhook (sleep-friendly, как HRV_backend).
set -euo pipefail
exec uvicorn combo_server:app --host 0.0.0.0 --port "${PORT:-8000}"
