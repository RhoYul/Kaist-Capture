const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={preparing:'촬영 준비',recording:'촬영 중',saving:'원본 저장 중',captured:'QC 전',qc_queued:'QC 대기열',qc_running:'QC 검사 중',qc_done:'QC 완료',review_needed:'검수 필요',auto_ok:'자동 승인',packing:'원본 묶는 중',sending:'서버 전송 중',sent:'서버 전달 완료',not_sent:'전송 전',receiving:'원본 수신',queued:'처리 대기',extracting:'원본 검증',converting:'PNG 변환',nas_copy:'NAS 저장',verifying:'NAS 검증',done:'NAS 완료',failed:'실패',deleted:'삭제됨',PASS:'PASS',WARN:'WARN',FAIL:'FAIL',SKIP:'SKIP'};
let state=null,info=null,view=location.pathname==='/admin'?'admin':'worker',selected=null,reviewFingerprint='',pending=false,polling=false,previewOn=false,episodeTask='',episodeDate='';
const BLANK='data:image/gif;base64,R0lGODlhAQABAAAAACw=';
// 촬영 길이 (5 · 10초) — 이 브라우저에 기억해 둔다 (없거나 읽을 수 없으면 5초)
let recSeconds=(()=>{try{const v=Number(localStorage.getItem('rec_seconds'));return v===10?10:5}catch(e){return 5}})();
const badge=(s)=>'<span class="badge '+(['done','PASS','sent','auto_ok'].includes(s)?'success':['failed','FAIL'].includes(s)?'fail':['WARN','SKIP','review_needed'].includes(s)?'warn':'')+'">'+esc(names[s]||s||'대기')+'</span>';
const reviewBadge=ep=>ep.status!=='qc_done'?'':ep.review?.approved?(ep.review.auto?' '+badge('auto_ok'):''):' '+badge('review_needed');
function toast(s,error=false){$('toast').textContent=s;$('toast').style.background=error?'#9e4440':'#183e32';$('toast').hidden=false;clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>$('toast').hidden=true,6500)}
async function api(url,method='GET',body){const r=await fetch('/api'+url,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});let d=await r.json().catch(()=>({}));if(!r.ok){if(r.status===401&&url!='/login'){showLogin()}throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail||r.status))}return d}
async function action(fn,message){if(pending)return;pending=true;try{await fn();if(message)toast(message);await refresh()}catch(e){toast(e.message,true)}finally{pending=false}}
function showLogin(){$('login').hidden=false;$('app').hidden=true;syncPreview()}
const VIEWS={worker:['촬영 작업실','4대의 시선, 하나의 에피소드. 준비가 끝나면 촬영을 시작하세요.'],logs:['촬영 로그','촬영부터 NAS 저장까지, 에피소드별로 무슨 일이 있었는지 확인하세요.'],admin:['관리 대시보드','작업의 흐름을 한눈에. 촬영 원본 수신부터 NAS 완료까지 확인하세요.']};
function setView(v){if(!VIEWS[v])v='worker';view=v;document.body.classList.toggle('worker-mode',v==='worker'&&state?.role==='capture');$('worker-view').hidden=v!=='worker';$('logs-view').hidden=v!=='logs';$('admin-view').hidden=v!=='admin';document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===v));$('page-heading').innerHTML=VIEWS[v][0]+'<span class="title-dot">.</span>';$('page-subtitle').textContent=VIEWS[v][1];$('breadcrumb').textContent='작업 공간 / '+VIEWS[v][0];syncPreview();if(v==='logs')fetchWorkerLogs()}
const EPISODE_LIMIT=100;let episodeAll=false;
// job 찾기 —상태를 받을 때마다 id → job 표를 한 번 만든다 (에피소드마다 job 목록 전체를 훑으면 기록이 많을 때 느려진다)
let jobMap=null,jobMapOf=null;
function jobFor(ep){if(jobMapOf!==state.jobs){jobMapOf=state.jobs;jobMap=new Map(state.jobs.map(j=>[j.id,j]))}return jobMap.get(ep.export_job)}
// 내용이 그대로면 DOM 을 건드리지 않는다 (2초마다 표 전체를 다시 만들면 기록이 많을 때 화면이 끊긴다). 바꿨으면 true
function setHTML(id,html){const el=$(id);if(!el||el._html===html)return false;el._html=html;el.innerHTML=html;return true}
function render(){
 $('login').hidden=true;$('app').hidden=false;$('demo-banner').hidden=!state.demo;$('admin-nav').hidden=state.user_role!=='admin';$('logs-nav').hidden=state.role!=='capture';if(view==='logs'&&state.role!=='capture')view='worker';if(view==='admin'&&state.user_role!=='admin')view='worker';if(state.role==='export'&&state.user_role==='admin')view='admin';setView(view);
 $('station').textContent=state.station;$('role-label').textContent=state.role==='capture'?'촬영 PC · 로컬 저장':'export 서버 · NAS 저장';$('mode-badge').textContent=state.demo?'DEMO':state.role.toUpperCase();$('clock').textContent=new Date().toLocaleTimeString('ko-KR');$('connection').hidden=!state.server_error;$('connection').textContent=state.server_error||'';$('live-status').textContent=state.busy?'● '+(state.busy==='calibrating'?'캘리브레이션 계산 중':state.busy==='qc'?'QC 검사 중':'촬영 진행 중'):'● 작업 준비';
 $('capture-content').hidden=state.role!=='capture';
 const current=$('task-select').value,opts=state.tasks.filter(t=>t.active).map(t=>'<option value="'+esc(t.id)+'">'+esc(t.name)+' · '+esc(t.scene)+'</option>').join('');
 if($('task-select').dataset.options!==opts){$('task-select').innerHTML='<option value="">작업을 선택하세요</option>'+opts;$('task-select').dataset.options=opts;$('task-select').value=current}
 {const t=state.tasks.find(t=>t.id===$('task-select').value),box=$('task-object');box.hidden=!t;
  if(t)box.innerHTML=t.prompt?'<span class="eyebrow">녹화 때 rgbd_episode.json 에 기록되는 물체 정보</span><dl><dt>SAM 프롬프트</dt><dd>'+esc(t.prompt)+'</dd><dt>마스크 라벨</dt><dd>'+esc(t.label)+'</dd><dt>장면</dt><dd>'+esc(String(t.scene).toLowerCase())+'</dd><dt>납품 물체 이름</dt><dd>'+esc(t.object_name)+'</dd><dt>상호작용</dt><dd>'+esc(t.interaction)+'</dd></dl><small class="muted">작업 설정 값입니다. 바꾸려면 관리자 화면에서 작업을 새로 만드세요.</small>'
   :'<span class="error">이 작업에는 물체 정보(SAM 프롬프트 등)가 없습니다. rgbd_episode.json 의 object 가 비어 기록됩니다. 관리자 화면에서 물체 정보를 넣어 작업을 새로 만드세요.</span>'}
 {const d=state.disk,box=$('disk-info');if(d){const gb=b=>(b/1024**3).toFixed(1)+'GB';box.className='disk-info'+(d.low?' low':'');box.innerHTML='저장 공간 <b>'+gb(d.free)+'</b> 남음 · 약 <b>'+d.remaining_episodes+'</b>개 더 촬영 가능<br><small>NAS 로 아직 안 간 원본 '+d.pending+'개 ('+gb(d.pending_bytes)+') · 최소 '+gb(d.min_free)+' 남겨 둠. NAS 저장이 확인되면 원본은 자동으로 지워집니다.</small>'+(d.low?'<br><b>저장 공간 부족 — 촬영이 막혔습니다</b>':'')}else box.innerHTML=''}
 $('camera-count').textContent=state.cameras.filter(c=>c.connected).length+' / 4 연결'+(state.resolution?' · '+state.resolution.replace('x','×'):'');
 if(state.resolution&&$('capture-res'))$('capture-res').textContent=state.resolution.replace('x',' × ')+' · 30 FPS 녹화 · 화면 15 FPS';
 if(!$('camera-grid').children.length){$('camera-grid').innerHTML=[1,2,3,4].map(n=>'<div class="cam"><canvas id="preview-'+n+'" width="0" height="0" role="img" aria-label="카메라 '+n+' 미리보기"></canvas><span class="cam-label">CAM '+String(n).padStart(2,'0')+'</span><span class="cam-state" id="cam-state-'+n+'">연결 대기</span></div>').join('')}
 for(let n=1;n<=4;n++){const c=state.cameras.find(c=>c.camera_id==='cam_'+n),usb=c?.usb?' · USB '+c.usb:'';
  $('cam-state-'+n).textContent=state.busy==='recording'?'● REC · '+(c?.fps??'-')+' fps':c?.warning?'⚠ USB '+c.usb+' — USB 3 포트로':c?.connected?'● '+c.serial+' · '+c.fps+' fps · '+(c.resolution||'')+usb:state.busy?'촬영 / 처리 중':'연결 대기'+usb;
  $('cam-state-'+n).classList.toggle('rec',state.busy==='recording');$('cam-state-'+n).classList.toggle('warn',!!c?.warning);
  if(!c?.connected)clearPreview(n)}   // 끊긴 카메라는 마지막 화면을 남기지 않는다
 {const w=state.cameras.filter(c=>c.warning||(!c.connected&&c.error)).map(c=>'<div>⚠ <b>'+esc(c.camera_id)+'</b> ('+esc(c.serial)+'): '+esc(c.warning||c.error)+'</div>');if($('cam-warn')){$('cam-warn').hidden=!w.length;$('cam-warn').innerHTML=w.join('')}}
 const cal=state.calibration,calOld=state.calibration_mismatch;$('calib-status').textContent=calOld?'⚠ 촬영 해상도가 '+(state.resolution||'')+' 로 바뀌었습니다 — 캘리브레이션을 다시 하세요 (지금 결과는 '+calOld+' 기준)':cal?.confirmed?'✓ 캘리브레이션 확인 완료':'○ 캘리브레이션 결과 확인 필요';$('calib-status').classList.toggle('error',!!calOld);const pend=state.pending,full=!!pend&&pend.limit>0&&pend.total>=pend.limit;$('record').disabled=!!state.busy||!cal?.confirmed||!!calOld||full||state.cameras.filter(c=>c.connected).length!==4||!$('task-select').value;if(state.durations?.length&&!state.durations.includes(recSeconds))recSeconds=state.durations[0];document.querySelectorAll('[data-sec]').forEach(b=>{b.hidden=!!state.durations?.length&&!state.durations.includes(Number(b.dataset.sec));b.classList.toggle('active',Number(b.dataset.sec)===recSeconds);b.disabled=!!state.busy});
 $('record').textContent=state.busy==='recording'?(live?.phase==='recording'?'● 촬영 중…':live?.phase==='saving'?'원본 저장 중…':live?.phase==='countdown'?'카운트다운…':'촬영 준비 중…'):full?'대기 '+pend.total+'/'+pend.limit+' — QC · 검수 먼저':'● '+recSeconds+'초 촬영 시작';$('record').classList.toggle('recording',state.busy==='recording');if(state.live?.active)startLive();
 if(pend&&$('pending-info')){$('pending-info').innerHTML='보내지 않은 촬영 <b>'+pend.total+'</b> / '+pend.limit+' · QC 전 '+pend.need_qc+' · QC 중 '+pend.in_qc+' · 검수 필요 '+pend.review+(pend.auto_export?'<br><small class="muted">QC 전체 PASS 는 자동으로 NAS 전송 · WARN/FAIL 은 검수 후 전송'+(pend.qc_on_export?' · QC 는 export 서버에서 (녹화 중엔 잠깐 멈춤)':'')+'</small>':'');$('pending-info').classList.toggle('error',full);$('qc-batch').disabled=!pend.need_qc||state.busy==='qc';$('qc-batch').textContent='QC 일괄 실행'+(pend.need_qc?' ('+pend.need_qc+')':'')}$('snapshot').disabled=!!state.busy;$('compute').disabled=!!state.busy||state.captures<5;$('capture-count').textContent=state.captures+' / 5';$('calib-summary').textContent=cal?.confirmed?'확인 완료 · 카메라 이동 시 다시 보정':'기존 ChArUco 방식';
 $('calib-result').innerHTML=cal?'<div class="calib-values">'+Object.entries(cal.reprojectionErrors||{}).map(([c,e])=>'<span class="badge '+(e>.7?'warn':'success')+'">'+esc(c)+' · '+esc(e)+' px</span>').join('')+'<button id="confirm-calib" '+(state.busy?'disabled':'')+'>'+ (cal.confirmed?'확인 완료 ✓':'수치 확인 · 촬영 준비 완료')+'</button></div>':'';
 if($('confirm-calib'))$('confirm-calib').onclick=()=>action(()=>api('/calibration/confirm','POST'),'캘리브레이션을 확인했습니다.');
 // 촬영 기록 — 작업(Task)별로 묶어 보고, 칩으로 한 작업만 골라 볼 수 있다
 const byTask=new Map();const liveEps=state.episodes.filter(e=>e.status!=='deleted');for(const ep of liveEps){if(!byTask.has(ep.task_id))byTask.set(ep.task_id,[]);byTask.get(ep.task_id).push(ep)}
 if(episodeTask&&!byTask.has(episodeTask))episodeTask='';
 const taskName=id=>state.tasks.find(t=>t.id===id)?.name||byTask.get(id)?.[0]?.task_name||id,doneOf=eps=>eps.filter(e=>jobFor(e)?.status==='done').length;
 if(setHTML('episode-filter',byTask.size?'<span class="muted">작업</span><button type="button" class="task-chip'+(episodeTask?'':' active')+'" data-task="">전체 '+liveEps.length+'</button>'+[...byTask].map(([id,eps])=>'<button type="button" class="task-chip'+(episodeTask===id?' active':'')+'" data-task="'+esc(id)+'">'+esc(taskName(id))+' '+eps.length+' <span class="muted">· NAS '+doneOf(eps)+'</span></button>').join(''):''))
  document.querySelectorAll('[data-task]').forEach(b=>b.onclick=()=>{episodeTask=b.dataset.task;render()});
 // 날짜(이 PC 기준 현지 날짜)별 구분 — 작업 선택과 함께 날짜 칩으로 하루만 골라 볼 수도 있다
 const taskEps=(episodeTask?liveEps.filter(e=>e.task_id===episodeTask):liveEps).slice().sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at))),groupBy=(eps,f)=>{const m=new Map();for(const e of eps){const k=f(e);if(!m.has(k))m.set(k,[]);m.get(k).push(e)}return m};
 const byDate=groupBy(taskEps,epDay);if(episodeDate&&!byDate.has(episodeDate))episodeDate='';
 if(setHTML('episode-dates',byDate.size>0?'<span class="muted">날짜</span><button type="button" class="task-chip'+(episodeDate?'':' active')+'" data-day="">전체 '+taskEps.length+'</button>'+[...byDate].map(([d,eps])=>'<button type="button" class="task-chip'+(episodeDate===d?' active':'')+'" data-day="'+esc(d)+'">'+esc(dayLabel(d))+' '+eps.length+'</button>').join(''):''))
  document.querySelectorAll('[data-day]').forEach(b=>b.onclick=()=>{episodeDate=b.dataset.day;render()});
 const shown=episodeDate?taskEps.filter(e=>epDay(e)===episodeDate):taskEps;
 $('episode-count').textContent=shown.length+' episodes';
 const dayRows=eps=>[...groupBy(eps,epDay)].map(([d,list])=>'<tr class="date-group"><td colspan="5">📅 '+esc(dayLabel(d))+' <span class="muted">· '+list.length+'건 · NAS 완료 '+doneOf(list)+'</span></td></tr>'+list.map(row).join('')).join('');
 const row=ep=>{const j=jobFor(ep);return '<tr><td><b>'+esc(ep.task_name)+'</b><small><code>'+esc(ep.id)+'</code> · '+esc(fmtDate(ep.created_at))+(ep.local_deleted?' · 이 PC 원본 삭제됨':'')+'</small></td><td>'+badge(ep.status)+(ep.qc?' '+badge(ep.qc.overall):'')+reviewBadge(ep)+'</td><td>'+badge(ep.transfer_status)+(ep.transfer_status==='sending'?'<small>'+ep.transfer_progress+'%</small>':'')+'</td><td>'+badge(j?.status||'대기')+'</td><td><button data-review="'+esc(ep.id)+'">검수 · 상세 →</button></td></tr>'};
 // 기록이 많으면 표 그리기만 수백 ms → 기본은 최근 EPISODE_LIMIT 건만 (작업 · 날짜 칩으로 좁히거나 「전체 보기」)
 const vis=episodeAll?shown:shown.slice(0,EPISODE_LIMIT),cut=shown.length>EPISODE_LIMIT;
 const more=cut?'<tr class="date-group"><td colspan="5"><button type="button" id="episodes-more">'+(episodeAll?'최근 '+EPISODE_LIMIT+'건만 보기':'전체 '+shown.length+'건 보기')+'</button> <span class="muted">'+(episodeAll?'전체 '+shown.length+'건 표시 중 (화면이 느려질 수 있습니다)':'최근 '+EPISODE_LIMIT+'건만 표시 중 · 작업 · 날짜 칩으로 좁혀 볼 수 있습니다')+'</span></td></tr>':'';
 if(setHTML('episodes',!shown.length?'<tr><td colspan="5" class="empty">아직 촬영한 에피소드가 없습니다.<br>캘리브레이션을 확인하고 첫 촬영을 시작하세요.</td></tr>'
  :(episodeTask?dayRows(vis)
  :[...byTask.keys()].map(id=>[id,shown.filter(e=>e.task_id===id),vis.filter(e=>e.task_id===id)]).filter(([,,v])=>v.length).map(([id,eps,v])=>'<tr class="task-group"><td colspan="5">'+esc(taskName(id))+' <span class="muted">· '+eps.length+'건 · QC 완료 '+eps.filter(e=>e.qc).length+' · NAS 완료 '+doneOf(eps)+'</span></td></tr>'+dayRows(v)).join(''))+more)){
  document.querySelectorAll('[data-review]').forEach(b=>b.onclick=()=>openReview(b.dataset.review));
  if($('episodes-more'))$('episodes-more').onclick=()=>{episodeAll=!episodeAll;render()}}
 const done=state.jobs.filter(j=>j.status==='done').length,failed=state.jobs.filter(j=>j.status==='failed').length,active=state.jobs.length-done-failed;
 setHTML('stats',[['NAS 저장 완료',done,'체크섬 검증 완료'],['처리 중 · 대기',active,'원본 수신 / 검증 / NAS'],['확인 필요',failed,'실패 로그 확인 · 재시도'],['활성 작업',state.tasks.filter(t=>t.active).length,'촬영 작업 관리']].map(([l,n,s])=>'<div class="stat"><span>'+l+'</span><strong>'+n+'</strong><small>'+s+'</small></div>').join(''));
 $('stations').innerHTML=(state.stations||[]).map(s=>{const online=Date.now()-Date.parse(s.last_seen)<20000;return '<div class="task-item"><div><b>'+esc(s.id)+'</b><small>카메라 '+(s.cameras||[]).filter(c=>c.connected).length+'/4 · '+esc(s.busy||'대기')+' · 최근 에피소드 '+(s.episodes||[]).length+'건</small></div><span class="badge '+(online?'success':'warn')+'">'+(online?'연결됨':'연결 확인 필요')+'</span></div>'}).join('')||'<p class="muted">촬영 PC가 연결되면 표시됩니다. 촬영 PC의 관리자 화면에서는 로컬 로그를 확인할 수 있습니다.</p>';
 $('task-form').hidden=state.role!=='export';if(setHTML('tasks',state.tasks.map(t=>'<div class="task-item"><div><b>'+esc(t.name)+'</b><small>'+esc(t.scene)+' · 목표 '+t.target+'건 · '+(t.active?'진행 중':'종료')+'</small><small>'+(t.prompt?'SAM "'+esc(t.prompt)+'" · 라벨 '+esc(t.label)+' · '+esc(t.object_name)+'<br>'+esc(t.interaction):'<span class="error">물체 정보 없음 (예전 작업) — 새로 만들어 쓰세요</span>')+'</small></div>'+(state.role==='export'?'<button data-toggle="'+esc(t.id)+'">'+(t.active?'종료':'다시 열기')+'</button>':'')+'</div>').join('')||'<div class="empty">작업을 만들어 시작하세요.</div>'))
  document.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=()=>action(()=>api('/tasks/'+b.dataset.toggle+'/toggle','POST')));
 $('server-state').textContent=state.role==='export'?(state.nas_configured?'NAS 경로 설정됨':'NAS 설정 필요'):'원격 서버';
 if(setHTML('jobs',state.jobs.map(j=>'<div class="job"><div class="job-top"><div><b>'+esc(state.tasks.find(t=>t.id===j.task_id)?.name||j.task_id)+'</b><small>'+esc(j.station)+' · '+esc(j.episode_id?.slice(0,18))+'</small></div>'+badge(j.status)+'</div><div class="progress"><i style="width:'+Math.min(100,Math.max(0,j.progress||0))+'%"></i></div><small>'+esc(j.error|| (j.status==='done'?(j.output||'').split(/[\\/]/).pop()+' · NAS '+(j.timings?.nas_s??'-')+'초 · 전체 '+(j.timings?.total_s??'-')+'초':(j.progress||0)+'%'))+'</small>'+(j.status==='failed'&&state.role==='export'?'<button data-retry="'+esc(j.id)+'">재시도</button>':'')+'</div>').join('')||'<div class="empty">촬영 PC에서 승인한 원본을 보내면<br>여기서 처리 상태를 확인할 수 있습니다.</div>'))
  document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=()=>action(()=>api('/jobs/'+b.dataset.retry+'/retry','POST'),'재시도를 등록했습니다.'));
 {const sel=$('log-station'),cur=sel.value,opts=(state.stations||[]).map(s=>'<option value="'+esc(s.id)+'">'+esc(s.id)+'</option>').join('');if(sel.dataset.options!==opts){sel.innerHTML='<option value="">전체 촬영 PC</option>'+opts;sel.dataset.options=opts;sel.value=cur}}
 renderLogs();
 if(selected)updateReview();
 syncPreview();fitStage();
}
// 작업 로그 탐색 — 조건이 있으면 서버에서 찾아 온다 (에피소드 ID · 내용 · 수준 · 촬영 PC)
let filteredLogs=null,logTimer=null;
function logFilter(){const f={episode:$('log-episode').value.trim(),q:$('log-q').value.trim(),level:$('log-level').value,station:$('log-station').value};return Object.values(f).some(Boolean)?f:null}
async function fetchLogs(){const f=logFilter();if(!f||state?.user_role!=='admin'){filteredLogs=null;return}const p=new URLSearchParams(Object.entries(f).filter(([,v])=>v));p.set('limit','500');try{filteredLogs=await api('/logs?'+p)}catch(e){}}
function renderLogs(){if(!state)return;const f=logFilter(),list=f?(filteredLogs||[]):state.logs;
 $('log-count').textContent=f?'찾은 로그 '+list.length+'건'+(list.length>=500?' (최근 500건까지)':''):'최근 300건 · 자동 갱신';
 if(setHTML('logs',list.map(l=>'<div class="log '+esc(l.level)+'"><time>'+esc(fmtDate(l.at))+'</time><span>'+esc(String(l.level).toUpperCase())+'</span><span class="log-ep">'+(l.episode?'<button type="button" class="ep-chip" data-ep="'+esc(l.episode)+'" title="이 에피소드 로그만 보기">'+esc(l.episode)+'</button>':'')+'</span><span>'+esc(names[l.message]||l.message)+'</span></div>').join('')||'<div class="empty">'+(f?'조건에 맞는 로그가 없습니다.':'아직 로그가 없습니다.')+'</div>'))
  document.querySelectorAll('[data-ep]').forEach(b=>b.onclick=()=>{$('log-episode').value=b.dataset.ep;onLogFilter()})}
