from __future__ import annotations
import hashlib, json, os, re, sqlite3, threading, uuid
from datetime import datetime, timezone
from pathlib import Path

def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix=''): return prefix + uuid.uuid4().hex

def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(value)):
        raise ValueError('잘못된 식별자')
    return str(value)

def digest(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()

def save_json(p, data):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(p)

class Store:
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'state.sqlite3'
        self.lock = threading.RLock()
        with self.connect() as c:
            c.executescript('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS records (kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id)); CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,level TEXT,entity TEXT,message TEXT);')
            cols={r[1] for r in c.execute('PRAGMA table_info(events)')}
            if 'episode' not in cols:   # 로그에 에피소드 ID (예전 DB 는 열을 더하고, 에피소드 로그는 entity 로 채운다)
                c.execute('ALTER TABLE events ADD COLUMN episode TEXT')
                c.execute("UPDATE events SET episode=entity WHERE entity LIKE 'ep\\_%' ESCAPE '\\'")
            if 'station' not in cols:
                c.execute('ALTER TABLE events ADD COLUMN station TEXT')
            c.execute('CREATE INDEX IF NOT EXISTS events_episode ON events(episode)')
    def connect(self): return sqlite3.connect(self.path, timeout=30)
    def get(self, kind, key):
        with self.connect() as c:
            r = c.execute('SELECT body FROM records WHERE kind=? AND id=?',(kind,key)).fetchone()
        return json.loads(r[0]) if r else None
    def put(self, kind, key, data):
        data = dict(data, id=key, updated_at=now())
        with self.lock, self.connect() as c:
            c.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)',(kind,key,json.dumps(data,ensure_ascii=False,allow_nan=False)))
        return data
    def patch(self, kind, key, **changes):
        with self.lock:
            return self.put(kind,key,dict(self.get(kind,key) or {},**changes))
    def all(self, kind, drop=()):
        """drop: 빼고 읽을 최상위 키 (예: 에피소드마다 복사된 calibration). SQLite 가 빼고 주므로 읽기 · JSON 풀기가 빨라진다."""
        col='json_remove(body,'+','.join('?'*len(drop))+')' if drop else 'body'
        with self.connect() as c: rows=c.execute('SELECT '+col+' FROM records WHERE kind=? ORDER BY rowid DESC',(*('$.'+k for k in drop),kind)).fetchall()
        return [json.loads(r[0]) for r in rows]
    def log(self, entity, message, level='info', episode=None, station=None):
        if episode is None and str(entity).startswith('ep_'): episode=entity   # 에피소드 로그는 entity 가 에피소드 ID
        with self.connect() as c:
            c.execute('INSERT INTO events(at,level,entity,message,episode,station) VALUES (?,?,?,?,?,?)',(now(),level,entity,str(message),episode,station))
    def logs(self, entity=None, episode=None, q=None, level=None, station=None, limit=300):
        """로그 탐색. episode · q 는 부분 일치, level · station · entity 는 일치. 최신 순."""
        where=[]; args=[]
        if entity: where.append('entity=?'); args.append(entity)
        if episode: where.append('episode LIKE ?'); args.append('%'+episode+'%')
        if level: where.append('level=?'); args.append(level)
        if station: where.append('station=?'); args.append(station)
        if q: where.append('(message LIKE ? OR episode LIKE ? OR entity LIKE ?)'); args += ['%'+q+'%']*3
        sql='SELECT * FROM events'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY seq DESC LIMIT ?'
        with self.connect() as c:
            c.row_factory=sqlite3.Row
            r=c.execute(sql,args+[max(1,min(int(limit),2000))]).fetchall()
        return [dict(x) for x in r]

