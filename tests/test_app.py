import importlib.util
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('context_handoff_server',ROOT/'server.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();app.DB=Path(self.temp.name)/'db.sqlite3';app.PENDING.clear()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.state=self.call('GET','/api/state')[1]
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2);self.temp.cleanup()
    def call(self,method,path,obj=None,headers=None):
        raw=None if obj is None else json.dumps(obj).encode()
        h={'X-Local-Action':app.TOKEN};h.update(headers or {})
        if raw is not None:h['Content-Type']='application/json'
        req=Request(self.base+path,data=raw,headers=h,method=method)
        try:
            with urlopen(req) as response:
                payload=response.read()
                if response.status==204:return response.status,None
                return response.status,json.loads(payload) if response.headers.get('Content-Type','').startswith('application/json') else payload.decode()
        except HTTPError as e:
            with e:
                payload=e.read()
                return e.code,json.loads(payload) if e.headers.get('Content-Type','').startswith('application/json') else payload.decode()
    def test_sample_is_draft_and_pending_checks_never_count_complete(self):
        _,d=self.call('GET','/api/state')
        self.assertNotEqual(d['approved']['files'],d['current']['files'])
        self.assertEqual(app.completion(d['current']),'in_progress')
        complete=app.default_snapshot();complete['checks']=complete['checks'][:1];complete['questions']=[]
        self.assertEqual(app.completion(complete),'complete')
        complete['checks'][0]['evidence']='';self.assertEqual(app.completion(complete),'in_progress')
    def test_save_validates_strict_schema_and_passed_evidence(self):
        self.assertEqual(self.call('PUT','/api/state',[]) [0],400)
        bad={**self.state['current'],'checks':[{'id':'a','text':'test','status':'passed','evidence':''}]}
        self.assertEqual(self.call('PUT','/api/state',bad)[0],400)
        bad={**self.state['current'],'transcript':'private transcript'}
        status,body=self.call('PUT','/api/state',bad);self.assertEqual(status,400);self.assertIn('does not import transcripts',body['error'])
        unknown={**self.state['current'],'checks':[{'id':'a','text':'x','status':'passed','evidence':'ok','credential':'x'}]}
        self.assertEqual(self.call('PUT','/api/state',unknown)[0],400)
    def test_review_resume_compares_approved_and_rejects_stale_preview(self):
        candidate=app.default_snapshot();candidate['files'][0]['change']='Updated after review.'
        status,preview=self.call('POST','/api/resume-preview',{'snapshot':candidate});self.assertEqual(status,200)
        self.assertTrue(any(x['area']=='files' and x['state']=='changed' for x in preview['changes']))
        edited={**self.state['current'],'objective':'A concurrent edit.'}
        self.assertEqual(self.call('PUT','/api/state',edited)[0],200)
        status,body=self.call('POST','/api/resume',{'preview_id':preview['preview_id']})
        self.assertEqual(status,400);self.assertIn('state changed',body['error'])
        self.assertEqual(self.call('POST','/api/resume',{'preview_id':preview['preview_id']})[0],400)
    def test_export_redacts_selected_fields_and_approval_requires_unchanged_draft(self):
        status,preview=self.call('POST','/api/export-preview',{'redact':['objective','files']});self.assertEqual(status,200)
        self.assertEqual(preview['snapshot']['objective'],'');self.assertEqual(preview['snapshot']['files'],[])
        self.assertEqual(preview['snapshot']['checks'],self.state['current']['checks'])
        current={**self.state['current'],'objective':'Changed after preview.'}
        self.assertEqual(self.call('PUT','/api/state',current)[0],200)
        status,body=self.call('POST','/api/approve-export',{'preview_id':preview['preview_id']})
        self.assertEqual(status,400);self.assertIn('Draft changed',body['error'])
        status,body=self.call('POST','/api/export-preview',{'redact':['schema_version']});self.assertEqual(status,400)
    def test_redacted_question_text_keeps_open_state_incomplete_after_resume(self):
        complete=app.default_snapshot();complete['checks']=complete['checks'][:1];complete['questions']=[{'id':'q-open','question':'Pending user decision','status':'open','answer':''}]
        self.assertEqual(self.call('PUT','/api/state',complete)[0],200)
        status,preview=self.call('POST','/api/export-preview',{'redact':['questions']});self.assertEqual(status,200)
        self.assertEqual(preview['snapshot']['questions'],[{'id':'q-open','question':'[REDACTED]','status':'open','answer':''}])
        _,approved=self.call('POST','/api/approve-export',{'preview_id':preview['preview_id']})
        status,resume=self.call('POST','/api/resume-preview',{'snapshot':approved['snapshot']});self.assertEqual(status,200)
        _,loaded=self.call('POST','/api/resume',{'preview_id':resume['preview_id']})
        self.assertEqual(loaded['completion'],'in_progress')
    def test_approval_then_resume_preserves_snapshot_and_local_database(self):
        status,preview=self.call('POST','/api/export-preview',{'redact':['questions']});self.assertEqual(status,200)
        status,approved=self.call('POST','/api/approve-export',{'preview_id':preview['preview_id']});self.assertEqual(status,200)
        _,saved=self.call('GET','/api/state');self.assertEqual(saved['approved'],approved['snapshot'])
        candidate=app.default_snapshot();candidate['objective']='Compare with approved state.'
        _,resume=self.call('POST','/api/resume-preview',{'snapshot':candidate})
        self.assertTrue(any(x['area']=='objective' and x['state']=='changed' for x in resume['changes']))
        _,loaded=self.call('POST','/api/resume',{'preview_id':resume['preview_id']});self.assertEqual(loaded['current'],candidate)
        _,again=self.call('GET','/api/state');self.assertEqual(again['approved'],approved['snapshot'])
    def test_boundary_rejects_wrong_host_origin_and_missing_token(self):
        self.assertEqual(self.call('PUT','/api/state',self.state['current'],{'Origin':'https://attacker.example'})[0],403)
        self.assertEqual(self.call('POST','/api/reset',{}, {'X-Local-Action':'wrong'})[0],403)
        self.assertEqual(self.call('GET','/',headers={'Host':'attacker.example'})[0],403)
    def test_preview_cache_expires_entries_and_stays_capped(self):
        now=app.datetime.now(app.timezone.utc).timestamp()
        app.PENDING['expired']={'expires':now-1}
        for i in range(app.MAX_PENDING):app.PENDING[f'p{i}']={'expires':now+100+i}
        app.store_pending('new',{'expires':now+1000})
        self.assertNotIn('expired',app.PENDING)
        self.assertNotIn('p0',app.PENDING)
        self.assertIn('new',app.PENDING)
        self.assertLessEqual(len(app.PENDING),app.MAX_PENDING)
    def test_rendering_escapes_user_text_and_uses_no_remote_assets(self):
        status,html=self.call('GET','/');self.assertEqual(status,200);self.assertIn('<h1>AI Task Handoff</h1>',html)
        self.assertIn('Manual JSON only.',html);self.assertNotIn('googleapis.com',html);self.assertNotIn('transcript"',html)
        dangerous={**self.state['current'],'objective':'<img src=x onerror=alert(1)>'}
        self.call('PUT','/api/state',dangerous)
        _,js=self.call('GET','/app.js');self.assertIn('esc(v)',js);self.assertIn('textContent',js)
        self.assertIn('approved={current:d.approved,approved_at:d.approved_at}',js)
        self.assertIn('Redaction choices changed. Build the preview again.',js)
        self.assertIn('label.htmlFor=input.id',js)

    def test_database_connection_closes_when_setup_fails(self):
        connection=Mock()
        connection.execute.side_effect=RuntimeError('injected setup failure')
        with patch.object(app,'prepare_database'), patch.object(app.sqlite3,'connect',return_value=connection):
            with self.assertRaisesRegex(RuntimeError,'injected setup failure'):
                app.database()
        connection.close.assert_called_once_with()

if __name__=='__main__':unittest.main()