function onLogFilter(){clearTimeout(logTimer);logTimer=setTimeout(async()=>{await fetchLogs();renderLogs()},250)}
function epDay(ep){return fmtDate(ep.created_at).slice(0,10)}
function dayLabel(d){const t=new Date(),p=n=>String(n).padStart(2,'0'),f=x=>x.getFullYear()+'-'+p(x.getMonth()+1)+'-'+p(x.getDate()),y=new Date(t.getTime()-864e5);const w=new Date(d+'T00:00:00');const wd=isNaN(w)?'':' ('+'일월화수목금토'[w.getDay()]+')';return d===f(t)?d+wd+' · 오늘':d===f(y)?d+wd+' · 어제':d+wd}
function fmtDate(s){const d=new Date(s);if(isNaN(d))return String(s||'');const p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds())}
// 작업자 촬영 로그 — 이 PC 촬영 로그 + export 서버 NAS 저장 로그 (에피소드 ID · 내용 · 수준으로 찾기)
let wlogTimer=null,wlogBusy=false;
async function fetchWorkerLogs(){if(wlogBusy||!state||state.role!=='capture')return;wlogBusy=true;try{const p=new URLSearchParams();for(const [k,id] of [['episode','wlog-episode'],['q','wlog-q'],['level','wlog-level']]){const v=$(id).value.trim();if(v)p.set(k,v)}p.set('limit','500');renderWorkerLogs(await api('/my-logs?'+p))}catch(e){}finally{wlogBusy=false}}
function renderWorkerLogs(list){const f=['wlog-episode','wlog-q','wlog-level'].some(id=>$(id).value.trim());$('wlog-count').textContent=(f?'찾은 로그 ':'최근 로그 ')+list.length+'건';
 $('wlogs').innerHTML=list.map(l=>'<div class="log '+esc(l.level)+'"><time>'+esc(fmtDate(l.at))+'</time><span>'+esc(String(l.level).toUpperCase())+'</span><span class="log-ep">'+(l.episode?'<button type="button" class="ep-chip" data-wep="'+esc(l.episode)+'" title="이 에피소드 로그만 보기">'+esc(l.episode)+'</button>':'')+'</span><span><span class="log-src">'+esc(l.source||'')+'</span> '+esc(names[l.message]||l.message)+'</span></div>').join('')||'<div class="empty">'+(f?'조건에 맞는 로그가 없습니다.':'아직 로그가 없습니다.')+'</div>';
 document.querySelectorAll('[data-wep]').forEach(b=>b.onclick=()=>{$('wlog-episode').value=b.dataset.wep;onWorkerLogFilter()})}
