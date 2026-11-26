"""PhysicalAI KAIST Platform 서버 관리 (Linux) — tools/Server.ps1 과 같은 동작을 표준 라이브러리만으로 한다.
    <python> tools/studioctl.py start|stop|restart|status [--role all|capture|export|control] [--config-dir production-config]
    <python> tools/studioctl.py register-local      # 같은 PC 의 export 서버를 관제 화면에 등록 (이미 있으면 그대로)
- start  : 꺼져 있으면 백그라운드로 켠다 (export → capture → control 순서). 로그는 logs/<역할>.out.log · .err.log
- stop   : 서버에 '정상 종료'를 요청 → 카메라 스트림을 닫고 스스로 끝난다. 15초 안에 안 꺼질 때만 강제 종료
           촬영 · QC · 전송 · NAS 저장 중이면(409) 끄지 않고 멈춘다 (데이터 보호)
- status : 켜져 있는지 · 카메라 연결 수
Python 은 이 파일을 실행한 Python 을 그대로 쓴다 (tools/linux/studio.sh 가 .venv 의 Python 으로 부른다).
"""
import argparse, http.cookiejar, json, os, signal, socket, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / 'logs'
NAMES = {'capture': '촬영 PC 서버', 'export': 'export 서버', 'control': '관제 서버'}
ORDER = ('export', 'capture', 'control')


