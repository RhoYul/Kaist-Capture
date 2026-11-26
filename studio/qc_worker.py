"""QC 한 건을 별도 프로세스로 실행한다 (export 서버의 QC 대기열이 띄운다).

    python -m studio.qc_worker <에피소드 폴더> <장면 1D|2D|3D> <결과 json 경로>

PhysicalAI 원본 QC(vendor/capture_qc.py) · 장면별 기준(presets.py)을 그대로 부른다. 결과만 JSON 으로 쓴다.
별도 프로세스라 녹화가 시작되면 export 서버가 바로 멈출(종료할) 수 있고, 끝나면 처음부터 다시 돌린다.
"""
from __future__ import annotations
import json, math, sys
from pathlib import Path


def clean(value):
    if isinstance(value, dict): return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [clean(x) for x in value]
    if isinstance(value, float) and not math.isfinite(value): return None
    if hasattr(value, 'item') and callable(value.item):   # numpy 숫자
        try: return clean(value.item())
        except Exception: pass
    return value


def main(argv):
    folder, scene, out = argv[1], argv[2], Path(argv[3])
    from .presets import PRESETS
    from .vendor.capture_qc import run_capture_qc
    try:
        result = run_capture_qc(folder, **PRESETS[scene])
    except Exception as e:
        result = dict(ok=False, error=f'{type(e).__name__}: {e}')
    tmp = out.with_suffix('.tmp')
    tmp.write_text(json.dumps(clean(result), ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, 'tolist') else str(o)), encoding='utf-8')
    tmp.replace(out)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