function onWorkerLogFilter(){clearTimeout(wlogTimer);wlogTimer=setTimeout(fetchWorkerLogs,250)}
async function refresh(){if(polling)return;polling=true;try{state=await api('/state');if(logFilter())await fetchLogs();render();if(view==='logs')fetchWorkerLogs()}catch(e){if(!$('app').hidden){$('connection').hidden=false;$('connection').textContent='연결이 끊겼습니다. 재연결 중 · '+e.message}}finally{polling=false}}
// QC 결과 — 위에 종합 판정, 아래 항목마다 한 줄(핵심 수치). 원문 상세는 펼쳐서 본다. 문제 있는 항목이 먼저, 펼쳐진 채로.
const QC_ORDER={FAIL:0,WARN:1,SKIP:2,PASS:3};
function qcSummary(c){const d=String(c.detail||''),g=r=>{const m=d.match(r);return m?m[1]:null},j=a=>a.filter(Boolean).join('  ·  ');
 switch(c.name){
  case 'frame_continuity':{const per=[...d.matchAll(/(cam_\d):(\d+)d\/(\d+)c/g)].map(x=>x[2]===x[3]?x[2]:x[2]+'/'+x[3]);return j([per.length&&'프레임 '+per.join(' / '),g(/overlap:\s*([\d.]+)s/)&&'겹침 '+g(/overlap:\s*([\d.]+)s/)+'초'])}
  case 'color_quality':return j([g(/brightness\s+([\d.]+)/)&&'밝기 '+g(/brightness\s+([\d.]+)/),g(/exposure-bad frames\s+(\d+)/)!=null&&'노출 불량 '+g(/exposure-bad frames\s+(\d+)/)+'프레임',g(/var\)\s*([\d.]+)/)&&'선명도 '+g(/var\)\s*([\d.]+)/)]);
  case 'depth_completeness':{const per=(g(/per-cam\s+\[([^\]]*)\]/)||'').replace(/'/g,'').split(',').map(x=>x.trim()).filter(Boolean);return j([g(/ratio\s+(\d+%)/)&&'유효 깊이 '+g(/ratio\s+(\d+%)/),per.length&&'카메라별 '+per.join(' / '),g(/dropout-frames\s+(\d+\/\d+)/)&&'누락 '+g(/dropout-frames\s+(\d+\/\d+)/)])}
  case 'cross_cam_residual':return j([g(/median\s+([\d.]+mm)/)&&'잔차 '+g(/median\s+([\d.]+mm)/),g(/overlap\s+(\d+%)/)&&'겹침 '+g(/overlap\s+(\d+%)/)]);
  case 'world_frame':return j([g(/tilt-from-z\s+([\d.]+)deg/)&&'테이블 기울기 '+g(/tilt-from-z\s+([\d.]+)deg/)+'°',g(/table_z\s+([-\d.]+)m/)&&'높이 '+g(/table_z\s+([-\d.]+)m/)+'m',g(/inliers\s+(\d+%)/)&&'평면 일치 '+g(/inliers\s+(\d+%)/)]);
  case 'sync':return j([g(/mean\s+([\d.]+ms)/)&&'평균 '+g(/mean\s+([\d.]+ms)/),g(/max\s+([\d.]+ms)/)&&'최대 '+g(/max\s+([\d.]+ms)/)]);
 }
 return d.length>70?d.slice(0,70)+'…':d}
