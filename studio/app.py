from __future__ import annotations
import asyncio, hmac, json, os, secrets, sys, threading, time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import FileResponse, Response, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .core import Store, safe_id, uid, now, overlap, newest
from .capture import Capture
from .exporter import Exporter, nas_candidates, resolve_nas_cached
from .vendor.calibration import ExtrinsicCalibrationError
from . import update

class Login(BaseModel):
    role: str
    password: str
class Task(BaseModel):
    name: str=Field(min_length=1,max_length=100)
    scene: str='3D'
    target: int=Field(default=100,ge=1,le=1000000)
    # PhysicalAI Task 의 'RGB-D 처리 물체 정보' (create-section.tsx RGBD_FIELDS). 다섯 칸 모두 필수 — PhysicalAI Phase 1 과 같은 기준
    prompt: str=Field(min_length=1,max_length=200)        # SAM 프롬프트
    label: str=Field(min_length=1,max_length=100)         # 마스크 라벨
    object_name: str=Field(min_length=1,max_length=200)   # 납품 물체 이름 (object_name)
    interaction: str=Field(min_length=1,max_length=500)   # 상호작용 설명
class Calib(BaseModel):
    # 현장 보드: 7x5칸, 칸 50mm, 마커 25mm, DICT_6X6_50
    squares_x: int=Field(default=7,ge=2,le=50)
    squares_y: int=Field(default=5,ge=2,le=50)
    square_length_mm: float=Field(default=50,gt=0,le=500)
    marker_length_mm: float=Field(default=25,gt=0,le=500)
    aruco_dict_name: str='DICT_6X6_50'
class Review(BaseModel):
    note: str=Field(default='',max_length=2000)


