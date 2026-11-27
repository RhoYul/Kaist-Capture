from __future__ import annotations
import base64, json, math, shutil, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import cv2, httpx
import numpy as np
from .core import now, uid, save_json, digest
from .presets import PRESETS
from .vendor.calibration import compute_extrinsics_charuco
from .vendor.capture_qc import run_capture_qc
from .exporter import make_bundle, CAMERAS, public_extrinsics, episode_mark, EPISODE_MARK, task_object

def clean(value):
    if isinstance(value,dict): return {k:clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [clean(x) for x in value]
    if isinstance(value,float) and not math.isfinite(value): return None
    return value

def epoch_iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t,timezone.utc).isoformat()

class Capture:
    DURATIONS=(5,10)   # 촬영 길이 (초) — 첫 프레임이 실제로 기록된 시각부터 센다
    # 녹화 전 카운트다운 3초 (고정). 촬영자가 보도록 화면 전체에 3 · 2 · 1 을 띄우고, 그동안 실제 녹화와 같은 처리(정렬 · mp4 · npy 쓰기)를
    # 버리는 폴더에 돌려 안정시킨다 (프리롤) → 녹화 시작 직후의 프레임 누락을 피한다 (실측 2026-11-27: 누락이 녹화 시작 후 약 4.5초 안에 몰림)
    COUNTDOWN=3
    def __init__(self,cfg,db):
        self.cfg,self.db=cfg,db; self.root=Path(cfg['data_dir'])
        self.lock=threading.RLock(); self.busy=None; self.session=None; self.preview=None
        self.live=None   # 지금 녹화 상태 (화면의 초 세기용): episode · seconds · phase · started(첫 프레임 epoch) · until
        self.calib_sets=[]; self.calib_serials=None
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='capture')
        self.transfer=ThreadPoolExecutor(max_workers=1,thread_name_prefix='transfer')
        self.transferring=set(); self.requires_restart=False
        for ep in db.all('episode'):
            if ep['status'] in ('recording','saving','preparing'):
                db.patch('episode',ep['id'],status='failed',error='촬영 PC 재시작. 원본 확인 후 재촬영하세요.')
            if ep['status'] in ('qc_queued','qc_running') and not self.export_is_local():
                db.patch('episode',ep['id'],status='captured',error='촬영 PC 재시작으로 QC 중단. 다시 실행하세요.')   # export 서버 QC 는 거기서 이어서 돈다
            if ep.get('transfer_status') in ('packing','sending'):
                db.patch('episode',ep['id'],transfer_status='failed',transfer_error='전송 중 재시작. 재전송으로 이어받을 수 있습니다.')
    def cameras(self):
        if self.cfg['demo']:
            return [dict(camera_id=c,serial='DEMO-'+c,connected=True,resolution=self.resolution(),target=self.resolution(),fps=self.cfg['capture_fps'],usb='3.2',warning='',error='') for c in CAMERAS]
        if self.preview is None:
            # 카메라 열기는 장치가 멈추면 오래 걸리거나 멈출 수 있다 → 화면 응답을 막지 않게 별도 스레드에서 연다
            if not getattr(self,'_preview_thread',None):
                def open_cameras():
                    from . import warm
                    from .warm import WarmCameraService, configure   # 녹화 설정으로 상시 열린 카메라 (미리보기 · 캘리브레이션 · 녹화 공용)
                    configure(self.cfg['capture_width'],self.cfg['capture_height'],self.cfg['capture_fps'])
                    warm.SDK_QUEUE=int(self.cfg.get('sdk_queue',32))   # 카메라 SDK 안의 프레임 버퍼 (0 = 예전 방식)
                    warm.WHITE_BALANCE=self.cfg.get('white_balance','lock')   # "lock" · 숫자(K) · "auto"
                    warm.AE_PRIORITY=bool(self.cfg.get('auto_exposure_priority',False))   # False = 노출 우선 끔 (fps 고정)
                    self.preview=WarmCameraService.get()
                self._preview_thread=threading.Thread(target=open_cameras,daemon=True,name='warm-open'); self._preview_thread.start()
                self._preview_thread.join(15)   # 정상이면 몇 초 안에 끝난다
            if self.preview is None: return []
        return self.preview.list_cameras()
    def serials(self): return [c['serial'] for c in self.cameras()]
    def resolution(self): return f"{self.cfg['capture_width']}x{self.cfg['capture_height']}"
    def calibration_mismatch(self,cal):
        """캘리브레이션을 다른 해상도에서 했으면 그 해상도 (extrinsics.json 의 intrinsics 가 영상과 달라지므로 다시 해야 한다)."""
        sizes={f"{v.get('width')}x{v.get('height')}" for v in (cal.get('intrinsics') or {}).values() if isinstance(v,dict) and v.get('width')}
        if not sizes and cal.get('resolution'): sizes={cal['resolution']}
        if not sizes and not (cal.get('demo') or self.cfg['demo']): sizes={'640x480'}   # 해상도 기록이 없는 실제 카메라 결과 = 640×480 시절
        other=sizes-{self.resolution()}
        return ', '.join(sorted(other)) if other else None
    def ready(self):
        cams=self.cameras()
        if len(cams)!=4 or not all(c.get('connected') for c in cams): raise ValueError('카메라 4대가 모두 연결되어야 합니다')
        return cams
    def _demo_frame(self,cam):
        image=np.full((self.cfg['capture_height'],self.cfg['capture_width'],3),(26,35,48),np.uint8)
        cv2.putText(image,'DEMO  /  '+cam,(40,220),cv2.FONT_HERSHEY_SIMPLEX,1,(130,230,200),2)
        cv2.putText(image,time.strftime('%H:%M:%S')+'.%02d'%int(time.time()*100%100),(40,270),cv2.FONT_HERSHEY_SIMPLEX,0.8,(130,230,200),2)
        return cv2.imencode('.jpg',image)[1].tobytes()
    def frame(self,cam):
        if self.cfg['demo']: return self._demo_frame(cam)
        self.cameras(); obj=self.preview._cameras.get(cam)  # 녹화 중에도 같은 스트림의 미리보기
        return obj.get_frame_jpeg() if obj else None
    def preview_frames(self):
        """미리보기 스트림용: 카메라 4대의 최신 JPEG {cam: bytes|None}. 새 프레임이 없으면 지난번과 같은 bytes 객체를 돌려준다."""
        if self.cfg['demo']:
            tick=int(time.time()*15)   # 데모는 화면 15fps 에 맞춰 새 프레임
            if getattr(self,'_demo_tick',None)!=tick:
                self._demo_tick=tick; self._demo_frames={c:self._demo_frame(c) for c in CAMERAS}
            return dict(self._demo_frames)
        if self.preview is None: self.cameras()   # 처음 한 번 카메라 열기
        if self.preview is None: return {}
        cams=self.preview._cameras
        return {c:(cams[c].get_frame_jpeg() if c in cams else None) for c in CAMERAS}
    def snapshot(self):
        with self.lock:
            if self.busy: raise ValueError('현재 작업 완료 후 캡처하세요')
            cams=self.ready(); serials=[c['serial'] for c in cams]
            if self.calib_serials and self.calib_serials!=serials: raise ValueError('카메라 구성이 바뀌었습니다. 보드 캡처를 초기화하세요')
            self.calib_serials=serials
            frames=[self.frame(c) for c in CAMERAS]
            if not all(frames): raise ValueError('카메라 프레임이 아직 준비되지 않았습니다')
            if len(self.calib_sets)>=40: raise ValueError('최대 40세트. 초기화 후 다시 촬영하세요')
            self.calib_sets.append(frames)
            return len(self.calib_sets)
    def calibrate(self,params):
        with self.lock:
            if self.busy: raise ValueError('현재 작업 완료 후 실행하세요')
            self.ready()
            if len(self.calib_sets)<5: raise ValueError('보드 캡처 5세트 이상 필요')
            self.busy='calibrating'
        try:
            if self.cfg['demo']:
                result=dict(ok=True,method='charuco',cameras=CAMERAS,transforms={c:dict(R=np.eye(3).tolist(),T=[[0],[0],[0]]) for c in CAMERAS},reprojectionErrors={c:0.2 for c in CAMERAS[1:]},demo=True)
            else:
                intr=self.preview.get_factory_intrinsics_list(CAMERAS)
                result=compute_extrinsics_charuco([[s[i] for s in self.calib_sets] for i in range(4)],camera_names=CAMERAS,factory_intrinsics=intr,**params)
            result=clean(dict(result,version_id=uid('cal_'),serials=self.serials(),saved_at=now(),parameters=params,demo=self.cfg['demo'],resolution=self.resolution()))
            self.db.put('calibration','current',result)
            save_json(self.root/'calibrations'/(result['version_id']+'.json'),result)
            self.db.log('calibration','ChArUco 계산 완료. 재투영 오차 확인 필요')
            return result
        finally: self.busy=None
    def confirm_calibration(self):
        result=self.db.get('calibration','current')
        if not result or result.get('serials')!=self.serials(): raise ValueError('현재 카메라 구성의 캘리브레이션이 없습니다')
        if self.calibration_mismatch(result): raise ValueError(f"이 캘리브레이션은 {self.calibration_mismatch(result)} 에서 계산했습니다. 지금 해상도({self.resolution()})로 보드를 다시 캡처해 계산하세요")
        self.db.patch('calibration','current',confirmed=True,confirmed_at=now())
        self.db.log('calibration','작업자가 기존 품질 기준을 확인하고 촬영 승인')
    def new_episode_id(self):
        """PhysicalAI 와 같은 에피소드 ID: 촬영 시작 시각(UTC) 'ep_YYYYMMDD_HHMMSS'. NAS 폴더 이름도 이 ID 다.
        같은 초에 이미 있으면 _2, _3 … 을 붙인다."""
        from datetime import datetime, timezone
        base=datetime.now(timezone.utc).strftime('ep_%Y%m%d_%H%M%S'); key=base; n=2
        while self.db.get('episode',key) or (self.root/'episodes'/key).exists():
            key=f'{base}_{n}'; n+=1
        return key
    def start(self,task,seconds=5):
        try: seconds=int(seconds)
        except (TypeError,ValueError): seconds=0
        if seconds not in self.DURATIONS: raise ValueError('촬영 길이는 '+' · '.join(f'{s}초' for s in self.DURATIONS)+' 중에서 고르세요')
        with self.lock:
            if self.requires_restart: raise ValueError('카메라 저장 스레드가 종료되지 않았습니다. 프로그램을 재시작하세요')
            if self.busy: raise ValueError('촬영 또는 QC가 실행 중입니다')
            self.ready()
            cal=self.db.get('calibration','current')
            if not cal or not cal.get('confirmed') or cal.get('serials')!=self.serials(): raise ValueError('캘리브레이션 결과를 확인·승인하세요')
            if self.calibration_mismatch(cal): raise ValueError(f"촬영 해상도가 {self.resolution()} 로 바뀌었습니다. 캘리브레이션을 다시 하세요 (지금 결과는 {self.calibration_mismatch(cal)} 기준)")
            limit=int(self.cfg.get('max_pending',10)); waiting=len(self.pending_episodes())
            if limit and waiting>=limit: raise ValueError(f'아직 보내지 않은 촬영이 {waiting}개입니다 (최대 {limit}). QC 일괄 실행 후 검수가 필요한 것을 처리하면 계속 촬영할 수 있습니다')
            d=self.disk()
            if d['low']: raise ValueError(f"저장 공간이 부족합니다 (남은 공간 {d['free']/1024**3:.1f}GB, 최소 {d['min_free']/1024**3:.0f}GB). 전송을 끝내 원본이 지워지거나, 보내지 않을 에피소드를 삭제한 뒤 촬영하세요")
            key=self.new_episode_id(); out=self.root/'episodes'/key; out.mkdir(parents=True)
            ep=dict(task_id=task['id'],task_name=task['name'],scene=task.get('scene','3D'),object=task_object(task),station=self.cfg['station'],demo=self.cfg['demo'],created_at=now(),status='preparing',duration=seconds,calibration=cal,transfer_status='not_sent')
            self.db.put('episode',key,ep); save_json(out/'extrinsics.json',public_extrinsics(cal))  # PhysicalAI 와 같은 키만
            self.busy='recording'; self.live=dict(episode=key,seconds=seconds,fps=int(self.cfg['capture_fps']),phase='preparing',started=None,until=None,
                                                  countdown=self.COUNTDOWN,go=None)
            self.executor.submit(self._record,key)
            return self.db.get('episode',key)
    def live_status(self):
        """화면의 녹화 타이머용 (가볍게 · 0.2초마다 불린다). 시각은 이 서버의 epoch 초 — 화면은 server_now 로 시계 차이를 맞춘다."""
        lv=dict(self.live or {})
        if not lv: return dict(active=False,server_now=time.time())
        s=self.session
        lv['frames']=s.get_live_status() if s and lv.get('phase')=='recording' else lv.get('frames')
        return dict(lv,active=True,server_now=time.time())
    def _record(self,key):
        out=self.root/'episodes'/key; seconds=int(self.db.get('episode',key).get('duration') or 5)
        try:
            self.db.log(key,f'{seconds}초 촬영 준비')
            countdown=self.COUNTDOWN
            if self.cfg['demo']:
                if countdown and self.cfg.get('demo_realtime'):
                    self.live.update(phase='countdown',go=time.time()+countdown); time.sleep(countdown)
                t0=time.time(); self.live.update(phase='recording',started=t0,until=t0+seconds)
                self.db.patch('episode',key,status='recording',started_at=epoch_iso(t0))
                if self.cfg.get('demo_realtime'): time.sleep(seconds)   # 데모에서 화면 타이머를 실제 시간으로 확인할 때
                fps=int(self.cfg['capture_fps'])
                self.live.update(phase='saving',frames={c:seconds*fps for c in CAMERAS})
                self._demo_capture(out,seconds*fps,fps)
            else:
                from .warm import WarmSession
                cal=self.db.get('episode',key)['calibration']
                if self.serials()!=cal['serials']: raise ValueError('장치 구성 변경: 재캘리브레이션 필요')
                # 카메라는 이미 녹화 설정으로 열려 있다 → 닫았다 다시 열지 않고 지금 시각부터 4대가 함께 기록
                self.notify_recording(True)     # 녹화 우선: 그동안 NAS 저장은 디스크를 쓰지 않고 기다린다
                self.session=WarmSession(self.preview)
                # 카운트다운 동안은 실제 녹화와 같은 처리(정렬 · mp4 · npy 쓰기)를 프리롤 폴더에 하고 버린다 (녹화 시작 직후 프레임 누락 방지)
                pre_root=self.root/'preroll'
                for old in (pre_root.glob('*') if pre_root.exists() else []):
                    if old.name!=key: shutil.rmtree(old,ignore_errors=True)   # 지난 녹화가 비정상 종료로 남긴 것
                names=self.session.start(out,seconds=seconds,preroll=countdown,preroll_dir=pre_root/key)
                if names!=CAMERAS: raise ValueError('4대 구성이 아닙니다')
                if countdown:
                    self.live.update(phase='countdown',go=self.session.rec_at)   # 화면에 큰 3 · 2 · 1 (에피소드 상태는 그대로 '촬영 준비')
                # 버튼을 누른 시각이 아니라 4대 모두 첫 프레임이 실제로 기록된 시각부터 센다 → 카메라마다 정확히 seconds × FPS 장
                # (프레임 번호는 4대 공통 시간 격자의 슬롯 번호 — warm._Grid)
                first=self.session.wait_first(timeout=countdown+3.0)
                until=self.session.schedule_stop(seconds)
                self.live.update(phase='recording',started=first,until=until)
                self.db.patch('episode',key,status='recording',started_at=epoch_iso(first))
                while time.time()<until+0.1: time.sleep(0.02)   # 끝 시각 뒤 첫 프레임이 와야 카메라가 닫힌다 (30fps → 33ms)
                self.live.update(phase='saving',frames=self.session.get_live_status())
                self.db.patch('episode',key,status='saving')
                recorders=list(self.session._recorders)
                results=self.session.stop(); self.session=None
                if any(getattr(r,'_writer_thread',None) and r._writer_thread.is_alive() for r in recorders):
                    self.requires_restart=True
                    raise ValueError('파일 저장 시간 초과. 다음 촬영 전 프로그램을 재시작하세요')
                if len(results)!=4 or any(r.get('error') or r.get('frame_count',0)==0 for r in results.values()):
                    raise ValueError('카메라 저장 실패: '+str(results))
                counts={c:r['frame_count'] for c,r in results.items()}
                lost={c:int(r.get('missing',0))+int(r.get('dropped',0)) for c,r in results.items()}
                # 받지 못한 프레임의 슬롯은 가까운 프레임으로 채워 번호(=시각)와 장수를 4대 똑같이 맞춘다 → 채운 수를 기록한다
                filled={c:int(r.get('filled',0)) for c,r in results.items()}
                self.db.patch('episode',key,frame_counts=counts,frames_lost=lost,frames_filled=filled)
                if countdown:
                    pre={c:int(r.get('preroll_missing',0)) for c,r in results.items()}
                    self.db.patch('episode',key,preroll_seconds=countdown,preroll_lost=pre)
                    self.db.log(key,f'카운트다운 {countdown}초 (프리롤 · 저장 안 함) 중 받지 못한 프레임: '+' · '.join(f'{c} {n}' for c,n in pre.items()))
                # 카메라가 만들었는데 받지 못한 프레임 (하드웨어 시각 간격으로 계산). 정상이면 0
                note=lambda c:' · '.join(x for x in (f'누락 {lost[c]}' if lost[c] else '',f'채움 {filled[c]}' if filled[c] else '') if x)
                summary=' · '.join(f'{c} {counts[c]}장'+(f' ({note(c)})' if note(c) else '') for c in counts)
                expect=seconds*int(self.cfg['capture_fps'])
                worst=max((max(lost[c],filled[c])/max(1,expect) for c in counts),default=0)
                uneven=any(n!=expect for n in counts.values())   # 장수가 기준과 다르면 4대 번호가 끝까지 맞지 않는다
                self.db.log(key,f'저장 프레임 (기준 {seconds}초 × {self.cfg["capture_fps"]}fps = {expect}장): '+summary,'error' if worst>0.05 or uneven else 'info')
            self.db.patch('episode',key,status='captured',ended_at=now())
            # PhysicalAI 원본 폴더의 표식 rgbd_episode.json (Task · 촬영 시각 · 프레임 수 · 캘리브레이션 · 에피소드 UID)
            mark=episode_mark(self.db.get('episode',key))
            self.db.patch('episode',key,episode_uid=mark['episodeUid'],nas_folder=mark['episodeId'])
            save_json(out/EPISODE_MARK,mark)
            self.db.patch('episode',key,bytes=self._folder_bytes(out))
            self._save_episode(key)
            self.db.log(key,'원본 저장 완료. 촬영 QC 대기')
        except Exception as e:
            self.db.patch('episode',key,status='failed',error=str(e)); self.db.log(key,str(e),'error')
        finally:
            if self.session:
                try: self.session.stop()
                except Exception: pass
                self.session=None
            self.busy=None; self.live=None
            if not self.cfg['demo']: self.notify_recording(False)
    def _demo_capture(self,out,n=150,fps=30):
        # Synthetic images are explicitly tagged and must never enter a production export server.
        W,H=self.cfg['capture_width'],self.cfg['capture_height']
        for cam in CAMERAS:
            (out/(cam+'_depth')).mkdir()
            w=cv2.VideoWriter(str(out/(cam+'.mp4')),cv2.VideoWriter_fourcc(*'mp4v'),fps,(W,H))
            rows=['frame_idx,hw_timestamp_ms,host_time_s']
            for i in range(n):
                im=np.full((H,W,3),55,np.uint8); cv2.putText(im,'DEMO '+cam+' '+str(i),(50,240),0,1,(90,220,180),2); w.write(im)
                np.save(out/(cam+'_depth')/('frame_%06d.npy'%i),np.full((H,W),650,np.uint16))
                rows.append(f'{i},{i*1000/fps:.3f},{1700000000+i/fps:.6f}')
            w.release(); (out/(cam+'_timestamps.csv')).write_text('\n'.join(rows)+'\n')
            save_json(out/(cam+'_intrinsics.json'),dict(serial='DEMO-'+cam,depth_scale=.001,color_intrinsics=dict(width=W,height=H,fx=600,fy=600,ppx=W/2,ppy=H/2),aligned_to='color',color_fps=fps,depth_fps=fps))
        self.db.patch('episode',out.name,frame_counts={c:n for c in CAMERAS})
    # ── QC ──
    # 같은 PC export 서버가 있으면 QC 를 거기 대기열에 맡긴다 (촬영 서버는 촬영만 · 녹화하면 QC 가 잠깐 멈춤).
    # QC 전체 PASS 는 자동 승인 · NAS 전송, WARN · FAIL 이 하나라도 있으면 작업자 검수를 기다린다.
    PENDING=('captured','qc_queued','qc_running','qc_done')
    def active_episodes(self):
        """이 PC 에 원본이 남아 있는 에피소드 (NAS 저장이 끝나 지운 것 · 삭제한 것 제외, 캘리브레이션 사본은 빼고 읽는다).
        대기 수 · QC 결과 반영 · 원본 정리 · 저장 공간 계산은 모두 이것만 보면 된다 — 기록 전체(수천 건)를 2초마다 읽어 풀지 않는다."""
        return self.db.all('episode',drop=('calibration',),active=True)
    def pending_episodes(self,eps=None):
        """아직 NAS 로 보내지 않은 촬영 (QC 전 · QC 중 · 검수 대기). max_pending 개가 쌓이면 촬영을 막는다.
        eps: 이미 읽어 둔 에피소드 목록 (상태 화면이 한 번 읽어 같이 쓴다)."""
        return [e for e in (self.active_episodes() if eps is None else eps) if e.get('status') in self.PENDING and not e.get('export_job') and not e.get('local_deleted')]
    def qc_remote(self): return self.export_is_local()
    def qc(self,key):
        with self.lock:
            ep=self.db.get('episode',key)
            if not ep or ep['status'] not in ('captured','qc_done'): raise ValueError('저장이 완료된 촬영을 선택하세요')
            if key in self.transferring or ep.get('export_job'): raise ValueError('진행 중이거나 이미 전송된 에피소드입니다')
            if self.qc_remote(): return self._queue_qc(key,ep)
            if self.busy: raise ValueError('촬영 또는 QC가 실행 중입니다')
            self.busy='qc'; self.db.patch('episode',key,status='qc_running',review=None)
            self.executor.submit(self._qc,key)
    def qc_batch(self):
        """QC 를 안 한 촬영을 한 번에 대기열에 넣는다."""
        with self.lock:
            keys=[e['id'] for e in sorted(self.pending_episodes(),key=lambda e:e.get('created_at','')) if e['status']=='captured']
            if not keys: raise ValueError('QC 할 촬영이 없습니다')
            if self.qc_remote():
                for k in keys: self._queue_qc(k,self.db.get('episode',k))
            else:
                for k in keys: self.db.patch('episode',k,status='qc_queued',review=None)
                self.executor.submit(self._qc_batch_inline,keys)
            self.db.log('qc','QC 일괄 실행: '+str(len(keys))+'개')
            return len(keys)
    def _queue_qc(self,key,ep):
        self._export('POST','/api/qc/'+key+'/local',timeout=10,json=dict(path=str((self.root/'episodes'/key).resolve()),scene=ep.get('scene','3D'),station=self.cfg['station']))
        self.db.patch('episode',key,status='qc_queued',review=None,error=None)
    def reconcile_qc(self,qc_list,eps=None):
        """export 서버의 QC 결과를 받아 반영한다 (상태 갱신 · 하트비트에서 부른다). 에피소드를 바꿨으면 True.
        eps: 이미 읽어 둔 에피소드 목록 (바꿀 때는 DB 에서 다시 읽어 확인한다)."""
        by_id={q.get('id'):q for q in qc_list or []}; changed=False
        for ep in (self.active_episodes() if eps is None else eps):
            if ep.get('status') not in ('qc_queued','qc_running') or ep.get('local_deleted'): continue
            q=by_id.get(ep['id'])
            if not q:
                # export 서버에 기록이 없다 (export 서버 설정을 새로 만든 경우 등) → 다시 맡긴다
                if self.qc_remote():
                    try: self._queue_qc(ep['id'],ep); changed=True
                    except Exception: pass
                continue
            with self.lock:
                cur=self.db.get('episode',ep['id'])
                if not cur or cur.get('status') not in ('qc_queued','qc_running'): continue
                if q['status']=='running' and cur['status']!='qc_running': self.db.patch('episode',ep['id'],status='qc_running'); changed=True
                elif q['status']=='queued' and cur['status']!='qc_queued': self.db.patch('episode',ep['id'],status='qc_queued'); changed=True
                elif q['status']=='done': self._finish_qc(ep['id'],q.get('result') or {},q.get('seconds')); changed=True
                elif q['status']=='failed':
                    self.db.patch('episode',ep['id'],status='captured',error='QC 실패: '+str(q.get('error',''))); changed=True
        return changed
    def _finish_qc(self,key,result,seconds):
        result=clean(result); save_json(self.root/'episodes'/key/'qc.json',result)
        self.db.patch('episode',key,status='qc_done',qc=result,qc_seconds=seconds,error=None)
        self._save_episode(key); self.db.log(key,'QC 완료: '+str(result.get('overall')))
        if result.get('overall')=='PASS' and self.cfg.get('auto_export',True):
            self.db.patch('episode',key,review=dict(approved=True,note='자동 승인 (QC 전체 PASS)',auto=True,at=now()))
            self._save_episode(key); self.db.log(key,'QC 전체 PASS — 자동 승인 · NAS 전송')
            try: self.send(key)
            except Exception as e: self.db.log(key,'자동 전송 실패: '+str(e),'error')
        elif result.get('overall')!='PASS':
            self.db.log(key,'QC '+str(result.get('overall'))+' — 검수가 필요합니다 (전송하지 않음)')
    def _qc_result(self,key):
        ep=self.db.get('episode',key)
        if self.cfg['demo']:
            return dict(ok=True,overall='PASS',checks=[dict(name='demo',label='데모 데이터',status='PASS',detail='UI·전송 검증용 모의 결과. 실제 QC 알고리즘 실행 결과가 아닙니다.')],demo=True)
        return run_capture_qc(str(self.root/'episodes'/key),**PRESETS[ep['scene']])
    def _qc(self,key):
        """export 서버가 다른 PC 일 때: 이 서버에서 QC (예전 방식)."""
        try:
            start=time.perf_counter(); result=self._qc_result(key)
            if not result.get('ok'): raise ValueError(result.get('error','QC 실패'))
            with self.lock: self._finish_qc(key,result,round(time.perf_counter()-start,2))
        except Exception as e:
            self.db.patch('episode',key,status='captured',error=str(e));self.db.log(key,str(e),'error')
        finally: self.busy=None
    def _qc_batch_inline(self,keys):
        for k in keys:
            while self.busy: time.sleep(0.5)       # 촬영 중이면 끝날 때까지 기다린다
            ep=self.db.get('episode',k)
            if not ep or ep.get('status')!='qc_queued': continue
            self.busy='qc'; self.db.patch('episode',k,status='qc_running'); self._qc(k)
    def approve(self,key,note):
        with self.lock:
            ep=self.db.get('episode',key)
            if not ep or ep['status']!='qc_done': raise ValueError('QC 완료 후 승인하세요')
            if key in self.transferring or ep.get('export_job'): raise ValueError('전송을 시작한 원본은 수정할 수 없습니다')
            if ep['qc']['overall']!='PASS' and not note.strip(): raise ValueError('WARN/FAIL은 확인 사유를 입력하세요')
            self.db.patch('episode',key,review=dict(approved=True,note=note,at=now()))
            self._save_episode(key);self.db.log(key,'검수 승인: '+note)
    def _save_episode(self,key): save_json(self.root/'episodes'/key/'episode.json',self.db.get('episode',key))
    # ── export 서버와의 연결 ──
    def export_is_local(self):
        from urllib.parse import urlsplit
        return bool(self.cfg.get('local_fastpath',True)) and urlsplit(self.cfg['export_url']).hostname in ('127.0.0.1','localhost','::1')
    def _export(self,method,url,timeout=4,**kw):
        with httpx.Client(base_url=self.cfg['export_url'],headers={'Authorization':'Bearer '+self.cfg['transfer_token']},timeout=timeout,trust_env=False) as c:
            r=c.request(method,url,**kw); r.raise_for_status(); return r.json()
    def notify_recording(self,active):
        """녹화 우선 — export 서버에 녹화 시작 · 끝을 알린다 (그동안 NAS 복사 · 체크섬 읽기를 멈춘다). 실패해도 촬영은 계속."""
        try: self._export('POST','/api/machine/recording',timeout=2,json=dict(active=bool(active)))
        except Exception: pass
    def wait_not_recording(self):
        while self.busy=='recording': time.sleep(0.2)
    # ── 저장 공간 ──
    def disk(self,eps=None):
        u=shutil.disk_usage(self.root)
        pending=[e for e in (self.active_episodes() if eps is None else eps) if not e.get('local_deleted') and e.get('status') not in ('deleted',)]
        sizes=[e['bytes'] for e in pending if e.get('bytes')]
        # 최근 에피소드 평균, 없으면 640×480 에서 약 400MB 를 해상도 비율로 (848×480 이면 약 530MB)
        per=int(sum(sizes[:10])/len(sizes[:10])) if sizes else int(400*1024**2*self.cfg['capture_width']*self.cfg['capture_height']/(640*480))
        reserve=float(self.cfg.get('min_free_gb',10))*1024**3
        return dict(free=u.free,total=u.total,per_episode=per,min_free=int(reserve),pending=len(pending),pending_bytes=sum(sizes),
                    remaining_episodes=max(0,int((u.free-reserve)//per)),low=u.free<reserve)
    def _folder_bytes(self,p):
        return sum(f.stat().st_size for f in Path(p).rglob('*') if f.is_file())
    # ── NAS 저장이 끝난 원본 지우기 (체크섬 검증 완료만) ──
    def reconcile(self,jobs,eps=None):
        """에피소드를 바꿨으면 True. eps: 이미 읽어 둔 에피소드 목록."""
        by_id={j['id']:j for j in jobs}; changed=False
        for ep in (self.active_episodes() if eps is None else eps):
            j=by_id.get(ep.get('export_job') or '')
            if not j or ep.get('local_deleted') or ep['id'] in self.transferring: continue
            if j.get('status')=='done' and ep.get('transfer_status')!='done':
                self.db.patch('episode',ep['id'],transfer_status='done',nas_output=j.get('output')); changed=True
                self.db.log(ep['id'],'NAS 저장 · 체크섬 검증 완료: '+str(j.get('output','')))
            # 원본 형식(PhysicalAI rgbd_raw 그대로)으로 저장된 것만 지운다. 옛 PNG 변환 · 작업 폴더 형식 저장은 원본을 남긴다 (files 기록이 없는 작업)
            if j.get('status')=='done' and self.cfg.get('delete_after_nas',True) and j.get('files'):
                shutil.rmtree(self.root/'episodes'/ep['id'],ignore_errors=True)
                (self.root/'bundles'/(ep['id']+'.zip')).unlink(missing_ok=True)
                if not (self.root/'episodes'/ep['id']).exists():
                    self.db.patch('episode',ep['id'],local_deleted=True,local_deleted_at=now()); changed=True
                    self.db.log(ep['id'],'촬영 PC 원본 삭제 (NAS 저장 확인됨)')
        return changed
    def delete_episode(self,key):
        """보내지 않을 에피소드 삭제 (QC FAIL 재촬영 등). 전송을 시작한 원본은 지울 수 없다."""
        with self.lock:
            ep=self.db.get('episode',key)
            if not ep: raise ValueError('에피소드 없음')
            if ep.get('status') in ('preparing','recording','saving','qc_queued','qc_running'): raise ValueError('촬영 · QC 중인 에피소드입니다')
            if key in self.transferring or ep.get('transfer_status') in ('packing','sending','sent','done') or ep.get('export_job'):
                raise ValueError('전송을 시작한 원본은 지울 수 없습니다 (NAS 저장이 끝나면 자동으로 지워집니다)')
            shutil.rmtree(self.root/'episodes'/key,ignore_errors=True)
            (self.root/'bundles'/(key+'.zip')).unlink(missing_ok=True)
            self.db.patch('episode',key,status='deleted',local_deleted=True,local_deleted_at=now())
            self.db.log(key,'에피소드 삭제 (전송하지 않음)')
    def send(self,key):
        with self.lock:
            ep=self.db.get('episode',key)
            if not ep or not (ep.get('review') or {}).get('approved'): raise ValueError('QC 확인·승인 후 전송하세요')
            if ep.get('local_deleted'): raise ValueError('이 PC 에서 삭제된 원본입니다')
            if key in self.transferring: raise ValueError('전송 중입니다')
            if ep.get('transfer_status') in ('sent','done'): return
            self.transferring.add(key); self.db.patch('episode',key,transfer_status='packing',transfer_error=None)
            self.transfer.submit(self._send,key)
    def _send(self,key):
        try:
            ep=self.db.get('episode',key); job_id=ep.get('export_job') or uid('job_')
            self.db.patch('episode',key,export_job=job_id)
            if self.export_is_local():
                # 같은 PC 지름길: 원본 폴더 경로만 넘긴다. export 서버가 바로 검증해 NAS 로 복사한다
                t=time.perf_counter()
                self._export('POST','/api/transfer/'+job_id+'/local',timeout=60,json=dict(task_id=ep['task_id'],episode_id=key,station=self.cfg['station'],
                             path=str((self.root/'episodes'/key).resolve())))
                self.db.patch('episode',key,transfer_status='sent',transfer_seconds=round(time.perf_counter()-t,2),transfer_progress=100,transfer_mode='local')
                self.db.log(key,'export 서버에 원본 폴더 전달 (같은 PC). NAS 저장 · 확인 뒤 이 PC 원본은 지워집니다.')
                return
            self.wait_not_recording()          # 녹화 우선: 녹화 중에는 원본을 읽지 않는다
            bundle=self.root/'bundles'/(key+'.zip'); t=time.perf_counter()
            if bundle.exists(): info=dict(size=bundle.stat().st_size,sha256=digest(bundle))
            else: info=make_bundle(self.root/'episodes'/key,bundle)
            self.db.patch('episode',key,packing_seconds=round(time.perf_counter()-t,2),transfer_status='sending')
            headers={'Authorization':'Bearer '+self.cfg['transfer_token']}
            meta=dict(info,task_id=ep['task_id'],episode_id=key,station=self.cfg['station'])
            t=time.perf_counter()
            with httpx.Client(base_url=self.cfg['export_url'],headers=headers,timeout=120,trust_env=False) as client:
                r=client.post('/api/transfer/'+job_id,json=meta);r.raise_for_status(); state=r.json()
                offset=state['offset']
                with open(bundle,'rb') as f:
                    while offset<info['size']:
                        self.wait_not_recording()
                        f.seek(offset); data=f.read(4*1024**2)
                        for attempt in range(3):
                            try:
                                r=client.put('/api/transfer/'+job_id,params={'offset':offset},content=data);r.raise_for_status();break
                            except httpx.HTTPError:
                                if attempt==2: raise
                                time.sleep(attempt+1)
                        new=r.json()['offset']
                        if new==offset: raise ValueError('서버 수신 진행이 멈췄습니다')
                        offset=new
                        self.db.patch('episode',key,transfer_progress=round(offset/info['size']*100,1))
                r=client.post('/api/transfer/'+job_id+'/complete');r.raise_for_status()
            self.db.patch('episode',key,transfer_status='sent',transfer_seconds=round(time.perf_counter()-t,2),transfer_progress=100)
            self.db.log(key,'서버 전달 완료. NAS 저장은 서버에서 별도로 진행됩니다.')
        except Exception as e:
            self.db.patch('episode',key,transfer_status='failed',transfer_error=str(e));self.db.log(key,str(e),'error')
        finally:
            with self.lock: self.transferring.discard(key)