function renderQC(qc){
 if(!qc?.checks?.length)return '<p class="muted">QC 검사 전입니다. QC 실행을 눌러주세요.</p>';
 const n=s=>qc.checks.filter(c=>c.status===s).length,st=s=>esc(String(s||'').toUpperCase());
 const counts=['PASS','WARN','FAIL','SKIP'].filter(s=>n(s)).map(s=>'<span class="qc-count '+s+'">'+s+' '+n(s)+'</span>').join('');
 const head='<div class="qc-summary '+st(qc.overall)+'"><div><span class="qc-overall">'+esc(qc.overall)+'</span><span class="muted">'+qc.checks.length+'개 항목</span></div><div class="qc-counts">'+counts+(qc.frame_count?'<span class="muted">기준 '+esc(qc.frame_count)+'프레임</span>':'')+'</div></div>';
 const rows=[...qc.checks].sort((a,b)=>(QC_ORDER[a.status]??9)-(QC_ORDER[b.status]??9)).map(c=>{const gates=Array.isArray(c.gates)?c.gates.join(', '):(c.gates||'');
  return '<details class="qc-item '+st(c.status)+'"'+(c.status!=='PASS'?' open':'')+'><summary>'+badge(c.status)+'<b>'+esc(c.label)+'</b><span class="qc-metric">'+esc(qcSummary(c))+'</span></summary>'
   +'<div class="qc-more">'+(c.recommendation?'<p class="qc-rec">'+esc(c.recommendation)+'</p>':'')+'<p class="qc-detail">'+esc(c.detail)+'</p>'+(gates?'<p class="qc-gates">영향 단계: '+esc(gates)+'</p>':'')+'</div></details>'}).join('');
 return head+'<div class="qc-list">'+rows+'</div><p class="fine">항목을 누르면 원문 측정값을 볼 수 있습니다. WARN / FAIL 항목은 펼쳐져 있습니다.</p>'}
