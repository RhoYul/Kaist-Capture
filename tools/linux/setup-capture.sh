#!/usr/bin/env bash
# PhysicalAI KAIST Platform — 촬영 PC 설치 (Linux · 촬영 + export + 관제를 한 PC 에서). Windows 의 Setup-CapturePC / Setup-ControlPC 와 같은 일.
#   tools/linux/setup-capture.sh --nas-root /저장소/rgbd_raw [--python /경로/python3.10] [--wheels /경로/wheels]
#                                [--station 이름] [--data-dir /경로] [--no-sudo]
#   1) .venv 만들기 — --wheels 폴더에서만 설치 (인터넷 불필요). --wheels 가 없으면 PyPI 에서 받는다 (인터넷 필요)
#   2) 설정 만들기 (production-config · control-config) — 이미 있으면 그대로 둔다
#      export 서버는 이 PC 전용(127.0.0.1) — 관제 서버도 같은 PC 라 방화벽을 열 필요가 없다
#   3) RealSense 권한(udev) · USB 버퍼(usbfs 1000MB) — sudo 필요 (--no-sudo 면 건너뜀)
#   4) 로그온 때 자동 실행 (systemd --user)   5) 서버 켜기 → 관제 화면에 이 PC 등록
# 다시 실행해도 된다 (만든 환경 · 설정은 그대로 쓴다).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
BASEPY="" WHEELS="" STATION="" NAS="" DATA="$ROOT/runtime-real" SUDO=1
while [ $# -gt 0 ]; do
  case "$1" in
    --python) BASEPY=$2; shift 2 ;;
    --wheels) WHEELS=$2; shift 2 ;;
    --station) STATION=$2; shift 2 ;;
    --nas-root) NAS=$2; shift 2 ;;
    --data-dir) DATA=$2; shift 2 ;;
    --no-sudo) SUDO=0; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "모르는 옵션: $1" >&2; exit 2 ;;
  esac
