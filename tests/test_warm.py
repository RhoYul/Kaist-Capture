import json, queue, tempfile, threading, time, unittest
from pathlib import Path
import cv2, numpy as np
from studio import warm


class _F:
    def __init__(self, arr, ts=0.0): self.arr, self.ts = arr, ts
    def get_data(self): return self.arr
    def get_timestamp(self): return self.ts
    def __bool__(self): return True

class _Frames:
    def __init__(self, i):
        self.c = _F(np.full((480, 640, 3), i % 255, np.uint8), 1000.0 + i * 1000 / 30)
        self.d = _F(np.full((480, 640), 500 + i, np.uint16))
        self.kept = False
    def keep(self): self.kept = True
    def get_color_frame(self): return self.c
    def get_depth_frame(self): return self.d

class _Align:
    def __init__(self, delay=0.0): self.delay = delay
    def process(self, f):
        if self.delay: time.sleep(self.delay)
        return f


def _wait_done(r, timeout=20):
    end = time.time() + timeout
    while not r.done and time.time() < end: time.sleep(0.01)
    assert r.done, "processing did not finish"
    r.rec._writer_thread.join(timeout)

INTR = {"serial": "S1", "camera_name": "cam_1", "depth_scale": 0.001,
        "color_intrinsics": {"width": 640, "height": 480, "fx": 1.0, "fy": 1.0, "ppx": 1.0, "ppy": 1.0, "model": "m", "coeffs": [0, 0, 0, 0, 0]},
        "aligned_to": "color", "color_fps": 30, "depth_fps": 30}


class _Cam:
    def __init__(self, cid, serial):
        self.camera_id, self.serial, self._intr, self._recording = cid, serial, dict(INTR, serial=serial), None
    def get_status(self): return {"connected": True}

class _Service:
    def __init__(self): self._cameras = {f"cam_{i}": _Cam(f"cam_{i}", f"S{i}") for i in range(1, 5)}


class WarmRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.out = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_recording_writes_original_format_via_vendor_writer(self):
        r = warm._Recording("S1", self.out, "cam_1", INTR, go_at=time.time() - 1)
        frames = [_Frames(i) for i in range(10)]
        for f in frames: r.offer(f, _Align(), np)
        self.assertTrue(all(f.kept for f in frames))   # 카메라 스레드는 붙잡아 넘기기만
        r.close(); _wait_done(r)
        self.assertEqual(r.rec._out["frame_count"], 10)
        self.assertEqual((r.missing, r.dropped), (0, 0))
        self.assertEqual(sorted(p.name for p in (self.out / "cam_1_depth").glob("*.npy"))[0], "frame_000000.npy")
        self.assertEqual(len(list((self.out / "cam_1_depth").glob("*.npy"))), 10)
        np.testing.assert_array_equal(np.load(self.out / "cam_1_depth" / "frame_000003.npy"), np.full((480, 640), 503, np.uint16))
        cap = cv2.VideoCapture(str(self.out / "cam_1.mp4")); self.assertEqual(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 10); cap.release()
        rows = (self.out / "cam_1_timestamps.csv").read_text().splitlines()
        self.assertEqual(rows[0], "frame_idx,hw_timestamp_ms,host_time_s"); self.assertEqual(len(rows), 11)
        intr = json.loads((self.out / "cam_1_intrinsics.json").read_text())
        self.assertEqual(list(intr), list(INTR))      # 원본 realsense.py 와 같은 키 순서

    def test_frames_before_go_and_after_stop_are_not_written(self):
        r = warm._Recording("S1", self.out, "cam_1", INTR, go_at=time.time() + 60)
        r.offer(_Frames(0), _Align(), np)
        self.assertEqual(r.frame_idx, 0)               # 시작 시각 전
        r.go_at = time.time() - 1
        r.offer(_Frames(1), _Align(), np); r.stop_at = time.time() - 0.001
        r.offer(_Frames(2), _Align(), np)              # 종료 시각 뒤 → 더 받지 않음
        self.assertTrue(r.closing); _wait_done(r)
        self.assertEqual(r.rec._out["frame_count"], 1)

    def test_slow_processing_does_not_drop_frames(self):
        # 정렬이 프레임 간격(33ms)보다 느려도 (50ms) 카메라 스레드는 막히지 않고, 모든 프레임이 저장된다
        r = warm._Recording("S1", self.out, "cam_1", INTR, go_at=time.time() - 1)
        align = _Align(delay=0.05); t0 = time.time()
        for i in range(60):
            r.offer(_Frames(i), align, np); time.sleep(1 / 30)
        offered_s = time.time() - t0
        r.close(); _wait_done(r)
        self.assertLess(offered_s, 60 / 30 + 1.0)      # 넘기는 쪽은 실시간 속도 유지
        self.assertEqual(r.rec._out["frame_count"], 60)
        self.assertEqual((r.missing, r.dropped), (0, 0))

    def test_counts_frames_the_camera_made_but_never_arrived(self):
        r = warm._Recording("S1", self.out, "cam_1", INTR, go_at=time.time() - 1)
        for i in (0, 1, 2, 5, 6, 9):                   # 3 · 4 · 7 · 8 번 프레임이 안 옴
            r.offer(_Frames(i), _Align(), np)
        r.close(); _wait_done(r)
        self.assertEqual((r.rec._out["frame_count"], r.missing), (6, 4))

    def test_session_starts_all_cameras_at_same_time_and_stops(self):
        svc = _Service(); s = warm.WarmSession(svc)
        names = s.start(self.out)
        self.assertEqual(names, ["cam_1", "cam_2", "cam_3", "cam_4"])
        self.assertEqual(len({c._recording.go_at for c in svc._cameras.values()}), 1)
        stop = threading.Event()
        def feed(cam):
            i = 0
            while not stop.is_set():
                rec = cam._recording
                if rec: rec.offer(_Frames(i), _Align(), np); i += 1
                time.sleep(0.01)
        ts = [threading.Thread(target=feed, args=(c,), daemon=True) for c in svc._cameras.values()]
        [t.start() for t in ts]; time.sleep(0.4)
        res = s.stop(); stop.set()
        self.assertEqual(set(res), set(names))
        self.assertTrue(all("error" not in r and r["frame_count"] > 0 for r in res.values()), res)
        self.assertTrue(all(c._recording is None for c in svc._cameras.values()))
        self.assertEqual(len(s._recorders), 4)

    def test_length_counts_from_the_latest_first_recorded_frame(self):
        # 녹화 길이는 버튼 시각이 아니라 4대 모두 첫 프레임이 실제로 기록된 시각부터. 끝 시각은 4대 공통 (장수는 격자가 끊는다)
        svc = _Service(); s = warm.WarmSession(svc); s.start(self.out, seconds=10)
        self.assertTrue(all(c._recording._raw.maxsize >= 30 * 12 for c in svc._cameras.values()))   # 10초 + 여유분은 처리가 밀려도 버리지 않음
        with self.assertRaises(RuntimeError): s.wait_first(timeout=0.02)       # 아직 첫 프레임 없음
        time.sleep(0.06)                                                        # go_at 지나서
        for c in svc._cameras.values(): c._recording.offer(_Frames(0), _Align(), np)
        first = s.wait_first(timeout=1)
        recs = list(s._recordings.values())
        self.assertEqual(first, max(r.first_at for r in recs))
        until = s.schedule_stop(5)
        for r in recs: self.assertAlmostEqual(r.stop_at, first + 5 + 1 / 30)
        self.assertAlmostEqual(until, first + 5 + 1 / 30)
        self.assertEqual(s.get_live_status(), {f"cam_{i}": 1 for i in range(1, 5)})
        for r in recs: r.stop_at = time.time() - 0.001; r.offer(_Frames(1), _Align(), np)   # 끝 시각 뒤 프레임 → 닫힘 · 기록 안 함
        res = s.stop()
        self.assertTrue(all(r["frame_count"] == 1 for r in res.values()), res)

    # ── 4대 공통 시간 격자: 프레임 번호 = 시각, 카메라마다 같은 장수 ──
    def _grid_run(self, plan, total=10, fps=30):
        """plan[cam] = 그 카메라가 받은 프레임의 (카메라 시각 ms) 목록. 지금 시각 기준(global time)으로 넣는다."""
        base = time.time() * 1000.0
        grid = warm._Grid(fps, len(plan), total)
        recs = {}
        for cam, stamps in plan.items():
            r = warm._Recording("S" + cam[-1], self.out, cam, dict(INTR, serial="S" + cam[-1]), go_at=time.time() - 1, grid=grid)
            recs[cam] = r
        for cam, stamps in plan.items():
            for i, ms in enumerate(stamps):
                f = _Frames(int(round(ms / (1000 / fps)))); f.c.ts = base + ms   # depth 값 = 500 + (그 카메라의 몇 번째 프레임 시각인지)
                recs[cam].offer(f, _Align(), np)
        for r in recs.values(): r.close()
        for r in recs.values(): _wait_done(r)
        rows = {cam: [l.split(",") for l in (self.out / f"{cam}_timestamps.csv").read_text().splitlines()[1:]] for cam in plan}
        return recs, {cam: [round(float(x[1]) - base, 1) for x in v] for cam, v in rows.items()}

    def test_grid_same_index_is_same_time_even_when_frames_are_lost(self):
        P = 1000 / 30
        full = [i * P for i in range(14)]
        plan = {"cam_1": full,
                "cam_2": [t + 10 for i, t in enumerate(full) if i not in (3, 4, 8)],   # 위상 10ms · 3장 받지 못함
                "cam_3": [t + 5 for t in full[1:]],                                    # 첫 프레임을 받지 못함 → 격자가 여기서 시작
                "cam_4": [t - 12 for t in full]}
        recs, ts = self._grid_run(plan, total=10)
        self.assertTrue(all(r.rec._out["frame_count"] == 10 for r in recs.values()), {c: r.rec._out["frame_count"] for c, r in recs.items()})
        for i in range(10):       # 같은 번호의 4대 프레임은 (채운 슬롯을 빼면) 한 프레임 간격 안
            real = [ts[c][i] for c in plan if not (c == "cam_2" and i in (2, 3, 7))]
            self.assertLess(max(real) - min(real), P, (i, real))
        self.assertEqual(ts["cam_3"][0], round(P + 5, 1))                # 0번 = 가장 늦은 첫 프레임
        self.assertEqual(ts["cam_1"][0], round(P, 1))                    # 그보다 이른 프레임은 버린다
        self.assertEqual({c: r.filled for c, r in recs.items()}, {"cam_1": 0, "cam_2": 3, "cam_3": 0, "cam_4": 0})
        # 받지 못한 슬롯은 가까운 프레임으로 채운다 → 같은 시각이 연달아 나온다 (cam_2: 원래 3 · 4 · 8번 = 격자 2 · 3 · 7번)
        self.assertEqual(ts["cam_2"][2], ts["cam_2"][1]); self.assertEqual(ts["cam_2"][3], ts["cam_2"][4]); self.assertEqual(ts["cam_2"][7], ts["cam_2"][6])
        self.assertEqual(len(list((self.out / "cam_2_depth").glob("*.npy"))), 10)
        cap = cv2.VideoCapture(str(self.out / "cam_2.mp4")); self.assertEqual(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 10); cap.release()

    def test_grid_numbers_do_not_drift_when_frame_times_are_uneven(self):
        # 프레임 시각이 고르지 않아도(45 · 45 · 10ms 간격 등) 번호는 기준 시각에서 바로 센다 → 반올림 오차가 쌓이지 않는다
        P = 1000 / 30
        uneven = []
        for i in range(0, 60, 3): uneven += [i * P, i * P + 45, i * P + 90]     # 3칸마다 45 · 45 · 10ms
        recs, ts = self._grid_run({"cam_1": uneven, "cam_2": [i * P for i in range(60)]}, total=60)
        self.assertEqual(recs["cam_1"].rec._out["frame_count"], 60)
        for i in range(60):
            self.assertLess(abs(ts["cam_1"][i] - i * P), P, (i, ts["cam_1"][i]))   # 번호 i 의 프레임은 격자 시각 i 에서 한 칸 안
            self.assertLess(abs(ts["cam_1"][i] - ts["cam_2"][i]), P)

    def test_host_time_is_arrival_time_not_dequeue_time(self):
        # SDK 버퍼에서 늦게 한꺼번에 꺼내도 host_time_s 는 프레임이 도착한 시각 (SDK 의 time_of_arrival)
        class _RS:
            class frame_metadata_value: time_of_arrival = "toa"
        old_rs, old_toa = warm._rs, warm._TOA; warm._rs, warm._TOA = (lambda: _RS), None
        try:
            base = time.time() - 1.0
            r = warm._Recording("S1", self.out, "cam_1", INTR, go_at=base - 1)
            for i in range(6):
                f = _Frames(i)
                f.c.supports_frame_metadata = lambda m: m == "toa"
                f.c.get_frame_metadata = lambda m, i=i: int((base + i / 30) * 1000)
                r.offer(f, _Align(), np)                                         # 6장을 같은 순간에 꺼냄
            bad = _Frames(6); bad.c.supports_frame_metadata = lambda m: True; bad.c.get_frame_metadata = lambda m: 12345   # 이상한 값 → 꺼낸 시각
            now = time.time(); r.offer(bad, _Align(), np)
            r.close(); _wait_done(r)
        finally:
            warm._rs, warm._TOA = old_rs, old_toa
        host = [float(l.split(",")[2]) for l in (self.out / "cam_1_timestamps.csv").read_text().splitlines()[1:]]
        for i in range(6): self.assertAlmostEqual(host[i], base + i / 30, delta=0.002)
        self.assertAlmostEqual(host[6], now, delta=0.5)

    def test_grid_fills_short_tail_but_not_a_recording_that_ended_early(self):
        P = 1000 / 30
        recs, ts = self._grid_run({"cam_1": [i * P for i in range(8)], "cam_2": [i * P + 3 for i in range(10)]}, total=10)
        self.assertEqual((recs["cam_1"].rec._out["frame_count"], recs["cam_1"].filled), (10, 2))   # 끝 2장은 마지막 프레임으로
        self.assertEqual((recs["cam_2"].rec._out["frame_count"], recs["cam_2"].filled), (10, 0))

    def test_grid_does_not_pad_when_most_of_the_recording_is_missing(self):
        P = 1000 / 30
        recs, _ = self._grid_run({"cam_1": [i * P for i in range(3)]}, total=60)
        self.assertEqual((recs["cam_1"].rec._out["frame_count"], recs["cam_1"].filled), (3, 0))

    # ── SDK 프레임 버퍼로 받기 (안 되면 예전 방식) ──
    def _fake_rs(self, queue_start_fails=False):
        class _Opt:
            max = 32.0
        class _Sensor:
            def __init__(s): s.set = None
            def supports(s, o): return True
            def get_option_range(s, o): return _Opt()
            def set_option(s, o, v): s.set = v
        sensor = _Sensor()
        class _Profile:
            def get_device(s): return type("D", (), {"query_sensors": lambda d: [sensor]})()
        class _Pipe:
            def __init__(s): s.args = None; s.stopped = False
            def start(s, config, fq=None):
                if fq is not None and queue_start_fails: raise RuntimeError("no queue")
                s.args = (config, fq); return _Profile()
            def stop(s): s.stopped = True
            def wait_for_frames(s, timeout_ms=0): return "classic"
        class _FQ:
            def __init__(s, cap, keep): s.cap, s.keep, s.items = cap, keep, []
            def wait_for_frame(s, ms): return s.items.pop(0)
        rs = type("RS", (), {"pipeline": _Pipe, "frame_queue": _FQ, "option": type("O", (), {"frames_queue_size": 1})})
        return rs, sensor

    def test_camera_receives_through_sdk_buffer(self):
        rs, sensor = self._fake_rs(); cam = warm.WarmCamera("cam_1", "S1")
        profile, pipe = cam._start(rs, rs.pipeline(), "cfg")
        self.assertIs(pipe.args[1], cam._fq); self.assertEqual((cam._fq.cap, cam._fq.keep), (warm.SDK_QUEUE, True))
        self.assertEqual(sensor.set, 32.0)                       # 붙잡아 둘 수 있는 프레임 수를 최대로
        fs = type("F", (), {"is_frameset": lambda s: True, "as_frameset": lambda s: "frameset"})()
        cam._fq.items.append(fs); self.assertEqual(cam._receive(pipe), "frameset")

    def test_camera_falls_back_to_classic_when_sdk_buffer_does_not_work(self):
        rs, _ = self._fake_rs(queue_start_fails=True); cam = warm.WarmCamera("cam_1", "S1")
        profile, pipe = cam._start(rs, rs.pipeline(), "cfg")
        self.assertIsNone(cam._fq); self.assertTrue(cam._classic); self.assertEqual(pipe.args, ("cfg", None))
        self.assertEqual(cam._receive(pipe), "classic")
        # 버퍼로 열렸는데 프레임 묶음이 오지 않으면 예전 방식으로 다시 연다
        rs, _ = self._fake_rs(); cam = warm.WarmCamera("cam_1", "S1"); _, pipe = cam._start(rs, rs.pipeline(), "cfg")
        loose = type("F", (), {"is_frameset": lambda s: False})()
        cam._fq.items = [loose] * 200
        with self.assertRaises(ValueError):
            for _ in range(200): self.assertIsNone(cam._receive(pipe))
        self.assertTrue(cam._classic)
        _, pipe = cam._start(rs, rs.pipeline(), "cfg"); self.assertIsNone(cam._fq)
        # sdk_queue 0 = 예전 방식
        old = warm.SDK_QUEUE; warm.SDK_QUEUE = 0
        try:
            cam = warm.WarmCamera("cam_1", "S1"); _, pipe = cam._start(rs, rs.pipeline(), "cfg"); self.assertIsNone(cam._fq)
        finally: warm.SDK_QUEUE = old

    # ── 프리롤 (녹화 전 카운트다운): 실제 녹화와 같은 처리를 하되 에피소드에는 쓰지 않는다 ──
    def test_preroll_frames_go_through_real_path_but_not_into_episode(self):
        pre = self.out / "pre"; ep = self.out / "ep"; ep.mkdir()
        r = warm._Recording("S1", ep, "cam_1", INTR, go_at=time.time() - 1, rec_at=time.time() + 0.3, preroll_dir=pre)
        warmup = [_Frames(i) for i in (0, 1, 2, 5, 6)]           # 프리롤 중 3 · 4 번을 받지 못함 (진단용으로만 센다)
        for f in warmup: r.offer(f, _Align(), np)
        self.assertIsNone(r.first_at); self.assertEqual(r.accepted, 0)
        time.sleep(0.35)                                         # 카운트다운 끝
        for i in range(7, 10): r.offer(_Frames(i), _Align(), np)
        self.assertTrue(all(f.kept for f in warmup))             # 프리롤도 실제 녹화와 같이 붙잡아 넘긴다
        r.close(); _wait_done(r); r.pre_rec._writer_thread.join(10)
        self.assertGreaterEqual(r.first_at, r.rec_at)
        self.assertEqual((r.rec._out["frame_count"], r.missing, r.accepted), (3, 0, 3))   # 프리롤 → 녹화 사이 간격은 누락이 아님
        self.assertEqual((r.warmed, r.pre_missing), (5, 2))
        np.testing.assert_array_equal(np.load(ep / "cam_1_depth" / "frame_000000.npy"), np.full((480, 640), 507, np.uint16))
        self.assertEqual(len(list((pre / "cam_1_depth").glob("*.npy"))), 5)   # 프리롤은 mp4 · npy 를 프리롤 폴더에 실제로 쓴다
        self.assertEqual(r.pre_rec._out["frame_count"], 5)

    def test_session_preroll_delays_recording_and_cleans_up(self):
        svc = _Service(); s = warm.WarmSession(svc); pre = self.out / "preroll" / "ep"
        t0 = time.time(); s.start(self.out / "ep", seconds=5, preroll=0.3, preroll_dir=pre)
        self.assertAlmostEqual(s.rec_at, t0 + 0.35, delta=0.05)
        self.assertTrue(all(c._recording.rec_at == s.rec_at for c in svc._cameras.values()))
        self.assertTrue(all(c._recording._raw.maxsize >= 30 * 7 for c in svc._cameras.values()))
        stop = threading.Event()
        def feed(cam):
            i = 0
            while not stop.is_set():
                rec = cam._recording
                if rec: rec.offer(_Frames(i), _Align(), np); i += 1
                time.sleep(1 / 30)
        ts = [threading.Thread(target=feed, args=(c,), daemon=True) for c in svc._cameras.values()]
        [t.start() for t in ts]
        with self.assertRaises(RuntimeError): s.wait_first(timeout=0.15)   # 카운트다운 중에는 기록하지 않는다
        first = s.wait_first(timeout=2); self.assertGreaterEqual(first, s.rec_at)
        time.sleep(0.2); res = s.stop(); stop.set()
        self.assertTrue(all("error" not in r and r["frame_count"] > 0 and r["preroll_frames"] > 0 for r in res.values()), res)
        self.assertTrue(pre.exists() or s.preroll_cleanup is not None)
        s.preroll_cleanup.join(10)
        self.assertFalse(pre.exists())                           # 프리롤 폴더는 녹화가 끝나면 지운다
        self.assertTrue((self.out / "ep" / "cam_1.mp4").exists())

    def test_session_without_preroll_is_unchanged(self):
        svc = _Service(); s = warm.WarmSession(svc); s.start(self.out)
        r = svc._cameras["cam_1"]._recording
        self.assertEqual(r.rec_at, r.go_at); self.assertIsNone(r.pre_rec); self.assertIsNone(s._preroll_dir)
        s.stop()

    def test_session_refuses_when_a_camera_has_no_video(self):
        svc = _Service(); svc._cameras["cam_3"].get_status = lambda: {"connected": False}
        with self.assertRaises(RuntimeError): warm.WarmSession(svc).start(self.out)
        self.assertTrue(all(c._recording is None for c in svc._cameras.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
