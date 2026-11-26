#!/usr/bin/env bash
# PhysicalAI KAIST Platform 켜기 · 끄기 · 상태 (Linux) — Windows 의 Start/Stop/Restart/Status-*.cmd 와 같다.
#   tools/linux/studio.sh start|stop|restart|status [--role capture|export|control]
#   tools/linux/studio.sh register-local      # 관제 화면에 이 PC 의 export 서버 등록
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PY=${STUDIO_PYTHON:-$ROOT/.venv/bin/python}
[ -x "$PY" ] || { echo "Python 환경이 없습니다: $PY (tools/linux/setup-capture.sh 를 먼저 실행)" >&2; exit 1; }
cd "$ROOT"
exec "$PY" tools/studioctl.py "$@"