DEFAULTS = dict(role='capture', host='127.0.0.1', port=8765, data_dir='./data', demo=False,
                station='station-01', export_url='http://127.0.0.1:8766', nas_root='',
                png_workers=2, max_upload_bytes=8*1024**3, worker_password='', admin_password='', transfer_token='',
                # 촬영 PC 저장 공간: NAS 저장 · 체크섬 확인이 끝난 원본은 지운다 / 여유 공간이 이보다 적으면 촬영을 막는다
                delete_after_nas=True, min_free_gb=10,
                # export 서버가 같은 PC 면 원본 폴더를 바로 NAS 로 (ZIP · HTTP 전송 생략)
                local_fastpath=True,
                # 촬영 해상도 (컬러 · 깊이 같은 값, 4대 공통). PhysicalAI MANO 손 피팅(data_process_hands.py RES)이 848×480 기준.
                # 848×480 은 카메라가 USB 3 로 연결되어야 나온다 (USB 2.x 는 640×480 만)
                # FPS 는 4대 공통 30 (촬영 · 저장 같음). 프레임 번호는 4대 공통 시간 격자로 맞춘다 (warm._Grid)
                capture_width=848, capture_height=480, capture_fps=30,
                # 카메라 SDK 안의 프레임 버퍼 (장). 촬영 서버가 잠깐 바빠 프레임을 늦게 꺼내도 버려지지 않게 한다. 0 = 예전 방식 (버퍼 1장)
                sdk_queue=32,
                # RGB 화이트밸런스: "lock" = 켤 때 자동 값이 자리 잡으면 4대 가운데 값으로 같이 고정 · 숫자 = 그 색온도(K)로 고정 · "auto" = 카메라마다 자동
                white_balance='lock',
                # RGB 노출 우선: False = 끔 (어두워도 fps 를 낮추지 않는다) · True = 카메라 기본 (어두우면 fps 가 떨어질 수 있음)
                auto_exposure_priority=False,
                # 연속 촬영: QC 를 안 했거나 검수를 기다리는 에피소드가 이만큼 쌓이면 촬영을 막는다 (QC 일괄 실행 · 검수 후 계속)
                max_pending=10,
                # QC 전체 PASS 에피소드는 자동 승인 · NAS 전송. WARN · FAIL 이 하나라도 있으면 작업자 검수를 기다린다
                auto_export=True,
                # export 서버의 QC 동시 실행 수 (QC 는 CPU · 디스크를 많이 쓴다. 녹화 중에는 일시정지했다가 끝나면 이어서 돈다)
                # qc_pause=False 면 예전처럼 녹화가 시작될 때 QC 를 종료하고 처음부터 다시 돌린다
                qc_workers=3, qc_pause=True,
                # export 서버가 NAS 로 복사 · 체크섬 확인할 때 동시에 처리하는 파일 수 (1 이면 하나씩)
                nas_workers=4)
RESOLUTIONS = {(640, 480), (848, 480), (1280, 720)}

def load_config(p):
    cfg=dict(DEFAULTS,**json.loads(Path(p).read_text(encoding='utf-8-sig')))
    need=('admin_password',) if cfg.get('role')=='control' else ('worker_password','admin_password','transfer_token')
    # 관제 서버는 이 PC 에서만 열리므로 (host 127.0.0.1 강제) 8자 이상, 사내망에 열리는 촬영 PC 값은 12자 이상
    minimum=8 if cfg.get('role')=='control' else 12
    for k in need:
        if len(cfg.get(k,''))<minimum: raise ValueError(k+'는 '+str(minimum)+'자 이상 설정하세요.')
    if cfg['role'] not in ('capture','export','control'): raise ValueError('role: capture · export · control')
    # 촬영 PC 이름은 서버 간 통신 · 기록의 식별자라 영문 · 숫자 · - · _ 만 (한글 · 공백이면 상태 보고 · NAS 저장이 거부된다)
    if cfg['role']!='control' and not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',str(cfg.get('station',''))):
        raise ValueError("station(촬영 PC 이름) '"+str(cfg.get('station'))+"' 은 쓸 수 없습니다. 영문 · 숫자 · - · _ 만 (예: capture-01). "
                         +'production-config 의 capture.json · export.json 둘 다 같은 값으로 고친 뒤 다시 켜세요.')
    if cfg['role']=='control' and cfg['host'] not in ('127.0.0.1','localhost','::1'): raise ValueError('관제 서버는 이 PC 에서만 (host 127.0.0.1)')
    if cfg['role']=='capture':
        size=(int(cfg['capture_width']),int(cfg['capture_height']))
        if size not in RESOLUTIONS: raise ValueError('capture_width × capture_height 는 '+' · '.join(f'{w}×{h}' for w,h in sorted(RESOLUTIONS))+' 중 하나')
        if int(cfg['capture_fps']) not in (15,30): raise ValueError('capture_fps 는 15 또는 30')
        cfg['capture_width'],cfg['capture_height'],cfg['capture_fps']=size[0],size[1],int(cfg['capture_fps'])
    cfg['data_dir']=str(Path(cfg['data_dir']).resolve())
    return cfg