function openReview(id){selected=id;reviewFingerprint='';$('review-note').value=state.episodes.find(e=>e.id===id)?.review?.note||'';$('frame-index').value=0;updateReview();$('review-dialog').showModal();syncPreview();loadFrames()}
function updateReview(){const ep=state.episodes.find(e=>e.id===selected);if(!ep)return;const j=jobFor(ep);$('review-info').innerHTML='<b>'+esc(ep.task_name)+'</b><p class="muted">'+esc(ep.id)+'</p>'+badge(ep.status)+' '+(ep.qc?badge(ep.qc.overall):'')+' '+badge(ep.transfer_status)+' '+(j?badge(j.status):'')+'<p class="error">'+esc(ep.error||ep.transfer_error||j?.error||'')+'</p>';
 const fingerprint=JSON.stringify(ep.qc);if(fingerprint!==reviewFingerprint){reviewFingerprint=fingerprint;$('qc-results').innerHTML=renderQC(ep.qc)}
 const locked=['packing','sending','sent','done'].includes(ep.transfer_status);$('run-qc').disabled=!!state.busy||!!ep.export_job||locked||!['captured','qc_done'].includes(ep.status);$('approve').disabled=ep.status!=='qc_done'||!!ep.export_job||locked;$('send').disabled=!ep.review?.approved||locked;const gone=!!ep.local_deleted;$('review-local').hidden=!gone;if(gone){$('review-local').textContent=ep.status==='deleted'?'삭제한 에피소드입니다. 원본이 이 PC 에 없습니다.':'NAS 저장 · 체크섬 확인 뒤 이 PC 원본은 지워졌습니다. NAS: '+(ep.nas_output||'');$('run-qc').disabled=$('approve').disabled=$('send').disabled=true}$('play-frames').disabled=gone;$('delete-episode').disabled=gone||locked||!!ep.export_job||['preparing','recording','saving','qc_running'].includes(ep.status);$('send').textContent=ep.transfer_status==='failed'?'전송 재시도 →':'export 서버로 전달 →';
 $('frame-index').max=Math.max(0,Math.min(...Object.values(ep.frame_counts||{cam:150}))-1);
}
// 검수 화면 프레임 — 열 때 4개 영상의 모든 프레임을 미리 받아 두고(서버가 한 번 풀어 둔 JPEG), 재생은 0.033초마다 다음 프레임
const FRAME_MS=33;let framePool=null,playing=false,playToken=0,showSeq=0,shownIndex=-1;const front={};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function frameMax(){return Number($('frame-index').max)||0}
function frameUrl(n,i){return framePool?.urls['cam_'+n]?.[i]||('/api/episodes/'+encodeURIComponent(selected)+'/frame/cam_'+n+'?index='+i)}
function setFrameLabel(i){$('frame-index').value=i;$('frame-label').textContent=i+' / '+frameMax()+' · '+(i*FRAME_MS/1000).toFixed(2)+'초'}
// 더블 버퍼 — 카메라마다 이미지 두 장. 다음 프레임을 숨은 쪽에서 다 풀어(decode) 둔 뒤 4대를 한 번에 바꾼다 → 넘어갈 때 빈(검은) 화면이 끼지 않는다
async function showFrame(i){i=Math.max(0,Math.min(frameMax(),i));setFrameLabel(i);const seq=++showSeq;
 await Promise.all([1,2,3,4].map(async n=>{const im=$('rv-'+(front[n]==='a'?'b':'a')+'-'+n);if(!im)return;const url=frameUrl(n,i);if(im.dataset.url!==url){im.dataset.url=url;im.src=url}try{await Promise.race([im.decode(),sleep(300)])}catch(e){}}));
 if(seq!==showSeq||!$('rv-a-1'))return false;   // 그사이 다른 프레임을 요청했으면 그쪽을 보여 준다
 for(let n=1;n<=4;n++){const nb=front[n]==='a'?'b':'a';$('rv-'+nb+'-'+n).classList.add('on');$('rv-'+front[n]+'-'+n).classList.remove('on');front[n]=nb;const lb=$('review-lbl-'+n);if(lb)lb.textContent='CAM 0'+n+' · FRAME '+i}
 shownIndex=i;return true}
