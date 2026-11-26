"""상시 열린 RealSense 카메라 + 즉시 녹화.

예전 흐름: 미리보기(컬러 15fps)를 닫고 → 녹화용(컬러+depth 30fps)으로 다시 열기 → 약 9초 지연, 카메라마다 시작 시각 차이.
이 모듈: 카메라 4대를 처음부터 녹화와 같은 설정으로 계속 열어 두고, 미리보기도 같은 스트림을 쓴다.
녹화 버튼을 누르면 그 순간(같은 호스트 시각)부터 4대가 쓰기 쪽으로 프레임을 넘긴다.

원본과 같게 둔 것 (vendor/realsense.py):
- 스트림 설정: bgr8 · z16, rs.align(rs.stream.color). 해상도 · FPS 는 원본 상수(vendor COLOR_W …) 를 쓰되,
  capture.json 의 capture_width · capture_height · capture_fps 로 실행 중에 바꾼다 (configure, 원본 파일은 그대로).
  기본 848×480@30 — PhysicalAI MANO 손 피팅이 848×480 기준. 848×480 은 USB 3 연결이어야 나온다.
- 프레임 처리: align.process → color/depth .copy() → (frame_idx, color, depth, hw_ts, host_t) 를 60프레임 큐에 put_nowait, 가득 차면 드롭 · 경고
- 쓰기: 원본 RealSenseRecorder 의 쓰기 스레드(_write_loop → mp4v mp4 · npy · timestamps.csv)를 그대로 실행
- intrinsics.json: 원본과 같은 키 · 형식

원본과 다른 것: 프레임 번호. 받은 순서가 아니라 4대 공통 시간 격자(_Grid)의 슬롯 번호로 저장한다
→ 같은 번호 = 4대 같은 시각, 카메라마다 정확히 같은 장수 (seconds × FPS).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import cv2

from .vendor import realsense as rv

logger = logging.getLogger(__name__)
PREVIEW_FPS = 15
JPEG_QUALITY = 80          # 원본 미리보기와 같은 품질 (캘리브레이션 보드 캡처도 이 JPEG 를 쓴다)
# 카메라 SDK 안에 두는 프레임 버퍼 (장). pipeline.wait_for_frames 는 버퍼가 1장뿐이라, 파이썬 쪽이 33ms 넘게 늦게 꺼내면
# (화면 응답 · 정렬 · 저장이 같은 프로세스에서 돌아 순서를 기다리는 동안) 그 사이 프레임을 SDK 가 버린다 → '받지 못한 프레임'.
# SDK 가 자기 스레드로 이 버퍼에 쌓아 두면 파이썬이 잠깐 늦어도 버려지지 않는다. 0 이면 예전 방식 (capture.json 의 sdk_queue)
SDK_QUEUE = 32
# RGB 화이트밸런스 (capture.json 의 white_balance).
#   "lock" = 카메라를 연 뒤 자동 화이트밸런스가 자리 잡으면(WB_SETTLE_S 초) 4대 값의 가운데 값으로 4대를 같이 고정 (기본)
#   숫자   = 그 색온도(K)로 4대 고정 (예: 4600) · "auto" = 예전처럼 카메라마다 자동
# 자동이면 카메라마다 · 장면마다(작업자 · 큰 물체) 색이 따로 바뀐다 → 4대 색이 맞지 않고 에피소드마다 색이 달라진다.
WHITE_BALANCE = "lock"
WB_SETTLE_S = 3.0
_wb_locked: Optional[int] = None   # "lock" 으로 정해진 값 (다시 열린 카메라에도 같은 값)
# RGB 노출 우선 (capture.json 의 auto_exposure_priority). 켜져 있으면 어두울 때 카메라가 노출 시간을 늘리려고
# 스스로 fps 를 낮춘다 (진단 실측: 끊김 없이 10초 290장 = 약 29fps). False = 끔 → 항상 설정 fps (기본)
AE_PRIORITY = False


def _rs():
    return rv._rs()


def _wb_target() -> Optional[int]:
    """지금 적용할 화이트밸런스 고정 값 (K). 자동이면 None."""
    wb = WHITE_BALANCE
    if isinstance(wb, (int, float)) and not isinstance(wb, bool):
        return int(wb)
    if isinstance(wb, str) and wb.strip().isdigit():
        return int(wb.strip())
    return _wb_locked if wb == "lock" else None


def _set_wb(rs, sensor, kelvin: int) -> int:
    """자동 화이트밸런스를 끄고 kelvin 으로 고정 (장치 범위 · 단위에 맞춘 값을 돌려준다)."""
    r = sensor.get_option_range(rs.option.white_balance)
    step = r.step or 1
    v = min(r.max, max(r.min, round((kelvin - r.min) / step) * step + r.min))
    sensor.set_option(rs.option.enable_auto_white_balance, 0)
    sensor.set_option(rs.option.white_balance, v)
    return int(v)


def _apply_color_options(rs, sensor, camera_id: str) -> str:
    """RGB 센서 설정: 노출 우선 · 화이트밸런스. 장치가 지원하지 않거나 실패해도 스트림은 그대로 (로그용 요약을 돌려준다)."""
    notes = []
    if sensor is None:
        return "RGB 센서 없음"
    try:
        if sensor.supports(rs.option.auto_exposure_priority):
            sensor.set_option(rs.option.auto_exposure_priority, 1 if AE_PRIORITY else 0)
            notes.append("노출 우선 " + ("켬" if AE_PRIORITY else "끔"))
    except Exception as e:
        logger.warning(f"[warm] {camera_id} 노출 우선 설정 실패 (그대로 계속): {e}")
    try:
        if sensor.supports(rs.option.white_balance) and sensor.supports(rs.option.enable_auto_white_balance):
            target = _wb_target()
            if target:
                notes.append(f"화이트밸런스 {_set_wb(rs, sensor, target)}K 고정")
            else:
                sensor.set_option(rs.option.enable_auto_white_balance, 1)
                notes.append("화이트밸런스 자동" + (" (고정 대기)" if WHITE_BALANCE == "lock" else ""))
    except Exception as e:
        logger.warning(f"[warm] {camera_id} 화이트밸런스 설정 실패 (그대로 계속): {e}")
    return " · ".join(notes)


_TOA = None


def _arrival(frames, now: float) -> float:
    """프레임이 PC 에 도착한 시각 (epoch 초) — SDK 가 프레임에 적어 둔 값(time_of_arrival).
    SDK 버퍼에서 파이썬이 늦게 꺼내면 '꺼낸 시각'은 여러 장이 한꺼번에 몰려 실제 도착 시각과 수백 ms 달라진다
    (timestamps.csv 의 host_time_s 가 흐트러져 QC 동기화 검사가 WARN 이 된다). 값을 못 읽거나 이상하면 꺼낸 시각(now)."""
    global _TOA
    try:
        if _TOA is None:
            _TOA = _rs().frame_metadata_value.time_of_arrival
        color = frames.get_color_frame()
        if color and color.supports_frame_metadata(_TOA):
            t = color.get_frame_metadata(_TOA) / 1000.0
            if now - 3.0 < t <= now + 0.05:
                return t
    except Exception:
        pass
    return now


def configure(width: int, height: int, fps: int) -> None:
    """촬영 해상도 · FPS 를 정한다 (카메라를 열기 전에). 원본 쓰기 스레드(_write_loop)도 같은 상수로 mp4 크기를 정하므로
    원본 모듈의 상수를 실행 중에 바꾼다 — 스트림 · mp4 · intrinsics 가 항상 같은 해상도가 된다."""
    rv.COLOR_W, rv.COLOR_H, rv.COLOR_FPS = int(width), int(height), int(fps)
    rv.DEPTH_W, rv.DEPTH_H, rv.DEPTH_FPS = int(width), int(height), int(fps)


def _device_info() -> Dict[str, dict]:
    """시리얼별 모델 이름 · USB 연결 종류 (예: '3.2', '2.1')."""
    out: Dict[str, dict] = {}
    try:
        rs = _rs()
        for d in rs.context().query_devices():
            def info(key):
                try: return d.get_info(key) if d.supports(key) else ""
                except Exception: return ""
            out[info(rs.camera_info.serial_number)] = {"model": info(rs.camera_info.name), "usb": info(rs.camera_info.usb_type_descriptor)}
    except Exception as e:
        logger.warning(f"[warm] 장치 정보 읽기 실패: {e}")
    return out


def usb_hint(usb: str) -> str:
    """USB 2.x 로 붙은 카메라 경고 (640×480 보다 큰 해상도는 USB 2 로 안 나온다)."""
    if usb and usb.startswith("2") and rv.COLOR_W > 640:
        return f"USB {usb} 로 연결됨 — {rv.COLOR_W}×{rv.COLOR_H} 을 낼 수 없습니다. USB 3 포트(파란색) · USB 3 케이블로 연결하세요"
    return ""


class _Grid:
    """4대 공통 시간 격자. 프레임 번호 = 시간 슬롯 (슬롯 k 의 시각 = 기준 시각 + k × 프레임 간격).

    카메라는 FPS 가 같아도 프레임을 만드는 순간이 서로 다르고, 받지 못한 프레임이 있으면 그 뒤 번호가 당겨져
    같은 번호가 카메라마다 다른 시각이 된다 (실측 30fps: 10초 300장 중 카메라마다 270~286장만 저장 → 뒤로 갈수록 어긋남).
    그래서 받은 순서가 아니라 카메라 시각(hw timestamp)으로 번호를 정한다.
    - 기준 시각 = 4대의 첫 프레임 중 가장 늦은 것 → 모든 카메라에 0번 프레임이 있다 (그보다 반 간격 넘게 이른 프레임은 버린다)
    - 프레임 번호 = round((프레임 시각 − 기준 시각) ÷ 간격) → 같은 번호의 4대 프레임은 서로 한 프레임 간격 안
    - 받지 못한 슬롯은 시간상 가장 가까운 프레임으로 채운다. timestamps.csv 에는 채운 프레임의 원래 시각을 쓴다
      (같은 hw_timestamp 가 연달아 나오면 채운 프레임) · 채운 수는 결과의 filled
    - total 장을 채우면 끝 → 카메라마다 정확히 같은 장수
    """

    def __init__(self, fps: int, cameras: int, total: Optional[int] = None) -> None:
        self.period = 1000.0 / max(1, int(fps))      # ms
        self.total = total                           # 카메라마다 저장할 장수 (None = 정하지 않음)
        self.tail_max = max(1, int(fps) // 2)        # 끝에서 못 받은 슬롯은 0.5초 분량까지만 채운다 (그보다 많으면 짧게 끝난 녹화)
        self.ref: Optional[float] = None
        self._cameras = cameras
        self._first: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def report(self, name: str, ts_ms: float) -> None:
        """카메라의 첫 프레임 시각. 4대가 다 알리면 기준 시각이 정해진다."""
        with self._lock:
            self._first.setdefault(name, ts_ms)
            if self.ref is None and len(self._first) >= self._cameras:
                self.ref = max(self._first.values()); self._ready.set()

    def wait(self, timeout: float = 3.0) -> float:
        """기준 시각 (첫 프레임을 알린 카메라만 부른다). 다른 카메라가 끝내 안 오면 온 카메라만으로 정한다."""
        self._ready.wait(timeout)
        with self._lock:
            if self.ref is None:
                self.ref = max(self._first.values())
            self._ready.set()
            return self.ref

    def release(self) -> None:
        """녹화 종료 — 기준 시각을 기다리는 처리 스레드를 깨운다."""
        with self._lock:
            if self.ref is None and self._first:
                self.ref = max(self._first.values())
            self._ready.set()


class _Recording:
    """카메라 하나의 녹화 한 번. 쓰기는 원본 RealSenseRecorder 의 쓰기 스레드를 그대로 쓴다.

    프레임 받기와 처리를 나눈다: 카메라 스레드는 프레임을 붙잡아(keep) 큐에 넣기만 하고 바로 다음 프레임을 받는다.
    정렬(align) · 복사 · 쓰기 큐 넣기는 이 녹화의 처리 스레드가 한다. 예전에는 카메라 스레드가 정렬까지 해서
    한 프레임 처리가 33ms 를 넘으면 그동안 온 프레임이 버려졌다 (실측: 5초에 약 153장 중 115장만 저장 · 약 25% 누락).
    처리가 잠깐 밀려도 RAW_MAX 장까지는 버리지 않고, 녹화가 끝난 뒤 남은 프레임을 마저 처리한다."""

    RAW_MAX = 300   # 카메라 1대 10초 분량 (기본 · 녹화 길이가 길면 WarmSession 이 늘린다)

    def __init__(self, serial: str, out_dir: Path, cam_name: str, intr: dict, go_at: float, raw_max: int = 0,
                 grid: Optional[_Grid] = None) -> None:
        self.rec = rv.RealSenseRecorder(serial)
        self.cam_name = cam_name
        self.go_at = go_at
        self.grid = grid          # 4대 공통 시간 격자 (없으면 예전처럼 받은 순서대로 번호를 매긴다)
        self.filled = 0           # 받지 못해 가까운 프레임으로 채운 슬롯 수 (정상이면 0)
        self._anchor = None       # 격자 기준 시각 ms (0번 슬롯의 시각)
        self._ts_off: Optional[float] = None
        self._prev = None         # 마지막으로 쓴 프레임 (슬롯 채우기용)
        self.stop_at: Optional[float] = None
        self.first_at: Optional[float] = None   # 실제로 기록을 시작한 첫 프레임의 호스트 시각
        self.accepted = 0                       # 기록하려고 받은 프레임 수 (화면의 실시간 프레임 수)
        self.frame_idx = 0
        self.dropped = 0          # 처리 큐가 가득 차 버린 프레임 (정상이면 0)
        self.missing = 0          # 카메라가 만들었지만 받지 못한 프레임 (하드웨어 시각 간격으로 계산 · 정상이면 0)
        self._last_hw: Optional[float] = None
        self.closing = False      # 더 받지 않음 (남은 프레임은 처리 중)
        self.done = False         # 처리 · 쓰기 큐 넣기까지 끝남
        self._align = None
        self._raw: "queue.Queue" = queue.Queue(maxsize=max(self.RAW_MAX, int(raw_max or 0)))
        depth_dir = out_dir / f"{cam_name}_depth"
        depth_dir.mkdir(parents=True, exist_ok=True)
        # 원본 RealSenseRecorder.start 와 같은 출력 경로
        self.rec._out = {
            "color_path":      str(out_dir / f"{cam_name}.mp4"),
            "depth_dir":       str(depth_dir),
            "depth_format":    "npy",
            "intrinsics_path": str(out_dir / f"{cam_name}_intrinsics.json"),
            "timestamps_path": str(out_dir / f"{cam_name}_timestamps.csv"),
            "frame_count":     0,
        }
        self._intr = dict(intr, camera_name=cam_name)
        self._intr_written = False
        self.rec._running = True
        self.rec._writer_thread = threading.Thread(target=self.rec._write_loop, args=(cam_name,), daemon=True,
                                                   name=f"rs-writer-{serial[-6:]}")
        self.rec._writer_thread.start()
        self._proc = threading.Thread(target=self._process_loop, daemon=True, name=f"rs-process-{serial[-6:]}")
        self._proc.start()

    def offer(self, frames, align, np=None) -> None:
        """카메라 스레드에서 프레임마다 호출 — 가볍게: 붙잡아 큐에 넣기만 한다."""
        host_t = _arrival(frames, time.time())   # 꺼낸 시각이 아니라 도착한 시각
        if self.closing or host_t < self.go_at:
            return
        if self.stop_at is not None and host_t >= self.stop_at:
            self.close(); return
        self._align = align
        try:
            frames.keep()                      # 다음 wait_for_frames 뒤에도 이 프레임을 쓸 수 있게
            self._raw.put_nowait((frames, host_t))
            if self.first_at is None:
                self.first_at = host_t
            self.accepted += 1
        except queue.Full:
            self.dropped += 1
            logger.warning(f"{self.rec.serial}: 처리 큐 가득 참 — 프레임 드롭 (총 {self.dropped})")

    def close(self, error: Optional[str] = None) -> None:
        """더 이상 프레임을 받지 않는다. 처리 스레드가 남은 프레임을 마저 처리하고 쓰기를 끝낸다."""
        if self.closing:
            return
        if error and not self.rec._capture_error:
            self.rec._capture_error = error
        self.closing = True
        while True:
            try: self._raw.put(None, timeout=1.0); break   # 종료 신호 (큐가 차 있으면 처리될 때까지 기다림)
            except queue.Full:
                if not self._proc.is_alive(): break

    def _process_loop(self) -> None:
        import numpy as np
        slot_ms = 1000.0 / max(1, rv.COLOR_FPS)
        try:
            while True:
                item = self._raw.get()
                if item is None:
                    break
                frames, host_t = item
                slot = None
                if self.grid is not None:
                    slot = self._slot(frames, host_t)
                    if slot is None:
                        continue                   # 격자 시작 전 프레임 · 이미 쓴 슬롯 (정렬하지 않고 버린다)
                    if self.grid.total is not None and slot >= self.grid.total:
                        self.closing = True        # 정해진 장수를 다 채움 → 더 받지 않는다
                        break
                frames = self._align.process(frames)
                color = frames.get_color_frame(); depth = frames.get_depth_frame()
                if not color or not depth:
                    continue
                if not self._intr_written:
                    self._write_intrinsics(color)
                hw_ts = color.get_timestamp()
                if self._last_hw is not None:
                    gap = int(round((hw_ts - self._last_hw) / slot_ms))
                    if gap > 1: self.missing += gap - 1
                self._last_hw = hw_ts
                color_img = np.asanyarray(color.get_data()).copy()
                depth_img = np.asanyarray(depth.get_data()).copy()
                cur = (color_img, depth_img, hw_ts, host_t)
                if slot is None:
                    slot = self.frame_idx
                # 받지 못한 슬롯 (직전에 쓴 번호와 이 프레임 사이) 은 시간상 가까운 쪽 프레임으로 채운다
                start = self.frame_idx
                for j in range(start, slot):
                    near_prev = self._prev is not None and (j - (start - 1)) <= (slot - j)
                    self._put(j, self._prev if near_prev else cur)
                    self.filled += 1
                self._put(slot, cur)
                self.frame_idx = slot + 1
                self._prev = cur
        except Exception as e:
            logger.exception(f"[warm] {self.cam_name} 녹화 처리 오류")
            if not self.rec._capture_error:
                self.rec._capture_error = f"녹화 처리 오류: {e}"
        finally:
            while True:                        # 남은 프레임 비우기 (붙잡은 메모리 해제)
                try:
                    if self._raw.get_nowait() is None: break
                except queue.Empty:
                    break
            self.finish()

    def _put(self, idx: int, frame: tuple) -> None:
        # 쓰기 스레드가 밀리면 여기서 기다린다 (버리지 않음 · 카메라 스레드는 영향 없음)
        self.rec._frame_queue.put((idx,) + frame, timeout=60)

    def _slot(self, frames, host_t: float) -> Optional[int]:
        """이 프레임의 격자 슬롯 번호. 쓰지 않을 프레임(격자 시작 전 · 이미 쓴 슬롯)이면 None."""
        g = self.grid
        color = frames.get_color_frame()
        if not color:
            return None
        ts = color.get_timestamp()
        if self._ts_off is None:
            # 카메라 시각이 PC 시각과 같은 기준(global time · 실측 차이 약 10~40ms)이면 그대로 4대를 비교한다.
            # 장치 내부 시계라면 카메라끼리 비교할 수 없으므로 첫 프레임 도착 시각에 맞춰 옮긴다
            self._ts_off = 0.0 if abs(ts - host_t * 1000.0) < 2000.0 else host_t * 1000.0 - ts
        ts += self._ts_off
        if self._anchor is None:
            g.report(self.cam_name, ts)
            self._anchor = g.wait()        # 4대 공통 기준 시각
        # 항상 기준 시각에서 바로 센다. 직전 프레임 기준으로 세면 프레임 시각이 고르지 않을 때(받지 못한 프레임 · 시각 흔들림)
        # 반올림 오차가 쌓여 번호가 카메라마다 다르게 밀린다 (실측 2026-11-19: 10초 녹화에서 최대 6칸 = 200ms)
        k = int(round((ts - self._anchor) / g.period))
        return k if k >= self.frame_idx else None

    def _write_intrinsics(self, color=None) -> None:
        """녹화 첫 프레임의 스트림에서 지금의 color intrinsics 를 읽어 원본과 같은 형식으로 쓴다.
        (카메라가 오래 열려 있으므로 열 때 읽은 값 대신 녹화 시점 값을 쓴다)"""
        intr = self._intr
        try:
            if color is not None:
                ci = color.get_profile().as_video_stream_profile().get_intrinsics()
                intr = dict(intr, color_intrinsics={"width": ci.width, "height": ci.height, "fx": ci.fx, "fy": ci.fy, "ppx": ci.ppx,
                                                    "ppy": ci.ppy, "model": str(ci.model), "coeffs": list(ci.coeffs)})
        except Exception:
            pass
        Path(self.rec._out["intrinsics_path"]).write_text(json.dumps(intr, indent=2, ensure_ascii=False))
        self._intr_written = True

    def finish(self) -> None:
        """처리 스레드가 끝날 때 한 번: 쓰기 스레드에 종료 신호."""
        if self.done:
            return
        if not self._intr_written:
            self._write_intrinsics()
        g = self.grid
        if g is not None and g.total is not None and self._prev is not None and not self.rec._capture_error:
            # 끝에서 받지 못한 슬롯: 마지막 프레임으로 채워 장수를 맞춘다 (0.5초 분량까지만)
            if 0 < g.total - self.frame_idx <= g.tail_max:
                try:
                    for j in range(self.frame_idx, g.total):
                        self._put(j, self._prev); self.filled += 1
                    self.frame_idx = g.total
                except Exception as e:
                    self.rec._capture_error = f"녹화 처리 오류: {e}"
        if self.dropped or self.missing or self.filled:
            logger.warning(f"{self.rec.serial}({self.cam_name}): {self.frame_idx}프레임 저장 · 받지 못함 {self.missing} · 채움 {self.filled} · 처리 큐 드롭 {self.dropped}")
        self.rec._frame_queue.put(None)   # 원본과 같은 종료 신호 → 쓰기 스레드가 큐를 비우고 끝난다
        self.done = True


class WarmCamera:
    """D455 한 대를 녹화 설정으로 계속 열어 둔다. 미리보기 JPEG 는 별도 스레드가 만든다."""

    def __init__(self, camera_id: str, serial: str) -> None:
        self.camera_id = camera_id
        self.serial = serial
        self.connected = False
        self.fps = 0.0
        self.resolution = ""
        self._running = False
        self._lock = threading.Lock()
        self._latest = None           # 미리보기용 최신 컬러 배열 (복사본)
        self._latest_at = 0.0
        self._jpeg: Optional[bytes] = None
        self._frame_times: List[float] = []
        self._intr: Optional[dict] = None
        self._factory: Optional[dict] = None
        self._recording: Optional[_Recording] = None
        self._thread: Optional[threading.Thread] = None
        self.last_frame_at = 0.0
        self.model = ""
        self.usb = ""
        self.last_error = ""
        self._fq = None               # SDK 프레임 버퍼 (rs.frame_queue) — 없으면 예전 방식
        self._classic = False         # SDK 버퍼 방식이 안 되는 장치 · SDK → 예전 방식으로만 연다
        self._loose = 0
        self._color_sensor = None     # RGB 센서 (노출 우선 · 화이트밸런스 설정용)

    # ── 열기 · 루프 ──
    def open(self):
        """파이프라인을 연다 (서비스가 카메라 순서대로 하나씩 부른다). 성공하면 (pipeline, align)."""
        rs = _rs()
        pipeline = rs.pipeline(); config = rs.config()
        config.enable_device(self.serial)
        config.enable_stream(rs.stream.color, rv.COLOR_W, rv.COLOR_H, rs.format.bgr8, rv.COLOR_FPS)
        config.enable_stream(rs.stream.depth, rv.DEPTH_W, rv.DEPTH_H, rs.format.z16, rv.DEPTH_FPS)
        profile, pipeline = self._start(rs, pipeline, config)
        align = rs.align(rs.stream.color)
        depth_scale = float(profile.get_device().first_depth_sensor().get_depth_scale())
        ci = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
        # 원본 realsense.py 의 intrinsics.json 과 같은 키 · 순서 (camera_name 은 녹화 때 넣는다)
        self._intr = {
            "serial": self.serial, "camera_name": self.camera_id, "depth_scale": depth_scale,
            "color_intrinsics": {"width": ci.width, "height": ci.height, "fx": ci.fx, "fy": ci.fy, "ppx": ci.ppx, "ppy": ci.ppy,
                                 "model": str(ci.model), "coeffs": list(ci.coeffs)},
            "aligned_to": "color", "color_fps": rv.COLOR_FPS, "depth_fps": rv.DEPTH_FPS,
        }
        import numpy as np
        self._factory = {"K": np.array([[ci.fx, 0, ci.ppx], [0, ci.fy, ci.ppy], [0, 0, 1]], dtype=np.float64),
                         "dist": np.array(ci.coeffs, dtype=np.float64), "size": (ci.width, ci.height)}
        self.last_error = ""
        try: self._color_sensor = profile.get_device().first_color_sensor()
        except Exception: self._color_sensor = None
        color_notes = _apply_color_options(rs, self._color_sensor, self.camera_id)   # 다시 열 때도 같은 설정
        logger.warning(f"[warm] {self.camera_id} ({self.serial}) 열림 {ci.width}x{ci.height}@{rv.COLOR_FPS} · "
                       + (f"SDK 버퍼 {SDK_QUEUE}장" if self._fq is not None else "SDK 버퍼 없음 (예전 방식)")
                       + (" · " + color_notes if color_notes else ""))
        return pipeline, align

    def _start(self, rs, pipeline, config):
        """스트림 시작. SDK 버퍼(rs.frame_queue)로 받는 방식을 먼저 쓰고, 안 되면 예전 방식(wait_for_frames)으로 연다."""
        self._fq = None; self._loose = 0
        profile = None
        if SDK_QUEUE > 0 and not self._classic:
            try:
                fq = rs.frame_queue(int(SDK_QUEUE), True)   # True = keep_frames: 버퍼에 있는 동안 프레임을 붙잡아 둔다
                profile = pipeline.start(config, fq)
                self._fq = fq
            except Exception as e:
                logger.warning(f"[warm] {self.camera_id} SDK 버퍼로 열지 못해 예전 방식으로 연다: {e}")
                self._classic = True
                try: pipeline.stop()
                except Exception: pass
                pipeline = rs.pipeline()
        if profile is None:
            profile = pipeline.start(config)
        if self._fq is not None:
            # 붙잡아 둘 수 있는 프레임 수 (기본 16장) 를 장치가 허용하는 최대로 → 버퍼에 쌓인 프레임 때문에 새 프레임이 버려지지 않게
            try:
                for s in profile.get_device().query_sensors():
                    if s.supports(rs.option.frames_queue_size):
                        s.set_option(rs.option.frames_queue_size, s.get_option_range(rs.option.frames_queue_size).max)
            except Exception as e:
                logger.warning(f"[warm] {self.camera_id} frames_queue_size 설정 실패 (기본값으로 계속): {e}")
        return profile, pipeline

    def _receive(self, pipeline):
        """다음 프레임 묶음 (컬러 + depth). 1초 안에 안 오면 RuntimeError. SDK 버퍼 방식에서 묶음이 아닌 프레임이면 None."""
        if self._fq is None:
            return pipeline.wait_for_frames(timeout_ms=1000)
        f = self._fq.wait_for_frame(1000)   # timeout ms
        if not f.is_frameset():
            self._loose += 1
            if self._loose > 90:
                # 묶음(frameset)이 오지 않는 SDK → 예전 방식으로 다시 연다
                self._classic = True
                raise ValueError("SDK 버퍼에서 프레임 묶음이 오지 않음 — 예전 방식으로 다시 연결")
            return None
        return f.as_frameset()

    def start(self, opened=None) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, args=(opened,), daemon=True, name=f"warm-{self.serial[-6:]}")
        self._thread.start()

    def _loop(self, opened) -> None:
        import numpy as np
        while self._running:
            pipeline = None
            try:
                pipeline, align = opened if opened else self.open()
                opened = None
                last_good = time.time(); got_any = False
                while self._running:
                    try:
                        frames = self._receive(pipeline)
                        if frames is None:
                            continue
                        last_good = time.time(); got_any = True
                    except RuntimeError:
                        if time.time() - self.last_frame_at > 3:
                            self.connected = False
                        if time.time() - last_good > 5:
                            # 열려 있는데 영상이 안 온다 (USB 를 다시 꽂았거나 장치가 멈춤) → 닫고 다시 연다
                            if self._fq is not None and not got_any:
                                self._classic = True   # SDK 버퍼 방식으로 연 뒤 한 장도 안 왔다 → 다음에는 예전 방식으로
                            raise RuntimeError("5초 동안 프레임이 오지 않음 — 다시 연결")
                        continue
                    t = time.time(); self.last_frame_at = t
                    rec = self._recording
                    if rec is not None:
                        rec.offer(frames, align, np)
                    if t - self._latest_at >= 1.0 / PREVIEW_FPS - 0.01:   # 30fps 스트림에서 두 장에 한 장 → 15fps
                        color = frames.get_color_frame()
                        if color:
                            img = np.asanyarray(color.get_data()).copy()
                            with self._lock:
                                self._latest, self._latest_at = img, t
                                self.connected = True
                                self._frame_times = [x for x in self._frame_times if t - x < 2.0] + [t]
                                self.fps = round(len(self._frame_times) / 2.0, 1)
                                self.resolution = f"{img.shape[1]}x{img.shape[0]}"
            except Exception as e:
                logger.warning(f"[warm] {self.camera_id} ({self.serial}) 스트림 오류: {e}")
                self.last_error = usb_hint(self.usb) or str(e)[:200]
                self.connected = False
                rec = self._recording
                if rec is not None:
                    rec.close(f"스트림 오류: {e}")
                time.sleep(3.0)
            finally:
                if pipeline is not None:
                    t0 = time.time()
                    try: pipeline.stop()
                    except Exception as e: logger.warning(f"[warm] {self.camera_id} 닫기 오류: {e}")
                    if not self._running:
                        logger.warning(f"[warm] {self.camera_id} 닫힘 ({time.time() - t0:.1f}초)")

    # ── 미리보기 ──
    def encode_preview(self) -> None:
        with self._lock:
            img = self._latest
            if img is None or (self._jpeg is not None and getattr(self, "_encoded", None) is img):
                return
            self._encoded = img
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if ok:
            self._jpeg = buf.tobytes()

    def get_frame_jpeg(self) -> Optional[bytes]:
        if time.time() - self.last_frame_at > 3:
            return None
        return self._jpeg

    def get_factory_intrinsics(self) -> Optional[dict]:
        return self._factory

    def get_status(self) -> dict:
        live = self.connected and time.time() - self.last_frame_at < 3
        return {"camera_id": self.camera_id, "source": "realsense", "connected": live, "fps": self.fps if live else 0.0,
                "resolution": self.resolution, "serial": self.serial, "backend": "realsense",
                "recording": self._recording is not None, "model": self.model, "usb": self.usb,
                "target": f"{rv.COLOR_W}x{rv.COLOR_H}", "warning": usb_hint(self.usb),
                "error": "" if live else self.last_error}

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float = 4.0) -> None:
        if self._thread:
            self._thread.join(timeout)


class WarmCameraService:
    """ExoCameraService 대신 쓰는 카메라 관리 (같은 메서드 이름: list_cameras · get_factory_intrinsics_list · _cameras)."""

    _instance: Optional["WarmCameraService"] = None
    _lock = threading.Lock()

    EXPECTED = 4

    def __init__(self) -> None:
        self._cameras: Dict[str, WarmCamera] = {}
        logger.warning("[warm] 카메라 찾는 중")
        self._build(rv.discover_devices())
        self._encoder = threading.Thread(target=self._encode_loop, daemon=True, name="warm-preview")
        self._encoder.start()
        # 서버를 켠 뒤에 카메라를 꽂거나 다시 꽂아도 알아서 찾는다 (재시작 불필요)
        self._watcher = threading.Thread(target=self._watch_loop, daemon=True, name="warm-watch")
        self._watcher.start()

    def _build(self, serials: List[str]) -> None:
        cams = {f"cam_{i + 1}": WarmCamera(f"cam_{i + 1}", s) for i, s in enumerate(serials)}
        info = _device_info()
        for cam in cams.values():
            cam.model = info.get(cam.serial, {}).get("model", ""); cam.usb = info.get(cam.serial, {}).get("usb", "")
            if usb_hint(cam.usb): logger.warning(f"[warm] {cam.camera_id} ({cam.serial}) {usb_hint(cam.usb)}")
        # 4대를 동시에 열면 장치가 멈추는 경우가 있어 하나씩 연다 (실측: 순차 열기는 안정적)
        for cam in cams.values():
            try:
                cam.start(cam.open())
            except Exception as e:
                logger.warning(f"[warm] {cam.camera_id} ({cam.serial}) 열기 실패, 다시 시도: {e}")
                cam.last_error = usb_hint(cam.usb) or str(e)[:200]
                cam.start(None)
        self._cameras = cams
        logger.warning(f"[warm] 카메라 {len(cams)}대 ({rv.COLOR_W}x{rv.COLOR_H}@{rv.COLOR_FPS}): "
                       + ", ".join(f"{c.serial} USB {c.usb or '?'}" for c in cams.values()))
        if WHITE_BALANCE == "lock" and _wb_locked is None and cams:
            threading.Thread(target=self._lock_white_balance, daemon=True, name="warm-wb").start()

    def _lock_white_balance(self) -> None:
        """자동 화이트밸런스가 자리 잡은 뒤 4대 값의 가운데 값으로 4대를 같이 고정한다 (서버를 켤 때 한 번)."""
        global _wb_locked
        time.sleep(WB_SETTLE_S)
        try:
            rs = _rs()
            cams = [c for c in self._cameras.values() if c._color_sensor is not None]
            values, default = {}, None
            for c in cams:
                try:
                    s = c._color_sensor
                    if not s.supports(rs.option.white_balance):
                        continue
                    values[c.camera_id] = float(s.get_option(rs.option.white_balance))
                    default = s.get_option_range(rs.option.white_balance).default
                except Exception as e:
                    logger.warning(f"[warm] {c.camera_id} 화이트밸런스 읽기 실패: {e}")
            if not values:
                logger.warning("[warm] 화이트밸런스 고정 못 함 — 읽을 수 있는 RGB 센서가 없음 (자동으로 계속)")
                return
            v = sorted(values.values())
            median = v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2
            _wb_locked = int(round(median))
            applied = {}
            for c in cams:
                try: applied[c.camera_id] = _set_wb(rs, c._color_sensor, _wb_locked)
                except Exception as e: logger.warning(f"[warm] {c.camera_id} 화이트밸런스 고정 실패: {e}")
            same_as_default = default is not None and all(abs(x - default) < 1 for x in values.values())
            logger.warning("[warm] 화이트밸런스 고정 " + str(_wb_locked) + "K (자동 값: "
                           + ", ".join(f"{k} {int(x)}K" for k, x in values.items()) + ")"
                           + (" — 4대 모두 장치 기본값이라 자동 값을 못 읽었을 수 있음. capture.json 에 white_balance 숫자를 넣으세요"
                              if same_as_default else ""))
        except Exception:
            logger.exception("[warm] 화이트밸런스 고정 실패 (자동으로 계속)")

    def _watch_loop(self) -> None:
        while True:
            time.sleep(5)
            try:
                cams = list(self._cameras.values())
                if any(c._recording is not None for c in cams):
                    continue
                healthy = len(cams) >= self.EXPECTED and all(c.get_status()["connected"] for c in cams)
                if healthy:
                    continue                      # 정상일 때는 장치 목록을 다시 읽지 않는다 (읽는 데 ~0.7초)
                serials = rv.discover_devices()
                if serials and serials != [c.serial for c in cams]:
                    for c in cams: c.stop()
                    time.sleep(1.5)               # 이전 파이프라인이 닫힐 시간
                    self._build(serials)
            except Exception:
                logger.exception("[warm] 카메라 다시 찾기 실패")

    @classmethod
    def get(cls) -> "WarmCameraService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _encode_loop(self) -> None:
        while True:
            for cam in list(self._cameras.values()):
                try: cam.encode_preview()
                except Exception: pass
            time.sleep(1.0 / (PREVIEW_FPS * 2))

    def shutdown(self) -> None:
        """서버 종료 때 스트림을 정상적으로 닫는다 (강제 종료로 장치가 멈추는 것을 줄인다)."""
        cams = list(self._cameras.values())
        t0 = time.time()
        logger.warning(f"[warm] 종료: 카메라 {len(cams)}대 닫는 중")
        for c in cams: c.stop()
        for c in cams: c.join(6.0)
        left = [c.camera_id for c in cams if c._thread and c._thread.is_alive()]
        logger.warning(f"[warm] 종료: {'모두 닫힘' if not left else '안 닫힌 카메라 ' + ', '.join(left)} ({time.time() - t0:.1f}초)")

    def list_cameras(self) -> List[dict]:
        return [c.get_status() for c in self._cameras.values()]

    def serials(self) -> List[str]:
        return [c.serial for c in self._cameras.values()]

    def get_factory_intrinsics_list(self, camera_names: Optional[List[str]] = None):
        keys = camera_names or list(self._cameras)
        return [self._cameras[k].get_factory_intrinsics() if k in self._cameras else None for k in keys]


class WarmSession:
    """즉시 녹화. RealSenseSession 과 같은 결과(dict[cam] = 출력 정보 또는 {'error'})를 돌려준다."""

    def __init__(self, service: WarmCameraService) -> None:
        self.service = service
        self._recordings: Dict[str, _Recording] = {}
        self._recorders: list = []        # capture.py 가 쓰기 스레드가 끝났는지 볼 때 쓴다 (원본과 같은 이름)
        self._grid: Optional[_Grid] = None

    def start(self, out_dir, seconds: float = 0) -> List[str]:
        out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        raw_max = int(rv.COLOR_FPS * (seconds + 2)) if seconds else 0   # 녹화 전체 + 2초 분량까지는 처리가 밀려도 버리지 않는다
        cams = list(self.service._cameras.values())
        if not cams:
            raise RuntimeError("연결된 RealSense 기기가 없습니다")
        for c in cams:
            if not c.get_status()["connected"] or c._intr is None:
                raise RuntimeError(f"{c.camera_id} 영상이 들어오지 않습니다 — USB 연결 확인")
            if c._recording is not None:
                raise RuntimeError(f"{c.camera_id} 이미 녹화 중")
        go_at = time.time() + 0.05      # 4대가 같은 호스트 시각부터 기록
        # 4대 공통 시간 격자: 프레임 번호 = 시간 슬롯, 카메라마다 정확히 seconds × FPS 장
        self._grid = _Grid(rv.COLOR_FPS, len(cams), int(round(seconds * rv.COLOR_FPS)) if seconds else None)
        for c in cams:
            r = _Recording(c.serial, out_dir, c.camera_id, c._intr, go_at, raw_max, self._grid)
            self._recordings[c.camera_id] = r
            self._recorders.append(r.rec)
        for c in cams:
            c._recording = self._recordings[c.camera_id]
        return [c.camera_id for c in cams]

    def get_live_status(self) -> Dict[str, int]:
        total = self._grid.total if self._grid else None
        return {name: min(r.accepted, total) if total else r.accepted for name, r in self._recordings.items()}

    def wait_first(self, timeout: float = 3.0) -> float:
        """4대 모두 첫 프레임을 실제로 받을 때까지 기다린다 → 4대가 다 기록 중인 시각 (가장 늦은 첫 프레임)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            recs = list(self._recordings.values())
            bad = [r for r in recs if r.closing and r.first_at is None]
            if bad:
                raise RuntimeError(f"{bad[0].cam_name} 녹화 시작 실패: {bad[0].rec._capture_error or '스트림 오류'}")
            if recs and all(r.first_at is not None for r in recs):
                return max(r.first_at for r in recs)
            time.sleep(0.005)
        late = [r.cam_name for r in self._recordings.values() if r.first_at is None]
        raise RuntimeError(f"{', '.join(late)} 첫 프레임이 {timeout:.0f}초 안에 들어오지 않음 — USB 연결 확인")

    def schedule_stop(self, seconds: float) -> float:
        """끝 시각을 정한다 (4대 공통). 장수는 격자가 정확히 끊는다 (카메라마다 seconds × FPS 장 · 30fps 10초 = 300장).
        격자의 0번은 가장 늦은 첫 프레임이므로 거기서 seconds 초 + 한 프레임 뒤 → 마지막 슬롯의 프레임이 조금 늦게 와도 받는다.
        끝 시각을 돌려준다."""
        recs = list(self._recordings.values())
        firsts = [r.first_at for r in recs if r.first_at is not None]
        if firsts:
            stop = max(firsts) + seconds + 1.0 / max(1, rv.COLOR_FPS)
            for r in recs:
                if r.stop_at is None:
                    r.stop_at = stop
        return max((r.stop_at for r in recs if r.stop_at is not None), default=time.time())

    def stop(self) -> dict:
        stop_at = time.time()
        for r in self._recordings.values():
            if r.stop_at is None or r.stop_at > stop_at:
                r.stop_at = stop_at      # 4대가 같은 호스트 시각에서 끊는다 (schedule_stop 으로 정한 끝 시각은 그대로)
        deadline = time.time() + 3
        while time.time() < deadline and not all(r.closing for r in self._recordings.values()):
            time.sleep(0.01)
        for name, r in self._recordings.items():
            if not r.closing:
                r.close(f"{name}: 종료 시각 이후 프레임이 오지 않음")
        if self._grid is not None:
            self._grid.release()
        for c in self.service._cameras.values():
            if c.camera_id in self._recordings:
                c._recording = None
        # 받아 둔 프레임을 마저 정렬 · 저장한다 (보통 1~2초)
        deadline = time.time() + 120
        while time.time() < deadline and not all(r.done for r in self._recordings.values()):
            time.sleep(0.02)
        results = {}
        for name, r in self._recordings.items():
            if not r.done and not r.rec._capture_error:
                r.rec._capture_error = "녹화 처리 시간 초과"
            t = r.rec._writer_thread
            if t: t.join(timeout=120.0)
            error = r.rec._capture_error or r.rec._writer_error
            results[name] = {"error": f"{r.rec.serial}: {error}"} if error else dict(r.rec._out, missing=r.missing, dropped=r.dropped, filled=r.filled)
        self._recordings = {}
        return results
