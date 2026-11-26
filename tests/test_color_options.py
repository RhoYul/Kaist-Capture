"""RGB 노출 우선 · 화이트밸런스 고정 (warm._apply_color_options · _lock_white_balance) — 가짜 SDK 로 확인."""
import types, unittest
from unittest import mock

from studio import warm


class _Range:
    def __init__(self, lo, hi, step, default):
        self.min, self.max, self.step, self.default = lo, hi, step, default


class _Sensor:
    def __init__(self, wb=4600, supports=True):
        self.opts = {'auto_exposure_priority': 1, 'enable_auto_white_balance': 1, 'white_balance': wb}
        self._supports = supports

    def supports(self, o): return self._supports
    def set_option(self, o, v): self.opts[o] = v
    def get_option(self, o): return self.opts[o]
    def get_option_range(self, o): return _Range(2800, 6500, 10, 4600)


RS = types.SimpleNamespace(option=types.SimpleNamespace(
    auto_exposure_priority='auto_exposure_priority', enable_auto_white_balance='enable_auto_white_balance',
    white_balance='white_balance'))


class ColorOptions(unittest.TestCase):
    def setUp(self):
        self._saved = (warm.WHITE_BALANCE, warm.AE_PRIORITY, warm._wb_locked, warm.WB_SETTLE_S)
        warm.WB_SETTLE_S = 0

    def tearDown(self):
        warm.WHITE_BALANCE, warm.AE_PRIORITY, warm._wb_locked, warm.WB_SETTLE_S = self._saved

    def test_exposure_priority_off_and_fixed_wb(self):
        warm.WHITE_BALANCE, warm.AE_PRIORITY = 5003, False
        s = _Sensor()
        note = warm._apply_color_options(RS, s, 'cam_1')
        self.assertEqual(s.opts['auto_exposure_priority'], 0)
        self.assertEqual(s.opts['enable_auto_white_balance'], 0)
        self.assertEqual(s.opts['white_balance'], 5000)        # 장치 단위(10K)로 맞춤
        self.assertIn('5000K 고정', note)

    def test_wb_clamped_and_string_number(self):
        warm.WHITE_BALANCE = '9000'
        s = _Sensor(); warm._apply_color_options(RS, s, 'cam_1')
        self.assertEqual(s.opts['white_balance'], 6500)

    def test_auto_keeps_auto(self):
        warm.WHITE_BALANCE = 'auto'
        s = _Sensor(); s.opts['enable_auto_white_balance'] = 0
        warm._apply_color_options(RS, s, 'cam_1')
        self.assertEqual(s.opts['enable_auto_white_balance'], 1)

    def test_lock_waits_then_uses_locked_value_on_reopen(self):
        warm.WHITE_BALANCE, warm._wb_locked = 'lock', None
        s = _Sensor(); warm._apply_color_options(RS, s, 'cam_1')
        self.assertEqual(s.opts['enable_auto_white_balance'], 1)   # 아직 고정 전 → 자동
        warm._wb_locked = 4210
        s2 = _Sensor(); warm._apply_color_options(RS, s2, 'cam_1')
        self.assertEqual((s2.opts['enable_auto_white_balance'], s2.opts['white_balance']), (0, 4210))

    def test_unsupported_sensor_is_ignored(self):
        warm.WHITE_BALANCE = 4600
        s = _Sensor(supports=False)
        warm._apply_color_options(RS, s, 'cam_1')
        self.assertEqual(s.opts['enable_auto_white_balance'], 1)
        self.assertEqual(warm._apply_color_options(RS, None, 'cam_1'), 'RGB 센서 없음')

    def test_lock_white_balance_uses_median_for_all(self):
        warm.WHITE_BALANCE, warm._wb_locked = 'lock', None
        sensors = [_Sensor(4000), _Sensor(4400), _Sensor(5200), _Sensor(4600)]
        svc = warm.WarmCameraService.__new__(warm.WarmCameraService)
        svc._cameras = {f'cam_{i+1}': types.SimpleNamespace(camera_id=f'cam_{i+1}', _color_sensor=s) for i, s in enumerate(sensors)}
        with mock.patch.object(warm, '_rs', return_value=RS):
            svc._lock_white_balance()
        self.assertEqual(warm._wb_locked, 4500)                  # (4400 + 4600) / 2
        for s in sensors:
            self.assertEqual((s.opts['enable_auto_white_balance'], s.opts['white_balance']), (0, 4500))


if __name__ == '__main__':
    unittest.main()