function loadFrames(){if(!selected)return;clearFrames();if(state.episodes.find(e=>e.id===selected)?.local_deleted){$('review-frames').innerHTML='<p class="muted">원본이 이 PC 에 없어 프레임을 볼 수 없습니다.</p>';return}for(let n=1;n<=4;n++)front[n]='b';shownIndex=-1;
 $('review-frames').innerHTML=[1,2,3,4].map(n=>'<div class="cam"><img class="fb" id="rv-a-'+n+'" alt=""><img class="fb on" id="rv-b-'+n+'" alt="카메라 '+n+' 저장 프레임"><span class="cam-label" id="review-lbl-'+n+'"></span></div>').join('');
 showFrame(Number($('frame-index').value)||0);preloadFrames()}
async function preloadFrames(){const key=selected,max=frameMax();framePool={key,urls:{cam_1:[],cam_2:[],cam_3:[],cam_4:[]},done:0};const pool=framePool,total=(max+1)*4,jobs=[];
 for(let i=0;i<=max;i++)for(let n=1;n<=4;n++)jobs.push([n,i]);
 const work=async()=>{while(jobs.length&&framePool===pool){const [n,i]=jobs.shift();try{const r=await fetch('/api/episodes/'+encodeURIComponent(key)+'/frame/cam_'+n+'?index='+i);if(r.ok&&framePool===pool)pool.urls['cam_'+n][i]=URL.createObjectURL(await r.blob())}catch(e){}pool.done++;if(framePool===pool&&(pool.done%20===0||pool.done===total))$('frame-preload').textContent=pool.done<total?'프레임 불러오는 중 '+Math.round(pool.done/total*100)+'%':'프레임 준비 완료'}};
 await Promise.all([1,2,3,4,5,6].map(work))}
function frameReady(i){return !framePool||[1,2,3,4].every(n=>framePool.urls['cam_'+n][i])}
// 재생 — 다음 프레임 4장이 준비·디코딩되면 바꾸고, 0.033초 간격을 맞춘다 (준비가 늦으면 기다렸다가 이어 감)
async function playLoop(token){let due=performance.now();
 while(playing&&token===playToken){const i=shownIndex+1;if(i>frameMax()){setPlaying(false);break}
  if(!frameReady(i)){await sleep(10);due=performance.now();continue}
  due+=FRAME_MS;await showFrame(i);const wait=due-performance.now();if(wait>0)await sleep(wait);else due=performance.now()}}
function setPlaying(on){playing=on;const token=++playToken;$('play-frames').textContent=on?'❚❚ 멈춤':'▶ 재생';if(!on)return;
 (async()=>{const cur=Number($('frame-index').value);if(cur>=frameMax())await showFrame(0);else if(cur!==shownIndex)await showFrame(cur);if(token===playToken)playLoop(token)})()}