done
say() { printf '\033[36m%s\033[0m\n' "$*"; }
ok() { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
say "=================================================="
say " PhysicalAI KAIST Platform 촬영 PC 설치 (Linux)  ($ROOT)"
say "=================================================="

# ── 1. Python ──
CHECK="import fastapi,uvicorn,httpx,numpy,cv2,scipy.spatial,pyrealsense2,sqlite3; assert hasattr(cv2,'aruco') and hasattr(cv2.aruco,'CharucoDetector'); print('ok')"
PY="$ROOT/.venv/bin/python"
if [ -x "$PY" ] && [ "$("$PY" -c "$CHECK" 2>/dev/null)" = ok ]; then
  ok "[1/5] Python 준비됨: $PY"
else
  if [ -z "$BASEPY" ]; then
    for c in "$ROOT"/python-dist/*/bin/python3.10 "$(command -v python3.10 || true)"; do [ -n "$c" ] && [ -x "$c" ] && { BASEPY=$c; break; }; done
  fi
  [ -n "$BASEPY" ] && [ -x "$BASEPY" ] || die "[1/5] Python 3.10 이 없습니다. --python 으로 지정하세요 (오프라인 묶음의 python-dist 폴더)."
  [ -z "$WHEELS" ] && [ -d "$ROOT/wheels" ] && WHEELS="$ROOT/wheels"
  say "[1/5] .venv 만들기 ($BASEPY)"
  rm -rf "$ROOT/.venv"
  "$BASEPY" -m venv "$ROOT/.venv"
  if [ -n "$WHEELS" ]; then
    "$PY" -m pip install -q --no-index --find-links "$WHEELS" -r tools/linux/requirements-linux.lock
  else
    warn "      --wheels 가 없어 PyPI 에서 받습니다 (인터넷 필요)"
    "$PY" -m pip install -q -r tools/linux/requirements-linux.lock
  fi
  [ "$("$PY" -c "$CHECK" 2>&1)" = ok ] || { "$PY" -c "$CHECK" || true; die "      패키지 설치 후에도 확인에 실패했습니다. 위 오류를 확인하세요."; }
  ok "      설치 완료: $PY"
fi

# ── 2. 설정 ──
CFG="$ROOT/production-config"
if [ -f "$CFG/capture.json" ] && [ -f "$CFG/export.json" ]; then
  ok "[2/5] 설정이 이미 있습니다 — 그대로 씁니다 ($CFG)"
else
  say "[2/5] 설정 만들기"
  [ -n "$NAS" ] || die "      --nas-root (검수 플랫폼이 읽는 rgbd_raw 폴더) 를 지정하세요"
  [ -d "$NAS" ] || die "      저장 폴더가 없습니다: $NAS"
  [ -w "$NAS" ] || die "      저장 폴더에 쓸 수 없습니다: $NAS"
  mkdir -p "$DATA"
  args=(init --directory production-config --nas-root "$(cd "$NAS" && pwd)" --data-dir "$(cd "$DATA" && pwd)")
  [ -n "$STATION" ] && args+=(--station "$STATION")
  "$PY" -m studio "${args[@]}"
fi
if [ -f "$ROOT/control-config/control.json" ]; then ok "      관제 설정 있음"; else "$PY" -m studio init-control --directory control-config; fi
chmod 600 "$CFG"/*.json "$ROOT"/control-config/*.json 2>/dev/null || true

# ── 3. 카메라 권한 · USB 버퍼 (sudo) ──
if [ "$SUDO" = 1 ]; then
  say "[3/5] RealSense 권한 · USB 버퍼 (sudo 암호를 물을 수 있습니다)"
  sudo install -m 644 tools/linux/99-realsense-libusb.rules /etc/udev/rules.d/99-realsense-libusb.rules
  sudo udevadm control --reload-rules && sudo udevadm trigger || true
  sudo usermod -aG plugdev,video "$USER" || true
  # 카메라 4대 × 848x480@30 컬러+깊이는 기본 usbfs 버퍼(16MB)로는 프레임이 빠진다 → 1000MB (부팅마다 적용)
  sudo tee /etc/systemd/system/kaist-usbfs.service >/dev/null <<'EOF'
[Unit]
Description=USB buffer for 4x RealSense (usbfs_memory_mb=1000)
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo 1000 > /sys/module/usbcore/parameters/usbfs_memory_mb'
[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload && sudo systemctl enable --now kaist-usbfs.service
  ok "      usbfs_memory_mb=$(cat /sys/module/usbcore/parameters/usbfs_memory_mb) · 카메라 USB 를 한 번 다시 꽂으세요"
else
  warn "[3/5] --no-sudo: udev · usbfs 설정을 건너뜁니다 (카메라가 안 열리면 sudo 로 다시 실행)"
fi

# ── 4. 로그온 때 자동 실행 (systemd --user) ──
UNIT_DIR="$HOME/.config/systemd/user"; UNIT="$UNIT_DIR/kaist-capture-studio.service"
USE_SYSTEMD=0
if systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=PhysicalAI KAIST capture platform (export 8766 · capture 8765 · control 8767)
After=default.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$ROOT/tools/linux/studio.sh start
ExecStop=$ROOT/tools/linux/studio.sh stop
TimeoutStartSec=180
TimeoutStopSec=120
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable kaist-capture-studio.service >/dev/null
  USE_SYSTEMD=1
  ok "[4/5] 로그온 자동 실행 등록 ($UNIT)"
else
  warn "[4/5] systemd --user 를 쓸 수 없습니다 — 직접 켭니다 (자동 실행 없음: 재부팅 뒤 tools/linux/studio.sh start)"
fi

# ── 5. 서버 켜기 · 관제 등록 ──
say "[5/5] 서버 켜기"
if [ "$USE_SYSTEMD" = 1 ]; then
  systemctl --user restart kaist-capture-studio.service || { tools/linux/studio.sh status || true; die "서버를 켜지 못했습니다. logs/*.err.log 를 확인하세요."; }
else
  tools/linux/studio.sh start || die "서버를 켜지 못했습니다. logs/*.err.log 를 확인하세요."
fi
tools/linux/studio.sh register-local || warn "관제 화면 등록은 나중에: tools/linux/studio.sh register-local"
tools/linux/studio.sh status || true
pw() { "$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }
echo
say "=================================================="
say " 촬영 플랫폼 설치 완료"
echo "  촬영 화면 : http://localhost:8765   작업자 비밀번호: $(pw "$CFG/capture.json" worker_password)"
echo "  관제 화면 : http://localhost:8767   비밀번호: $(pw "$ROOT/control-config/control.json" admin_password)"
echo "  저장 폴더 : $(pw "$CFG/export.json" nas_root)"
warn "  카메라 권한이 방금 바뀌었으면 로그아웃 후 다시 로그인하세요. 처음 쓰는 PC 는 캘리브레이션을 새로 하세요."
say "=================================================="