def cfg_path(role, cfg_dir, ctl_dir):
    return (ctl_dir / 'control.json') if role == 'control' else (cfg_dir / (role + '.json'))


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def token_of(role, cfg):
    return cfg['admin_password'] if role == 'control' else cfg['transfer_token']


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def call_api(method, url, body=None, headers=None, opener=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**({'Content-Type': 'application/json'} if data else {}), **(headers or {})})
    with (opener.open if opener else urllib.request.urlopen)(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def pids_on_port(port):
    """포트를 듣고 있는 프로세스 (ss 가 있으면 ss, 없으면 /proc 으로)."""
    try:
        out = subprocess.run(['ss', '-ltnpH', f'sport = :{port}'], capture_output=True, text=True, timeout=5).stdout
        return sorted({int(p.split('=')[1]) for p in out.replace(',', ' ').split() if p.startswith('pid=')})
    except (OSError, subprocess.SubprocessError):
        return []


def start_one(role, cfg_file):
    cfg = load(cfg_file); port = int(cfg['port'])
    if port_open(port):
        print(f'  {NAMES[role]} 이미 켜져 있음 (포트 {port})'); return True
    LOGS.mkdir(exist_ok=True)
    out, err = LOGS / f'{role}.out.log', LOGS / f'{role}.err.log'
    for log in (out, err):   # 직전 로그는 .prev 로 남긴다
        if log.exists(): log.replace(log.with_name(log.name + '.prev'))
    with open(out, 'ab') as o, open(err, 'ab') as e:
        proc = subprocess.Popen([sys.executable, '-m', 'studio', 'serve', '--config', str(cfg_file)], cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=o, stderr=e, start_new_session=True)
    for _ in range(60):
        time.sleep(0.5)
        if proc.poll() is not None: break
        if not port_open(port): continue
        try:
            call_api('GET', f'http://127.0.0.1:{port}/api/info', timeout=2)
            print(f'  {NAMES[role]} 켜짐 (포트 {port})'); return True
        except (OSError, ValueError):
            pass
    if proc.poll() is not None: print(f'  {NAMES[role]} 가 시작하다 종료되었습니다 (코드 {proc.returncode}). 로그: {err}')
    else: print(f'  {NAMES[role]} 가 30초 안에 응답하지 않습니다. 로그를 확인하세요: {err}')
    if err.exists():
        for line in err.read_text(encoding='utf-8', errors='replace').splitlines()[-15:]: print('    ' + line)
    return False


def stop_one(role, cfg_file):
    cfg = load(cfg_file); port = int(cfg['port'])
    if not port_open(port):
        print(f'  {NAMES[role]} 꺼져 있음'); return True
    print(f'  {NAMES[role]} 정상 종료 요청' + (' — 카메라를 닫고 끝납니다' if role == 'capture' else ''))
    try:
        call_api('POST', f'http://127.0.0.1:{port}/api/machine/shutdown', headers={'Authorization': 'Bearer ' + token_of(role, cfg)})
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print('  지금은 끌 수 없습니다: ' + e.read().decode('utf-8', 'replace'))
            print('  진행 중인 작업이 끝난 뒤 다시 실행하세요. (서버는 그대로 켜져 있습니다)'); return False
        print(f'  정상 종료 요청 실패 ({e.code}) — 기다린 뒤 필요하면 강제 종료합니다')
    except OSError:
        pass   # 종료하면서 연결이 끊기는 경우
    for _ in range(30):
        if not port_open(port): break
        time.sleep(0.5)
    if port_open(port):
        print('  정상 종료되지 않아 강제 종료합니다' + (' (카메라가 안 나오면 USB 를 다시 꽂으세요)' if role == 'capture' else ''))
        for pid in pids_on_port(port):
            try: os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError: pass
        time.sleep(2)
    else:
        print(f'  {NAMES[role]} 꺼짐')
    if role == 'capture': time.sleep(2)   # 카메라 장치가 풀릴 시간
    return True


def show_one(role, cfg_file):
    cfg = load(cfg_file); port = int(cfg['port'])
    if not port_open(port):
        print(f'  {NAMES[role]} : 꺼짐 (포트 {port})'); return False
    line = f'  {NAMES[role]} : 켜짐 (포트 {port})'
    if role == 'capture':
        try:
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            call_api('POST', f'http://127.0.0.1:{port}/api/login', {'role': 'worker', 'password': cfg['worker_password']}, opener=op, timeout=5)
            st = call_api('GET', f'http://127.0.0.1:{port}/api/state', opener=op, timeout=20)
            line += f" · 카메라 {sum(1 for c in st.get('cameras', []) if c.get('connected'))}/4 연결"
        except (OSError, ValueError):
            pass
    print(line); return True


def register_local(cfg_dir, ctl_dir):
    """관제 화면에 같은 PC 의 export 서버(127.0.0.1:8766)를 등록한다. 이미 같은 주소가 있으면 토큰만 갱신된다."""
    ctl = load(ctl_dir / 'control.json'); exp = load(cfg_dir / 'export.json')
    base = f"http://127.0.0.1:{int(ctl['port'])}"
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    call_api('POST', base + '/api/login', {'password': ctl['admin_password']}, opener=op)
    url = f"http://127.0.0.1:{int(exp['port'])}"
    if any(p.get('url') == url for p in call_api('GET', base + '/api/pcs', opener=op) or []):
        print(f'  관제 화면에 이미 등록됨: {url}'); return True
    call_api('POST', base + '/api/pcs', {'name': exp.get('station') or 'capture-01', 'url': url, 'token': exp['transfer_token']}, opener=op, timeout=20)
    print(f'  관제 화면에 등록했습니다: {url}'); return True


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('action', choices=['start', 'stop', 'restart', 'status', 'register-local'])
    p.add_argument('--role', default='all', choices=['all', 'capture', 'export', 'control'])
    p.add_argument('--config-dir', default=os.environ.get('STUDIO_CONFIG', 'production-config'))
    p.add_argument('--control-dir', default=os.environ.get('STUDIO_CONTROL_CONFIG', 'control-config'))
    a = p.parse_args()
    cfg_dir, ctl_dir = ROOT / a.config_dir, ROOT / a.control_dir
    if a.action == 'register-local':
        try: return 0 if register_local(cfg_dir, ctl_dir) else 1
        except (OSError, ValueError, KeyError) as e:
            print(f'  관제 화면 등록 실패: {e}'); return 1
    present = [r for r in ORDER if cfg_path(r, cfg_dir, ctl_dir).exists()]
    roles = present if a.role == 'all' else [a.role]
    if not roles:
        print('이 PC 에 서버 설정이 없습니다. tools/linux/setup-capture.sh 를 먼저 실행하세요.'); return 1
    files = {r: cfg_path(r, cfg_dir, ctl_dir) for r in roles}
    ok = True
    try:
        if a.action in ('stop', 'restart'):
            print('== PhysicalAI KAIST Platform ' + ('끄기' if a.action == 'stop' else '재시작'))
            ok = all([stop_one(r, files[r]) for r in reversed(roles)])
        if a.action == 'start' or (a.action == 'restart' and ok):
            if a.action == 'start': print('== PhysicalAI KAIST Platform 켜기')
            ok = all([start_one(r, files[r]) for r in roles]) and ok
        if a.action == 'status':
            print('== PhysicalAI KAIST Platform 상태')
            ok = all([show_one(r, files[r]) for r in roles])
    except (OSError, ValueError, KeyError) as e:
        print(f'오류: {e}'); ok = False
    if ok and a.action in ('start', 'restart'):
        if 'capture' in roles: print(f"촬영 화면: http://localhost:{load(files['capture'])['port']}")
        if 'control' in roles: print(f"관제 화면: http://localhost:{load(files['control'])['port']}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