function clearFrames(){setPlaying(false);if(framePool)for(const urls of Object.values(framePool.urls))urls.forEach(u=>u&&URL.revokeObjectURL(u));framePool=null;$('frame-preload').textContent=''}
$('login-form').onsubmit=async e=>{e.preventDefault();$('login-error').textContent='';try{await api('/login','POST',{role:$('login-role').value,password:$('password').value});$('password').value='';await refresh()}catch(e){$('login-error').textContent=e.message}};
$('logout').onclick=async()=>{await api('/logout','POST');showLogin()};document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>setView(b.dataset.view));
$('task-select').onchange=()=>render();
for(const id of ['wlog-episode','wlog-q'])$(id).oninput=onWorkerLogFilter;$('wlog-level').onchange=onWorkerLogFilter;$('wlog-clear').onclick=()=>{for(const id of ['wlog-episode','wlog-q','wlog-level'])$(id).value='';fetchWorkerLogs()};
$('delete-episode').onclick=()=>{const ep=state.episodes.find(e=>e.id===selected);if(!ep||!confirm(ep.id+' 에피소드를 삭제할까요?\n이 PC 의 원본 파일이 지워지고 되돌릴 수 없습니다. (NAS 로 보내지 않을 에피소드만 지우세요)'))return;action(async()=>{await api('/episodes/'+ep.id+'/delete','POST');$('review-dialog').close()},'에피소드를 삭제했습니다.')};
for(const id of ['log-episode','log-q'])$(id).oninput=onLogFilter;for(const id of ['log-level','log-station'])$(id).onchange=onLogFilter;
$('log-clear').onclick=()=>{for(const id of ['log-episode','log-q','log-level','log-station'])$(id).value='';filteredLogs=null;renderLogs()};$('snapshot').onclick=()=>action(()=>api('/calibration/snapshot','POST'),'보드 프레임을 캡처했습니다.');$('reset-calib').onclick=()=>action(()=>api('/calibration/reset','POST'),'보드 캡처를 초기화했습니다.');
$('calib-form').onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.target));for(const k of ['squares_x','squares_y','square_length_mm','marker_length_mm'])d[k]=Number(d[k]);action(()=>api('/calibration/compute','POST',d),'계산 완료. 오차를 확인하고 승인하세요.')};
$('record').onclick=()=>{const s=recSeconds;startLive(true);action(async()=>{try{await api('/capture/'+encodeURIComponent($('task-select').value)+'?seconds='+s,'POST')}catch(e){stopLive();throw e}},s+'초 촬영 — 카운트다운이 끝나면 녹화를 시작합니다.')};
document.querySelectorAll('[data-sec]').forEach(b=>b.onclick=()=>{if(state?.busy)return;recSeconds=Number(b.dataset.sec);try{localStorage.setItem('rec_seconds',String(recSeconds))}catch(e){}if(state)render()});
$('fullscreen').onclick=()=>{if(document.fullscreenElement){document.exitFullscreen()}else{const p=$('camera-panel').requestFullscreen?.();if(p)p.catch(e=>toast('전체 화면을 열 수 없습니다: '+e.message,true))}};
document.addEventListener('fullscreenchange',()=>{$('fullscreen').textContent=document.fullscreenElement?'✕ 전체 화면 닫기':'⛶ 전체 화면';fitStage()});
// 카메라 4대 영역을 창의 남은 높이에 맞춘다 (위쪽 알림 줄이 있어도 4대가 한 화면에 들어오게). 좁은 화면 · 전체 화면은 CSS 가 맡는다
function fitStage(){const st=$('stage');if(!st)return;if(document.fullscreenElement||window.innerWidth<=800||st.offsetParent===null){st.style.height='';return}
 const top=st.getBoundingClientRect().top+window.scrollY;st.style.height=Math.max(360,Math.round(window.innerHeight-top-44))+'px'}
window.addEventListener('resize',fitStage);
// 녹화 타이머 — 버튼을 누른 순간이 아니라 서버가 4대 모두 첫 프레임을 실제로 기록한 시각(started)부터 센다.
// 0.15초마다 /api/capture/live 를 묻고, 그 사이는 서버 시계와의 차이(liveOffset)로 매 화면마다 다시 그린다.
let live=null,liveOffset=0,livePoll=null,liveRaf=0,liveHide=null,liveWaiting=false;
function startLive(waiting){clearTimeout(liveHide);if(waiting){liveWaiting=true;live={phase:'preparing',seconds:recSeconds,fps:30,frames:null};drawLive()}if(!livePoll)pollLive()}
function stopLive(){clearTimeout(livePoll);livePoll=null;live=null;liveWaiting=false;cancelAnimationFrame(liveRaf);$('rec-timer').hidden=true;$('countdown').hidden=true;$('stage').classList.remove('recording')}
async function pollLive(){livePoll=-1;try{const t0=Date.now(),d=await api('/capture/live'),t1=Date.now();liveOffset=d.server_now-(t0+t1)/2000;
  if(d.active){liveWaiting=false;live=d;drawLive();livePoll=setTimeout(pollLive,150);return}
  if(liveWaiting){livePoll=setTimeout(pollLive,150);return}   // 시작 요청이 아직 서버에 닿기 전
  const last=live;livePoll=null;if(last&&last.phase!=='preparing'){live=Object.assign({},last,{phase:'done'});drawLive();liveHide=setTimeout(stopLive,4000)}else stopLive()}
 catch(e){livePoll=setTimeout(pollLive,500)}}