def create_app(cfg):
    db=Store(cfg['data_dir']); sessions={}; attempts={}
    # 쿠키는 포트를 구분하지 않는다 → 같은 PC의 촬영 PC(8765)·export(8766)가 서로 로그인을 덮어쓰지 않게 이름을 나눈다
    COOKIE='studio_session_'+str(cfg['port'])
    capture=Capture(cfg,db) if cfg['role']=='capture' else None
    exporter=Exporter(cfg,db) if cfg['role']=='export' else None
    install_root=Path(cfg.get('install_root') or update.ROOT)
    VERSION=update.version(install_root)   # 지금 돌고 있는 코드 버전 (관제 화면 · 업데이트 확인용)
    # 이 서버가 켜진 뒤로만 통하는 표식. 화면 · 촬영 서버는 목록(에피소드 · NAS 작업 · QC)을 한 번만 전체로 받고 그 뒤로는
    # '그 뒤에 바뀐 것만' 받아 합친다 — 서버가 다시 켜져 표식이 달라지면 전체를 다시 받는다.
    BOOT=uid('boot_')
    # export 서버의 작업 · QC 목록 사본 (촬영 서버가 들고 있는다). 상태 화면(2초) · 하트비트(5초)가 같이 쓴다
    rlock=threading.RLock(); rcache=dict(epoch=None,cursor=None,jobs={},qc={},tasks=[])
    def remote_snapshot():
        """지금 들고 있는 사본 (export 서버에 묻지 않는다). 최근에 바뀐 것부터."""
        with rlock:
            recent=lambda d:sorted(d.values(),key=lambda x:str(x.get('updated_at') or ''),reverse=True)
            return dict(tasks=list(rcache['tasks']),jobs=recent(rcache['jobs']),qc=recent(rcache['qc']),epoch=rcache['epoch'] or '')
    def remote_state():
        """export 서버의 작업(Task) · NAS 작업 · QC 목록. 처음 한 번만 전체를 받고 그 뒤로는 바뀐 것만 받아 사본에 합친다.
        (기록이 수천 건이면 전체는 1MB 를 넘고, export 서버가 읽어 묶는 데만 1초 가까이 걸렸다 — 화면이 2초마다 그걸 기다렸다.)
        export 서버가 다시 켜졌거나 (epoch 가 달라짐) 예전 코드라 표식을 주지 않으면 전체를 받는다."""
        with rlock:
            params=dict(since=rcache['cursor'],epoch=rcache['epoch']) if rcache['epoch'] and rcache['cursor'] else None
            data=remote('GET','/api/machine/state',params=params)
            if data.get('full',True) or data.get('epoch')!=rcache['epoch']: rcache['jobs']={}; rcache['qc']={}
            for j in data['jobs']: rcache['jobs'][j['id']]=j
            for q in data.get('qc',[]): rcache['qc'][q.get('id')]=q
            rcache.update(epoch=data.get('epoch'),cursor=data.get('cursor'),tasks=data['tasks'])
            return remote_snapshot()
    @asynccontextmanager
    async def lifespan(app):
        def report():
            eps=capture.active_episodes()   # 이 PC 에 원본이 남은 에피소드만 (기록 전체를 읽어 풀면 수백 ms)
            cams=capture.cameras() if cfg['demo'] else (capture.preview.list_cameras() if capture.preview else [])
            summary=[{k:e.get(k) for k in ('id','task_id','task_name','status','transfer_status','local_deleted','updated_at')} for e in db.all('episode',drop=('calibration',),limit=20)]
            remote('POST','/api/machine/heartbeat',json=dict(station=cfg['station'],busy=capture.busy,cameras=cams,episodes=summary,
                                                             logs=db.logs()[:100],disk=capture.disk(eps)))
            data=remote_state()
            changed=capture.reconcile_qc(data['qc'],eps)   # export 서버 QC 결과 반영 · PASS 는 자동 전송
            capture.reconcile(data['jobs'],None if changed else eps)   # NAS 저장이 끝난 원본 정리
        async def heartbeat_loop():
            last_error=None
            while True:
                try:
                    # DB 읽기 · 디스크 계산까지 모두 작업 스레드에서 (이벤트 루프에서 하면 그동안 미리보기 스트림이 멈춘다)
                    await asyncio.to_thread(report)
                    if last_error:
                        db.log('export','export 서버 연결 복구 ('+cfg['export_url']+')'); print('[heartbeat] 복구',file=sys.stderr,flush=True); last_error=None
                except Exception as e:
                    # 조용히 넘기지 않는다: 원인이 바뀔 때마다 한 번씩 로그 (촬영 로그 화면 · logs\capture.err.log)
                    if isinstance(e,httpx.HTTPStatusError):
                        try: detail=e.response.json().get('detail')
                        except Exception: detail=e.response.text[:200]
                        msg='HTTP '+str(e.response.status_code)+' '+str(detail)
                    else: msg=type(e).__name__+': '+str(e)[:300]
                    if msg!=last_error:
                        last_error=msg; db.log('export','export 서버에 상태 보고 실패 ('+cfg['export_url']+'): '+msg,'error')
                        print('[heartbeat] export 서버에 상태 보고 실패: '+msg,file=sys.stderr,flush=True)
                await asyncio.sleep(5)
        monitor=asyncio.create_task(heartbeat_loop()) if capture else None
        yield
        if monitor:
            monitor.cancel()
            try: await monitor
            except asyncio.CancelledError: pass
        if capture:
            capture.executor.shutdown(wait=True);capture.transfer.shutdown(wait=True)
            if capture.preview:
                if hasattr(capture.preview,'shutdown'): capture.preview.shutdown()   # 스트림을 닫고 기다린다
                else:
                    for cam in capture.preview._cameras.values(): cam.stop()
        if exporter: exporter.stop_qc(); exporter.pool.shutdown(wait=True)
    app=FastAPI(title='PhysicalAI KAIST Platform',lifespan=lifespan)
    app.state.db=db;app.state.capture=capture;app.state.exporter=exporter
    async def auth(request:Request):
        s=sessions.get(request.cookies.get(COOKIE))
        if not s or s['expires']<time.time(): raise HTTPException(401,'로그인이 필요합니다')
        return s['role']
    async def admin(role=Depends(auth)):
        if role!='admin': raise HTTPException(403,'관리자 권한 필요')
    async def machine(request:Request):
        expected='Bearer '+cfg['transfer_token']
        if not hmac.compare_digest(request.headers.get('authorization',''),expected): raise HTTPException(401,'서버 인증 실패')
        if not exporter: raise HTTPException(409,'export 서버 전용 API')
    def need_capture():
        if not capture: raise HTTPException(409,'촬영 PC에서 실행하세요')
    @app.middleware('http')
    async def guard(request,call_next):
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('origin')
            if origin:
                from urllib.parse import urlsplit
                if urlsplit(origin).netloc!=request.headers.get('host'): return JSONResponse({'detail':'다른 사이트의 요청 차단'},403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        if request.url.path.startswith('/api'):
            if 'cache-control' not in response.headers: response.headers['Cache-Control']='no-store'   # 검수 프레임처럼 바뀌지 않는 응답은 자체 값 유지
        # 화면 파일은 매번 새 버전인지 확인시킨다 (바뀌지 않았으면 304라 가볍다). 업데이트 뒤 옛 화면이 남는 것을 막는다
        elif request.url.path.startswith('/static') or request.url.path in ('/','/admin'): response.headers['Cache-Control']='no-cache'
        return response
    @app.exception_handler(ValueError)
    async def value_error(request,exc): return JSONResponse({'detail':str(exc)},400)
    @app.exception_handler(ExtrinsicCalibrationError)
    async def calibration_error(request,exc):
        return JSONResponse({'detail':str(exc)+' · 보드 칸 수·딕셔너리가 실제 보드와 같은지 확인하세요'},400)
    @app.get('/api/info')
    def info(): return dict(role=cfg['role'],station=cfg['station'],demo=cfg['demo'],version=VERSION)
    @app.post('/api/login')
    def login(body:Login,request:Request,response:Response):
        ip=request.client.host if request.client else 'local'; a=attempts.get(ip,[0,0])
        if a[0]>=10 and time.time()-a[1]<60: raise HTTPException(429,'잠시 후 다시 시도하세요')
        expected=cfg.get(body.role+'_password','') if body.role in ('admin','worker') else ''
        if not expected or not hmac.compare_digest(body.password,expected):
            attempts[ip]=[a[0]+1,time.time()];raise HTTPException(401,'비밀번호를 확인하세요')
        attempts.pop(ip,None); token=secrets.token_urlsafe(32)
        sessions[token]=dict(role=body.role,expires=time.time()+8*3600)
        response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=8*3600)
        return dict(role=body.role)
    @app.post('/api/logout')
    def logout(request:Request,response:Response):
        sessions.pop(request.cookies.get(COOKIE),None);response.delete_cookie(COOKIE);return {'ok':True}
    def remote(method,url,**kwargs):
        with httpx.Client(base_url=cfg['export_url'],headers={'Authorization':'Bearer '+cfg['transfer_token']},timeout=4,trust_env=False) as c:
            r=c.request(method,url,**kwargs);r.raise_for_status();return r.json()
    @app.get('/api/state')
    def state(role=Depends(auth),since:str|None=None,jsince:str|None=None,epoch:str|None=None):
        """화면이 2초마다 묻는다. 에피소드 · NAS 작업 목록은 처음(또는 서버가 다시 켜진 뒤) 한 번만 전체를 주고,
        그 뒤로는 화면이 돌려준 커서(since · jsince) 뒤에 바뀐 것만 준다 (episodes_full · jobs_full 이 False). 화면이 받아 합친다.
        (예전에는 매번 전체 — 에피소드 1,800건이면 6MB 를 2초마다 읽고 · 묶고 · 보내고 · 풀었다.)"""
        tasks=db.all('task'); server_error=None; all_jobs=None; repoch=''
        # 서버 쪽 집계 · 정리는 이 PC 에 원본이 남은 에피소드만 본다 (NAS 로 보내고 지운 기록은 볼 일이 없다)
        eps=capture.active_episodes() if capture else []
        if capture:
            try:
                data=remote_state()
                same=lambda a,b:{k:v for k,v in a.items() if k!='updated_at'}=={k:v for k,v in b.items() if k!='updated_at'}
                known={t['id']:t for t in tasks};tasks=data['tasks'];all_jobs=data['jobs'];repoch=data['epoch']
                for t in tasks:
                    if t['id'] not in known or not same(known[t['id']],t): db.put('task',t['id'],t)   # 바뀐 작업만 쓴다
                changed=capture.reconcile_qc(data['qc'],eps)
                changed=capture.reconcile(all_jobs,None if changed else eps) or changed
                if changed: eps=capture.active_episodes()
            except Exception:
                server_error='export 서버 연결 안 됨 · 마지막 작업 목록 사용 중'
                snap=remote_snapshot(); all_jobs=snap['jobs']; repoch=snap['epoch']
        # 화면에 줄 목록: 커서가 이 서버(와 export 서버)의 것이면 그 뒤에 바뀐 것만, 아니면 전체
        my_epoch=BOOT+'|'+repoch; delta=bool(epoch) and epoch==my_epoch
        es=overlap(since) if delta and since else None; js=overlap(jsince) if delta and jsince else None
        out_eps=db.all('episode',drop=('calibration',),since=es)   # 위 정리에서 바뀐 것까지 들어가게 마지막에 읽는다
        if capture: out_jobs=all_jobs if js is None else [j for j in all_jobs if str(j.get('updated_at') or '')>js]
        else: out_jobs=db.all('job',since=js) if exporter else []
        cams=capture.cameras() if capture else []
        cal=db.get('calibration','current')
        pend=capture.pending_episodes(eps) if capture else []
        # JSONResponse 로 바로 보낸다 — FastAPI 기본 변환(jsonable_encoder)은 에피소드가 많으면 수백 ms 걸린다 (값은 모두 DB 에서 읽은 JSON)
        jobs=out_jobs
        return JSONResponse(dict(epoch=my_epoch,episodes_full=es is None,jobs_full=js is None,
                    cursor=newest(out_eps,since if es else None),jcursor=newest(out_jobs,jsince if js else None),
                    resolution=capture.resolution() if capture else None,
                    pending=dict(total=len(pend),limit=int(cfg.get('max_pending',10)),need_qc=sum(e['status']=='captured' for e in pend),
                                 in_qc=sum(e['status'] in ('qc_queued','qc_running') for e in pend),review=sum(e['status']=='qc_done' for e in pend),
                                 auto_export=bool(cfg.get('auto_export',True)),qc_on_export=capture.qc_remote()) if capture else None,
                    calibration_mismatch=capture.calibration_mismatch(cal) if capture and cal else None,role=cfg['role'],user_role=role,station=cfg['station'],demo=cfg['demo'],tasks=tasks,jobs=jobs,
                    episodes=out_eps,cameras=cams,calibration=cal,
                    captures=len(capture.calib_sets) if capture else 0,busy=capture.busy if capture else None,
                    durations=list(capture.DURATIONS) if capture else [],live=capture.live_status() if capture else None,
                    logs=db.logs() if role=='admin' else [],server_error=server_error,disk=capture.disk(eps) if capture else None,
                    nas_configured=bool(cfg['nas_root']) if exporter else None,stations=db.all('station') if exporter else []))
    @app.post('/api/machine/heartbeat',dependencies=[Depends(machine)])
    async def heartbeat(request:Request):
        b=await request.json();station=safe_id(b['station'])
        with db.lock:
            previous=db.get('station',station) or {};last=previous.get('last_seq',0)
            logs=sorted(b.get('logs',[])[:100],key=lambda l:l['seq'])
            for line in logs:
                if line['seq']>last:
                    ep=line.get('episode') or (line.get('entity') if str(line.get('entity','')).startswith('ep_') else None)
                    db.log(station,'['+station+'] '+str(line.get('message',''))[:2000],line.get('level','info'),
                           episode=safe_id(ep) if ep else None,station=station)
                    last=line['seq']
            db.put('station',station,dict(last_seen=now(),last_seq=last,busy=b.get('busy'),cameras=b.get('cameras',[])[:4],episodes=b.get('episodes',[])[:20],
                                          disk=b.get('disk') if isinstance(b.get('disk'),dict) else None))
        return dict(ok=True)
    # ── 같은 PC 촬영 서버 → export 서버 ──
    @app.post('/api/machine/recording')
    async def recording(request:Request):
        local_machine(request)
        if not exporter: raise HTTPException(409,'export 서버 전용 API')
        exporter.set_recording(bool((await request.json()).get('active'))); return dict(ok=True)
    @app.post('/api/qc/{key}/local')
    async def local_qc(key:str,request:Request):
        """같은 PC 촬영 서버가 QC 를 맡긴다 (원본 폴더 경로 · 장면). 결과는 /api/machine/state 의 qc 로 돌려준다."""
        local_machine(request)
        if not exporter: raise HTTPException(409,'export 서버 전용 API')
        return await asyncio.to_thread(exporter.qc_begin,safe_id(key),await request.json())
    @app.post('/api/transfer/{key}/local')
    async def local_transfer(key:str,request:Request):
        local_machine(request)
        if not exporter: raise HTTPException(409,'export 서버 전용 API')
        return await asyncio.to_thread(exporter.local_begin,safe_id(key),await request.json())
    # ── 관제 PC 가 쓰는 API (서버 토큰) ──
    @app.get('/api/machine/overview',dependencies=[Depends(machine)])
    def machine_overview():
        nas=resolve_nas_cached(cfg)
        return dict(role='export',demo=cfg['demo'],nas_root=cfg['nas_root'],nas_configured=bool(nas_candidates(cfg)),
                    nas_reachable=nas is not None,nas_active=str(nas) if nas else None,
                    stations=db.all('station'),tasks=db.all('task'),jobs=db.all('job')[:100],logs=db.logs(limit=50),
                    version=VERSION,update=update.last_status(install_root))
    @app.post('/api/machine/update',dependencies=[Depends(machine)])
    async def machine_update(request:Request):
        """관제 PC 가 보낸 코드로 이 촬영 PC 를 업데이트한다. 촬영 · QC · NAS 저장 중이면 받지 않는다."""
        if update.busy_updating(install_root): raise HTTPException(409,'이미 업데이트 중입니다')
        if time.time()<exporter.recording_until: raise HTTPException(409,'녹화 중입니다. 끝난 뒤 다시 누르세요')
        from datetime import datetime
        def fresh(s):   # 30초 안에 소식이 있던 촬영 서버만 (꺼진 서버의 옛 상태로 막지 않게)
            try: return time.time()-datetime.fromisoformat(str(s.get('last_seen')).replace('Z','+00:00')).timestamp()<30
            except Exception: return False
        for s in db.all('station'):
            if s.get('busy') and fresh(s): raise HTTPException(409,'촬영 PC 가 '+{'recording':'녹화','qc':'QC','calibrating':'캘리브레이션'}.get(s['busy'],str(s['busy']))+' 중입니다. 끝난 뒤 다시 누르세요')
        if any(j['status'] in ('receiving','queued','extracting','nas_copy','verifying') for j in db.all('job')):
            raise HTTPException(409,'NAS 저장 처리 중입니다. 끝난 뒤 다시 누르세요')
        data=await request.body()
        result=await asyncio.to_thread(update.stage,install_root,data,VERSION)
        if result['version']==VERSION:
            update._write_status(install_root,status='ok',version=VERSION,**{'from':VERSION},message='이미 최신 코드입니다')
            return dict(ok=True,already=True,version=VERSION)
        update.launch_apply(install_root,result['version'])
        db.log('update','원격 업데이트 받음: '+VERSION+' → '+result['version']+' (파일 '+str(result['files'])+'개) · 서버 재시작')
        return dict(ok=True,version=result['version'],**{'from':VERSION})
    @app.post('/api/machine/tasks',dependencies=[Depends(machine)])
    def machine_create_task(body:Task): return create_task_record(body)
    @app.post('/api/machine/tasks/{key}/toggle',dependencies=[Depends(machine)])
    def machine_toggle(key:str): return toggle_task(key)
    @app.post('/api/machine/jobs/{key}/retry',dependencies=[Depends(machine)])
    def machine_retry(key:str): exporter.retry(safe_id(key)); return dict(ok=True)
    @app.get('/api/machine/logs',dependencies=[Depends(machine)])
    def machine_logs(episode:str|None=None,q:str|None=None,level:str|None=None,station:str|None=None,limit:int=300):
        return db.logs(episode=(episode or '').strip() or None,q=(q or '').strip() or None,level=level or None,station=station or None,limit=limit)
    def local_machine(request:Request):
        """이 PC 에서 서버 토큰으로만 (재시작 스크립트용)."""
        host=request.client.host if request.client else ''
        if host not in ('127.0.0.1','::1') or not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+cfg['transfer_token']):
            raise HTTPException(403,'이 PC 에서 서버 토큰으로만 가능합니다')
    @app.post('/api/machine/shutdown',dependencies=[Depends(local_machine)])
    def shutdown():
        """정상 종료: 카메라 스트림을 닫고 기다린 뒤 프로세스를 끝낸다 (강제 종료로 카메라가 멈추는 것을 막는다)."""
        if capture and capture.busy: raise HTTPException(409,'촬영 / QC / 캘리브레이션 진행 중입니다. 끝난 뒤 다시 시도하세요')
        if capture and capture.transferring: raise HTTPException(409,'서버로 전송 중입니다. 끝난 뒤 다시 시도하세요')
        if exporter and any(j['status'] in ('queued','extracting','nas_copy','verifying') for j in db.all('job')):
            raise HTTPException(409,'NAS 저장 처리 중입니다. 끝난 뒤 다시 시도하세요')
        def bye():
            time.sleep(0.3)
            try:
                if exporter: exporter.stop_qc()   # 돌던 QC 는 멈추고, 다음에 켤 때 이어서 돈다
                if capture and capture.preview and hasattr(capture.preview,'shutdown'): capture.preview.shutdown()
            finally:
                os._exit(0)
        threading.Thread(target=bye,daemon=True).start()
        return dict(ok=True)
    @app.get('/api/machine/state',dependencies=[Depends(machine)])
    def machine_state(since:str|None=None,epoch:str|None=None):
        """촬영 서버가 묻는 작업(Task) · NAS 작업 · QC 목록. 지난 응답의 cursor · epoch 를 돌려주면 그 뒤에 바뀐 것만 준다 (full=False).
        처음이거나 이 서버가 그사이 다시 켜졌으면 (epoch 가 다름) 전체 — 예전 촬영 서버 코드는 아무것도 주지 않으므로 늘 전체를 받는다."""
        s=overlap(since) if since and epoch==BOOT else None
        jobs=db.all('job',since=s); qc=db.all('qc',since=s) if s else db.all('qc',limit=200)
        return dict(tasks=db.all('task'),jobs=jobs,qc=qc,full=s is None,epoch=BOOT,cursor=newest(jobs+qc,since if s else None))
    def create_task_record(body):
        if not exporter: raise HTTPException(409,'작업은 export 서버의 관리자 화면에서 생성하세요')
        if body.scene not in ('1D','2D','3D'): raise ValueError('장면 유형 오류')
        data=body.model_dump()
        for k in ('prompt','label','object_name','interaction'):
            data[k]=data[k].strip()
            if not data[k]: raise ValueError('RGB-D 처리 물체 정보를 모두 입력하세요')
        key=uid('task_');result=db.put('task',key,dict(data,active=True,created_at=now()))
        db.log(key,'작업 생성: '+body.name);return result
    def toggle_task(key):
        t=db.get('task',safe_id(key))
        if not exporter or not t: raise HTTPException(404,'작업 없음')
        db.log(key,'작업 활성 상태 변경');return db.patch('task',key,active=not t['active'])
    @app.post('/api/tasks',dependencies=[Depends(admin)])
    def create_task(body:Task): return create_task_record(body)
    @app.post('/api/tasks/{key}/toggle',dependencies=[Depends(admin)])
    def toggle(key:str): return toggle_task(key)
    @app.get('/api/logs',dependencies=[Depends(admin)])
    def logs(entity:str|None=None,episode:str|None=None,q:str|None=None,level:str|None=None,station:str|None=None,limit:int=300):
        """로그 탐색 — episode(에피소드 ID 부분 일치) · q(내용 · ID 검색) · level · station."""
        return db.logs(entity=entity,episode=(episode or '').strip() or None,q=(q or '').strip() or None,
                       level=level or None,station=station or None,limit=limit)
    @app.get('/api/cameras/{cam}/frame',dependencies=[Depends(auth)])
    def frame(cam:str):
        need_capture()
        if cam not in ('cam_1','cam_2','cam_3','cam_4'): raise HTTPException(404)
        data=capture.frame(cam)
        if not data: raise HTTPException(503,'프레임 대기 중')
        return Response(data,media_type='image/jpeg')
    @app.get('/api/cameras/{cam}/stream',dependencies=[Depends(auth)])
    async def stream(cam:str,request:Request):
        # 미리보기 전용 MJPEG. 카메라 서비스가 이미 받은 JPEG를 그대로 보내므로 USB·촬영 경로에는 영향이 없다.
        need_capture()
        if cam not in ('cam_1','cam_2','cam_3','cam_4'): raise HTTPException(404)
        token=request.cookies.get(COOKIE)
        async def frames():
            last=None
            while not await request.is_disconnected():
                s=sessions.get(token)
                if not s or s['expires']<time.time(): break
                data=await asyncio.to_thread(capture.frame,cam)
                if data and data is not last:
                    last=data
                    yield b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data+b'\r\n'
                await asyncio.sleep(1/30)  # 카메라 15fps를 놓치지 않게 두 배로 확인. 같은 프레임은 다시 보내지 않는다
        return StreamingResponse(frames(),media_type='multipart/x-mixed-replace; boundary=frame')
    @app.get('/api/preview/stream',dependencies=[Depends(auth)])
    async def preview_stream(request:Request):
        """카메라 4대 미리보기를 연결 하나로 (화면이 칸마다 나눠 그린다).
        카메라별 스트림 4개는 브라우저의 같은 주소 동시 연결 한도(6개) 중 4개를 계속 차지해 다른 요청이 줄을 섰다.
        파트마다 X-Camera 헤더로 어느 카메라인지 표시한다. 카메라 서비스가 이미 만든 JPEG 를 그대로 보내므로 USB·촬영 경로에는 영향이 없다."""
        need_capture()
        token=request.cookies.get(COOKIE)
        async def frames():
            last={}
            while not await request.is_disconnected():
                s=sessions.get(token)
                if not s or s['expires']<time.time(): break
                for cam,data in (await asyncio.to_thread(capture.preview_frames)).items():   # 한 번에 4대 (스레드 왕복 1번)
                    if data and data is not last.get(cam):
                        last[cam]=data
                        yield (b'--frame\r\nContent-Type: image/jpeg\r\nX-Camera: '+cam.encode()+b'\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data+b'\r\n')
                await asyncio.sleep(1/30)  # 카메라 15fps를 놓치지 않게 두 배로 확인. 같은 프레임은 다시 보내지 않는다
        return StreamingResponse(frames(),media_type='multipart/mixed; boundary=frame',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
    @app.post('/api/calibration/snapshot',dependencies=[Depends(auth)])
    def snapshot(): need_capture();return dict(count=capture.snapshot())
    @app.post('/api/calibration/reset',dependencies=[Depends(auth)])
    def reset_calibration():
        need_capture()
        with capture.lock:
            if capture.busy: raise ValueError('현재 작업 완료 후 초기화하세요')
            capture.calib_sets=[];capture.calib_serials=None
            if db.get('calibration','current'): db.patch('calibration','current',confirmed=False)
        return dict(ok=True)
    @app.post('/api/calibration/compute',dependencies=[Depends(auth)])
    def calibrate(body:Calib):
        need_capture()
        if body.marker_length_mm>=body.square_length_mm: raise ValueError('마커 크기는 칸 크기보다 작아야 합니다')
        return capture.calibrate(body.model_dump())
    @app.post('/api/calibration/confirm',dependencies=[Depends(auth)])
    def confirm(): need_capture();capture.confirm_calibration();return dict(ok=True)
    @app.post('/api/capture/{task}',dependencies=[Depends(auth)])
    def start(task:str,seconds:int=5):
        need_capture();t=db.get('task',safe_id(task))
        if not t or not t.get('active'): raise ValueError('활성 작업을 선택하세요')
        return capture.start(t,seconds)
    @app.get('/api/capture/live',dependencies=[Depends(auth)])
    def capture_live():
        """녹화 타이머 (화면이 녹화 중 0.2초마다 묻는다): 첫 프레임 기록 시각 · 끝 시각 · 카메라별 기록 프레임 수."""
        need_capture();return capture.live_status()
    @app.post('/api/qc/batch',dependencies=[Depends(auth)])
    def qc_batch(): need_capture();return dict(queued=capture.qc_batch())
    @app.post('/api/episodes/{key}/qc',dependencies=[Depends(auth)])
    def qc(key:str): need_capture();capture.qc(safe_id(key));return dict(ok=True)
    @app.post('/api/episodes/{key}/approve',dependencies=[Depends(auth)])
    def approve(key:str,body:Review): need_capture();capture.approve(safe_id(key),body.note);return dict(ok=True)
    @app.post('/api/episodes/{key}/send',dependencies=[Depends(auth)])
    def send(key:str): need_capture();capture.send(safe_id(key));return dict(ok=True)
    @app.post('/api/episodes/{key}/delete',dependencies=[Depends(auth)])
    def delete_episode(key:str): need_capture();capture.delete_episode(safe_id(key));return dict(ok=True)
    @app.get('/api/my-logs',dependencies=[Depends(auth)])
    def my_logs(episode:str|None=None,q:str|None=None,level:str|None=None,limit:int=300):
        """작업자 촬영 로그 — 이 PC 의 촬영 · QC · 전송 로그 + export 서버의 NAS 저장 로그(이 촬영 PC 것만)를 시간순으로 합친다."""
        need_capture()
        ep=(episode or '').strip() or None; qq=(q or '').strip() or None
        rows=[dict(r,source='촬영') for r in db.logs(episode=ep,q=qq,level=level or None,limit=limit)]
        try:
            remote_rows=remote('GET','/api/machine/logs',params={k:v for k,v in dict(episode=ep,q=qq,level=level,station=cfg['station'],limit=limit).items() if v})
            rows+= [dict(r,source='NAS 저장') for r in remote_rows if not str(r.get('message','')).startswith('['+cfg['station']+']')]
        except Exception: pass
        rows.sort(key=lambda r:r.get('at') or '',reverse=True)
        return rows[:limit]
    @app.get('/api/episodes/{key}/video/{cam}',dependencies=[Depends(auth)])
    def video(key:str,cam:str):
        need_capture();safe_id(key)
        if cam not in ('cam_1','cam_2','cam_3','cam_4'): raise HTTPException(404)
        p=Path(cfg['data_dir'])/'episodes'/key/(cam+'.mp4')
        if not p.is_file(): raise HTTPException(404)
        # mp4v playback is browser dependent. UI also offers actual decoded RGB frames.
        return FileResponse(p,media_type='video/mp4')
    # 검수 화면 프레임 — 영상을 처음 볼 때 한 번 순서대로 풀어 JPEG 로 메모리에 둔다 (재생 · 탐색이 끊기지 않게). 최근 에피소드 2개만 보관
    frame_cache={}; frame_order=[]; frame_lock=threading.Lock(); decode_locks={}
    def episode_frames(key,cam):
        import cv2
        ck=(key,cam)
        with frame_lock:
            if ck in frame_cache: return frame_cache[ck]
            lk=decode_locks.setdefault(ck,threading.Lock())
        with lk:                           # 같은 영상을 동시에 두 번 풀지 않는다 (카메라끼리는 동시에)
            with frame_lock:
                if ck in frame_cache: return frame_cache[ck]
            return _decode_frames(key,cam,ck)
    def _decode_frames(key,cam,ck):
        import cv2
        p=Path(cfg['data_dir'])/'episodes'/key/(cam+'.mp4')
        if not p.is_file(): raise HTTPException(404)
        cap=cv2.VideoCapture(str(p)); frames=[]
        while True:
            ok,image=cap.read()
            if not ok: break
            frames.append(cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,80])[1].tobytes())
        cap.release()
        with frame_lock:
            frame_cache[ck]=frames; frame_order.append(key)
            keep=list(dict.fromkeys(reversed(frame_order)))[:2]
            for k in [k for k in frame_cache if k[0] not in keep]: frame_cache.pop(k,None)
            frame_order[:]=[k for k in frame_order if k in keep]
        return frames
    @app.get('/api/episodes/{key}/frame/{cam}',dependencies=[Depends(auth)])
    def recorded_frame(key:str,cam:str,index:int=0):
        need_capture();safe_id(key)
        if cam not in ('cam_1','cam_2','cam_3','cam_4'): raise HTTPException(404)
        frames=episode_frames(key,cam)
        if not 0<=index<len(frames): raise HTTPException(404,'프레임 없음')
        return Response(frames[index],media_type='image/jpeg',headers={'Cache-Control':'private, max-age=3600'})
    @app.post('/api/transfer/{key}',dependencies=[Depends(machine)])
    async def begin(key:str,request:Request): return await asyncio.to_thread(exporter.begin,safe_id(key),await request.json())
    @app.put('/api/transfer/{key}',dependencies=[Depends(machine)])
    async def chunk(key:str,request:Request,offset:int):
        data=bytearray()
        async for b in request.stream():
            data.extend(b)
            if len(data)>4*1024**2: raise HTTPException(413,'4MiB 청크 제한')
        return await asyncio.to_thread(exporter.chunk,safe_id(key),offset,data)
    @app.post('/api/transfer/{key}/complete',dependencies=[Depends(machine)])
    def complete(key:str): return exporter.complete(safe_id(key))
    @app.post('/api/jobs/{key}/retry',dependencies=[Depends(admin)])
    def retry(key:str):
        if not exporter: raise HTTPException(409,'export 서버에서 재시도하세요')
        exporter.retry(safe_id(key));return dict(ok=True)
    web=Path(__file__).parent/'web'
    app.mount('/static',StaticFiles(directory=web),name='static')
    @app.get('/')
    @app.get('/admin')
    def home(): return FileResponse(web/'index.html')
    return app
