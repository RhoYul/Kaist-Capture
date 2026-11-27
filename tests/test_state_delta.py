"""목록을 '바뀐 것만' 주고받기 — 기록이 수천 건이어도 상태 갱신이 가벼워야 한다.
(예전: 화면이 2초마다 에피소드 · NAS 작업 전체를 받았다. 1,800건이면 6MB · 1.5~2.5초)"""
import json, sqlite3, tempfile, unittest
from pathlib import Path
from fastapi.testclient import TestClient
from studio.core import Store, DEFAULTS, overlap, newest
from studio.app import create_app

OLD='2026-01-01T00:00:00+00:00'

def config(root,role='export'):
    nas=root/'nas';nas.mkdir(exist_ok=True)
    return dict(DEFAULTS,role=role,data_dir=str(root/'data'),nas_root=str(nas),demo=True,transfer_token='test-transfer-0123456789',admin_password='test-admin-0123456789',worker_password='test-worker-0123456789')

def put_old(db,kind,key,**body):
    """오래전에 바뀐 기록 (Store.put 은 updated_at 을 지금으로 쓰므로 직접 넣는다)."""
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)',(kind,key,json.dumps(dict(body,id=key,updated_at=OLD))))

class StoreFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Store(Path(self.tmp.name)/'data')
    def tearDown(self): self.tmp.cleanup()
    def test_since_active_limit(self):
        for i in range(30): put_old(self.db,'episode','ep_old%02d'%i,status='qc_done',local_deleted=True,calibration=dict(big='x'*100))
        put_old(self.db,'episode','ep_kept',status='qc_done')                    # 오래됐지만 원본이 이 PC 에 남음
        new=self.db.put('episode','ep_new',dict(status='captured',calibration=dict(big='y')))
        self.assertEqual(len(self.db.all('episode')),32)
        self.assertEqual({e['id'] for e in self.db.all('episode',active=True)},{'ep_kept','ep_new'})
        self.assertEqual([e['id'] for e in self.db.all('episode',since=overlap(new['updated_at']))],['ep_new'])
        self.assertEqual(self.db.all('episode',since=new['updated_at']),[])
        self.assertEqual([e['id'] for e in self.db.all('episode',limit=1)],['ep_new'])   # 최근에 바뀐 것부터
        self.assertNotIn('calibration',self.db.all('episode',drop=('calibration',),active=True)[0])
        self.assertEqual(self.db.all('job',active=True),[])
    def test_filters_use_indexes(self):
        with self.db.connect() as c:
            plan=lambda where,*a:' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT body FROM records WHERE kind=? AND '+where,('episode',*a)))
            self.assertIn('records_updated',plan("json_extract(body,'$.updated_at')>?",OLD))
            self.assertIn('records_active',plan("coalesce(json_extract(body,'$.local_deleted'),0)=0"))
    def test_cursor_helpers(self):
        self.assertIsNone(overlap('not a time'));self.assertIsNone(overlap(None))
        self.assertLess(overlap('2026-11-27T05:00:10.500000+00:00'),'2026-11-27T05:00:06+00:00')
        self.assertEqual(newest([dict(updated_at='b'),dict(updated_at='a'),{}],'c'),'c')
        self.assertEqual(newest([dict(updated_at='b')]),'b');self.assertIsNone(newest([]))

class StateDeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cfg=config(self.root)
        self.db=Store(self.cfg['data_dir']);self.auth={'Authorization':'Bearer '+self.cfg['transfer_token']}
        for i in range(40): put_old(self.db,'job','job_old%02d'%i,status='done',episode_id='ep_%02d'%i,task_id='t',station='s')
        for i in range(250): put_old(self.db,'qc','ep_q%03d'%i,status='done')
        # 방금 바뀐 것 하나씩 — 커서는 가장 늦은 updated_at 이고, 놓치지 않게 그 5초 앞부터 겹쳐 읽으므로 이것은 다음 응답에도 다시 온다
        self.db.put('job','job_recent',dict(status='nas_copy',episode_id='ep_recent',task_id='t',station='s'))
        self.db.put('qc','ep_recent',dict(status='done'))
    def tearDown(self): self.tmp.cleanup()
    def test_machine_state_sends_only_changes(self):
        with TestClient(create_app(self.cfg)) as c:
            self.assertEqual(c.get('/api/machine/state').status_code,401)
            first=c.get('/api/machine/state',headers=self.auth).json()          # 예전 촬영 서버처럼 아무것도 안 주면 전체
            self.assertTrue(first['full']);self.assertEqual(len(first['jobs']),41);self.assertEqual(len(first['qc']),200)
            self.assertEqual(first['qc'][0]['id'],'ep_recent')                  # 전체일 때 QC 는 최근 200건 (예전과 같음)
            self.db.put('job','job_new',dict(status='queued',episode_id='ep_new',task_id='t',station='s'))
            self.db.put('qc','ep_new',dict(status='running'))
            p=dict(since=first['cursor'],epoch=first['epoch'])
            d=c.get('/api/machine/state',headers=self.auth,params=p).json()
            self.assertFalse(d['full'])
            self.assertEqual({j['id'] for j in d['jobs']},{'job_new','job_recent'});self.assertEqual({q['id'] for q in d['qc']},{'ep_new','ep_recent'})
            self.assertGreater(d['cursor'],first['cursor'])
            again=c.get('/api/machine/state',headers=self.auth,params=dict(since=d['cursor'],epoch=d['epoch'])).json()
            self.assertFalse(again['full']);self.assertLessEqual(len(again['jobs']),2)   # 바뀐 게 없어도 겹친 구간의 것만 다시 온다 (합치면 같다)
            self.assertEqual(again['cursor'],d['cursor'])
            # 서버가 다시 켜졌으면 (epoch 가 다름) 전체
            other=c.get('/api/machine/state',headers=self.auth,params=dict(since=d['cursor'],epoch='boot_other')).json()
            self.assertTrue(other['full']);self.assertEqual(len(other['jobs']),42)
    def test_screen_state_sends_only_changes(self):
        with TestClient(create_app(self.cfg)) as c:
            c.post('/api/login',json=dict(role='admin',password=self.cfg['admin_password']))
            first=c.get('/api/state').json()
            self.assertTrue(first['jobs_full']);self.assertTrue(first['episodes_full']);self.assertEqual(len(first['jobs']),41)
            self.db.put('job','job_new',dict(status='queued',episode_id='ep_new',task_id='t',station='s'))
            d=c.get('/api/state',params=dict(epoch=first['epoch'],jsince=first['jcursor'])).json()
            self.assertFalse(d['jobs_full']);self.assertEqual({j['id'] for j in d['jobs']},{'job_new','job_recent'})
            self.assertEqual(d['epoch'],first['epoch']);self.assertGreater(d['jcursor'],first['jcursor'])
            self.assertTrue(c.get('/api/state',params=dict(epoch='boot_x|',jsince=first['jcursor'])).json()['jobs_full'])   # 다른 서버의 커서
            self.assertTrue(c.get('/api/state',params=dict(epoch=first['epoch'],jsince='garbage')).json()['jobs_full'])     # 읽을 수 없는 커서
            self.assertEqual(len(c.get('/api/state').json()['jobs']),42)                                                    # 커서 없이 묻는 쪽(스크립트)은 늘 전체

if __name__=='__main__': unittest.main()