function drawLive(){cancelAnimationFrame(liveRaf);const lv=live,box=$('rec-timer');if(!lv){box.hidden=true;$('countdown').hidden=true;return}
 box.hidden=false;box.className='rec-timer '+lv.phase;$('stage').classList.toggle('recording',lv.phase==='recording');
 const total=lv.seconds,fps=lv.fps||30;$('rt-total').textContent='/ '+total+'초';
 // 녹화 전 3초 카운트다운: 촬영자가 볼 수 있게 화면 전체에 큰 숫자 (그동안 서버는 실제 녹화와 같은 처리를 돌려 안정시킨다 · 저장 안 함)
 $('countdown').hidden=lv.phase!=='countdown';
 if(lv.phase==='countdown'){box.hidden=true;
  const left=lv.go?Math.max(0,lv.go-(Date.now()/1000+liveOffset)):(lv.countdown||3);
  $('cd-num').textContent=String(Math.max(1,Math.ceil(left)));
  liveRaf=requestAnimationFrame(drawLive);return}
 let el=0;if(lv.started&&lv.phase==='recording')el=Math.max(0,Math.min(total,Date.now()/1000+liveOffset-lv.started));else if(lv.phase==='saving'||lv.phase==='done')el=total;
 $('rt-elapsed').textContent=el.toFixed(1);$('rt-progress').style.width=(el/total*100)+'%';
 $('rt-phase').textContent=lv.phase==='preparing'?'○ 카메라 첫 프레임 기다리는 중…':lv.phase==='recording'?'● REC · 프레임 기록 중 · 남은 '+(total-el).toFixed(1)+'초':lv.phase==='saving'?'■ 촬영 끝 · 원본 저장 중':'✓ 촬영 완료';
 const f=lv.frames||{},keys=Object.keys(f);$('rt-frames').textContent=keys.length?keys.map(c=>c.replace('cam_','CAM ')+' '+f[c]).join(' · ')+'  (목표 '+total*fps+'장)':'목표 카메라마다 '+total*fps+'장';
 if(lv.phase==='recording')liveRaf=requestAnimationFrame(drawLive)}
$('qc-batch').onclick=()=>action(async()=>{const r=await api('/qc/batch','POST');toast(r.queued+'개 QC 를 시작합니다. 전체 PASS 는 자동으로 NAS 전송, 나머지는 검수가 필요합니다.')});
$('task-form').onsubmit=e=>{e.preventDefault();action(async()=>{await api('/tasks','POST',{name:$('task-name').value,scene:$('task-scene').value,target:Number($('task-target').value),prompt:$('task-prompt').value,label:$('task-label').value,object_name:$('task-object-name').value,interaction:$('task-interaction').value});for(const id of ['task-name','task-prompt','task-label','task-object-name','task-interaction'])$(id).value=''},'작업을 만들었습니다.')};
$('close-review').onclick=()=>{$('review-dialog').close();selected=null};$('review-dialog').addEventListener('close',()=>{clearFrames();selected=null;syncPreview()});let frameTimer;$('frame-index').oninput=()=>showFrame(Number($('frame-index').value));$('play-frames').onclick=()=>setPlaying(!playing);
$('run-qc').onclick=()=>action(()=>api('/episodes/'+selected+'/qc','POST'),'기존 촬영 QC를 실행합니다.');$('approve').onclick=()=>action(()=>api('/episodes/'+selected+'/approve','POST',{note:$('review-note').value}),'검수 승인 완료.');$('send').onclick=()=>action(()=>api('/episodes/'+selected+'/send','POST'),'서버 전달을 시작합니다. NAS 완료는 별도로 표시됩니다.');
(async()=>{info=await api('/info');$('login-info').textContent=(info.role==='capture'?'촬영 PC':'export 서버')+' · '+info.station+(info.demo?' · 데모':'');if(location.pathname==='/admin')$('login-role').value='admin';await refresh()})().catch(e=>toast(e.message,true));
setInterval(()=>{if(!$('app').hidden)refresh()},2000);
// 미리보기: 카메라 4대를 스트림 하나(/api/preview/stream)로 받아 칸마다 나눠 그린다 (15fps).
// 카메라별 스트림 4개는 브라우저(Edge · Chrome)의 같은 주소 동시 연결 6개 중 4개를 계속 차지해 상태 갱신 · 버튼 요청이 줄을 섰다.
// 검수 창이 열리면 끊는다 (그동안 디코딩을 쉬고, 저장 프레임 받기에 연결을 비워 준다).
let previewCtl=null;const previewBusy={},previewNext={};
function syncPreview(){const on=!$('app').hidden&&view==='worker'&&!!state&&state.role==='capture'&&!$('review-dialog').open;if(on&&!$('preview-1'))return;if(on===previewOn)return;previewOn=on;
 if(on)startPreview();else{stopPreview();for(let n=1;n<=4;n++)clearPreview(n)}}
function stopPreview(){if(previewCtl){previewCtl.abort();previewCtl=null}}
function startPreview(){stopPreview();const ctl=new AbortController();previewCtl=ctl;
 (async()=>{while(previewCtl===ctl){try{const r=await fetch('/api/preview/stream?t='+Date.now(),{signal:ctl.signal,cache:'no-store'});if(!r.ok)throw Error(String(r.status));await readPreview(r.body.getReader(),ctl)}catch(e){if(ctl.signal.aborted)return}
  await sleep(2000)}})()}   // 끊기면 (서버 재시작 · 세션 만료) 2초 뒤 다시 연결
// multipart 읽기: 파트마다 X-Camera · Content-Length 헤더 뒤에 JPEG
async function readPreview(reader,ctl){const dec=new TextDecoder();let buf=new Uint8Array(0);
 const headEnd=b=>{for(let i=0;i+3<b.length;i++)if(b[i]===13&&b[i+1]===10&&b[i+2]===13&&b[i+3]===10)return i;return -1};
 for(;;){const {value,done}=await reader.read();if(done||previewCtl!==ctl)return;
  const next=new Uint8Array(buf.length+value.length);next.set(buf);next.set(value,buf.length);buf=next;
  for(;;){const he=headEnd(buf);if(he<0)break;const head=dec.decode(buf.subarray(0,he)),len=Number((/content-length:\s*(\d+)/i.exec(head)||[])[1]),cam=(/x-camera:\s*(cam_\d)/i.exec(head)||[])[1];
   if(!(len>=0)){buf=buf.subarray(he+4);continue}
   if(buf.length<he+4+len)break;   // JPEG 가 아직 다 안 왔다
   const jpg=buf.slice(he+4,he+4+len);buf=buf.subarray(he+4+len);if(cam)drawPreview(cam.slice(4),jpg)}}}
// 디코딩이 밀리면 중간 프레임은 버리고 가장 최신 것만 그린다
async function drawPreview(n,jpg){previewNext[n]=jpg;if(previewBusy[n])return;previewBusy[n]=true;
 try{while(previewNext[n]){const data=previewNext[n];previewNext[n]=null;const bmp=await createImageBitmap(new Blob([data],{type:'image/jpeg'}));const cv=$('preview-'+n);
  if(cv&&previewOn){if(cv.width!==bmp.width||cv.height!==bmp.height){cv.width=bmp.width;cv.height=bmp.height}cv.getContext('2d').drawImage(bmp,0,0)}bmp.close()}}
 catch(e){}finally{previewBusy[n]=false}}
function clearPreview(n){const cv=$('preview-'+n);if(cv&&cv.width){cv.width=0;cv.height=0}}
