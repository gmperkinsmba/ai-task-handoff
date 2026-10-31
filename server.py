#!/usr/bin/env python3
"""Manual, local task handoffs with explicit review and resume comparison."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from desktop_server import make_server as _make_desktop_server
from pathlib import Path
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('CONTEXT_HANDOFF_DATA',Path.home()/'.local/share/context-handoff'))
DATA.mkdir(mode=0o700,parents=True,exist_ok=True)
try:DATA.chmod(0o700)
except OSError:pass
DB=DATA/'handoffs.sqlite3';TOKEN=secrets.token_urlsafe(24);MAX_BODY=1_000_000;MAX_PENDING=100;PENDING={}
LISTS=('decisions','files','checks','questions','failed_approaches')
ALLOWED={'schema_version','objective',*LISTS}
FIELD_LIMITS={'objective':4000,'text':1000,'evidence':2000,'path':500,'change':1500,'status':20,'question':1000,'answer':1500,'summary':1000,'reason':1500}

def database():
    db=sqlite3.connect(DB);db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS workspace (id TEXT PRIMARY KEY, current_json TEXT NOT NULL, approved_json TEXT NOT NULL, approved_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
    return db

def clean_text(value,field,required=False):
    if not isinstance(value,str):raise ValueError(f'{field} must be text.')
    value=value.strip()
    if required and not value:raise ValueError(f'{field} is required.')
    if len(value)>FIELD_LIMITS.get(field,1500):raise ValueError(f'{field} is too long.')
    return value

def default_snapshot():
    return {'schema_version':'1','objective':'Finish a local search page and verify its source links.','decisions':[{'id':'d-1','text':'Keep imports local and preserve the original source offsets.','evidence':'Confirmed against the import contract.'}],'files':[{'id':'f-1','path':'src/search.py','change':'Added local result ranking and source links.'}],'checks':[{'id':'c-1','text':'Search returns the expected synthetic result.','status':'passed','evidence':'Unit check: query "blue label" returned one result.'},{'id':'c-2','text':'Keyboard users can open a result.','status':'pending','evidence':''}],'questions':[{'id':'q-1','question':'Should results include archived threads?','status':'open','answer':''}],'failed_approaches':[{'id':'a-1','summary':'A loose source search matched repeated text at the wrong offset.','reason':'Offsets now use parsed message boundaries.'}]}

def sample_draft():
    draft=default_snapshot();draft['files'][0]['change']+=' Source inspector review remains open.'
    return draft

def validate_snapshot(obj):
    if not isinstance(obj,dict):raise ValueError('Handoff must be a JSON object.')
    unknown=set(obj)-ALLOWED
    if unknown:raise ValueError(f'Unsupported fields: {", ".join(sorted(unknown))}. This format does not import transcripts, credentials, or provider metadata.')
    if obj.get('schema_version')!='1':raise ValueError('Only Context Handoff schema version 1 is supported.')
    out={'schema_version':'1','objective':clean_text(obj.get('objective',''),'objective')}
    if not isinstance(obj.get('decisions'),list) or not isinstance(obj.get('files'),list) or not isinstance(obj.get('checks'),list) or not isinstance(obj.get('questions'),list) or not isinstance(obj.get('failed_approaches'),list):raise ValueError('Decisions, files, checks, questions, and failed approaches must be lists.')
    specs={
      'decisions':({'id','text','evidence'},{'text':'text','evidence':'evidence'}),
      'files':({'id','path','change'},{'path':'path','change':'change'}),
      'checks':({'id','text','status','evidence'},{'text':'text','status':'status','evidence':'evidence'}),
      'questions':({'id','question','status','answer'},{'question':'question','status':'status','answer':'answer'}),
      'failed_approaches':({'id','summary','reason'},{'summary':'summary','reason':'reason'})}
    seen=set()
    for group,(allowed,fields) in specs.items():
        values=[]
        if len(obj[group])>200:raise ValueError(f'{group} has too many entries.')
        for ix,entry in enumerate(obj[group],1):
            if not isinstance(entry,dict) or set(entry)-allowed:raise ValueError(f'{group} item {ix} has unsupported fields.')
            ident=entry.get('id')
            if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',ident):raise ValueError(f'{group} item {ix} needs a string ID.')
            if ident in seen:raise ValueError('Handoff item IDs must be unique strings.')
            seen.add(ident);value={'id':ident}
            for key,field in fields.items():value[key]=clean_text(entry.get(key,''),field,required=key not in ('evidence','answer'))
            if group=='checks':
                if value['status'] not in ('pending','running','passed','failed','skipped'):raise ValueError('Check state must be pending, running, passed, failed, or skipped.')
                if value['status']=='passed' and not value['evidence']:raise ValueError('A passed check needs evidence.')
            if group=='questions' and value['status'] not in ('open','resolved'):raise ValueError('Question state must be open or resolved.')
            if group=='questions' and value['status']=='resolved' and not value['answer']:raise ValueError('A resolved question needs an answer.')
            values.append(value)
        out[group]=values
    return out

def state_hash(obj):return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def store_pending(pid,item):
    now=datetime.now(timezone.utc).timestamp()
    for key,value in list(PENDING.items()):
        if value.get('expires',0)<now:PENDING.pop(key,None)
    if len(PENDING)>=MAX_PENDING:
        oldest=min(PENDING,key=lambda key:PENDING[key].get('expires',0))
        PENDING.pop(oldest,None)
    PENDING[pid]=item

def completion(obj):
    checks=obj['checks'];open_questions=[q for q in obj['questions'] if q['status']=='open']
    if not obj['objective'] or not checks or any(c['status']!='passed' or not c['evidence'] for c in checks) or open_questions:return 'in_progress'
    return 'complete'

def read_workspace(db=None):
    owned=db or database();row=owned.execute('SELECT * FROM workspace ORDER BY rowid LIMIT 1').fetchone()
    if not row:
        approved=default_snapshot();draft=sample_draft();now=datetime.now(timezone.utc).isoformat();wid=secrets.token_hex(8)
        owned.execute('INSERT INTO workspace VALUES (?,?,?,?,?)',(wid,json.dumps(draft,ensure_ascii=False),json.dumps(approved,ensure_ascii=False),now,now));owned.commit();row=owned.execute('SELECT * FROM workspace WHERE id=?',(wid,)).fetchone()
    result={'id':row['id'],'current':json.loads(row['current_json']),'approved':json.loads(row['approved_json']),'approved_at':row['approved_at'],'updated_at':row['updated_at']}
    if db is None:owned.close()
    return result

def compare_snapshots(old,new):
    changes=[]
    for field in ('objective',):
        if old.get(field)!=new.get(field):changes.append({'area':field,'state':'changed','before':old.get(field,''),'after':new.get(field,'')})
    for group in LISTS:
        left={x['id']:x for x in old.get(group,[])};right={x['id']:x for x in new.get(group,[])}
        for ident in sorted(set(left)|set(right)):
            if ident not in left:changes.append({'area':group,'state':'added','id':ident,'after':right[ident]})
            elif ident not in right:changes.append({'area':group,'state':'removed','id':ident,'before':left[ident]})
            elif left[ident]!=right[ident]:changes.append({'area':group,'state':'changed','id':ident,'before':left[ident],'after':right[ident]})
    return changes

HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b0f12"><title>Context Handoff</title><link rel="stylesheet" href="/style.css"></head><body><header><a class="brand" href="/" aria-label="Context Handoff home"><span class="mark">C</span><span>Context<br>Handoff</span></a><nav><button id="workspaceTab" class="nav active">Task state</button><button id="settingsTab" class="nav">Settings</button></nav><span class="local"><i></i> Saved on this device</span></header><main><section id="workspace"><div class="topline"><div><h1>Context Handoff</h1><p class="intro">Capture a task’s decisions, changed files, checks, and open questions. Export a reviewed handoff for the next session.</p></div><span id="completion" class="state-pill">IN PROGRESS</span></div><div class="toolbar"><button id="saveBtn" class="primary">Save draft</button><button id="importBtn" class="secondary">Compare resumed JSON</button><button id="exportBtn" class="secondary">Review export</button></div><section class="objective"><label for="objective">Task objective</label><textarea id="objective" rows="2" maxlength="4000"></textarea></section><section class="columns"><div class="column"><h2>Decisions</h2><div id="decisions"></div><button class="add" data-add="decisions">+ Add decision</button><h2>Changed files</h2><div id="files"></div><button class="add" data-add="files">+ Add changed file</button><h2>Failed approaches</h2><div id="failed_approaches"></div><button class="add" data-add="failed_approaches">+ Add failed approach</button></div><div class="column"><h2>Checks</h2><div id="checks"></div><button class="add" data-add="checks">+ Add check</button><h2>Open questions</h2><div id="questions"></div><button class="add" data-add="questions">+ Add question</button><section class="resume"><div class="resume-head"><div><span class="label">LAST APPROVED</span><h2>Resume comparison</h2></div><span id="approvedDate" class="small-muted"></span></div><div id="diff"></div></section></div></section><p class="note">Manual JSON only. No provider hook can open a new session for you. Transcript and credential fields are not part of this format.</p></section><section id="settings" hidden><button id="backBtn" class="text-button">← Back to task</button><h1>Settings</h1><p class="intro">This version stores task state locally and uses a manual JSON handoff.</p><div class="settings-block"><h2>Data and provider limits</h2><p>There are no provider connections, automatic session starts, transcripts, or credential fields. Export creates a file for you to carry to another session.</p><p>Checks count as passed only when they include evidence. The completion label is calculated from the current checks and open questions.</p><button id="wipeBtn" class="danger">Reset local handoff</button></div></section></main><dialog id="resumeDialog"><form method="dialog"><div class="dialog-head"><h2>Compare resumed handoff</h2><button class="close" value="cancel" aria-label="Close">×</button></div><label for="resumeFile">Choose a Context Handoff JSON file</label><input id="resumeFile" type="file" accept=".json,application/json"><button id="resumePreviewBtn" type="button" class="secondary">Compare</button><div id="resumePreview" class="preview"></div><div class="dialog-actions"><button class="secondary" value="cancel">Cancel</button><button id="acceptResume" class="primary" type="button" disabled>Use this handoff</button></div></form></dialog><dialog id="exportDialog"><form method="dialog"><div class="dialog-head"><h2>Review handoff export</h2><button class="close" value="cancel" aria-label="Close">×</button></div><p class="small-muted">Select sections to redact. Inspect the full JSON before approval.</p><fieldset><legend>Redact these sections</legend><label><input type="checkbox" value="objective"> Objective</label><label><input type="checkbox" value="decisions"> Decisions</label><label><input type="checkbox" value="files"> File paths and changes</label><label><input type="checkbox" value="checks"> Checks</label><label><input type="checkbox" value="questions"> Question text (open state retained)</label><label><input type="checkbox" value="failed_approaches"> Failed approaches</label></fieldset><button id="buildExport" type="button" class="secondary">Build preview</button><textarea id="exportText" rows="14" readonly aria-label="Export preview JSON"></textarea><div class="dialog-actions"><button class="secondary" value="cancel">Cancel</button><button id="approveExport" class="primary" type="button" disabled>Approve and download</button></div></form></dialog><div id="toast" role="status" aria-live="polite"></div><script src="/app.js" defer></script></body></html>'''

CSS=r''':root{color-scheme:dark;--bg:#0b0f12;--surface:#11181d;--surface2:#172126;--line:#2a373a;--text:#f1f5f4;--muted:#a6b3b0;--dim:#7c8c88;--teal:#81d4c7;--teal-ink:#0b2724;--orange:#f6a16f;--font:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;--display:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse 65% 38% at 80% -14%,#1d3432 0,transparent 75%),var(--bg);color:var(--text);font:16px/1.5 var(--font);min-height:100vh}button,input,textarea,select{font:inherit}button{cursor:pointer}header{height:72px;border-bottom:1px solid #ffffff12;display:flex;align-items:center;padding:0 max(24px,calc((100vw - 1380px)/2));gap:42px}.brand{display:flex;align-items:center;gap:10px;color:var(--text);font:700 13px/1.05 var(--display);text-decoration:none;letter-spacing:-.02em}.mark{width:32px;height:32px;border:1px solid #35867b;border-radius:10px;display:grid;place-items:center;color:#a1e8dc;font-weight:800}.nav{border:0;background:none;color:var(--muted);padding:10px 14px;border-radius:8px}.nav.active,.nav:hover{background:#ffffff0c;color:white}.local{margin-left:auto;color:var(--muted);font-size:12px;display:flex;gap:8px;align-items:center}.local i{width:7px;height:7px;background:#8ee2ad;border-radius:50%}main{max-width:1380px;margin:auto;padding:44px 32px}.topline{display:flex;justify-content:space-between;align-items:end;gap:18px}h1{font:750 clamp(36px,4.2vw,56px)/1.05 var(--display);letter-spacing:-.05em;margin:0 0 12px}h2{font:650 18px var(--display);letter-spacing:-.025em;margin:28px 0 12px}.intro{max-width:68ch;color:#b1bfbb;margin:0;font-size:15px}.state-pill{border:1px solid #47665f;color:#b5e4d8;padding:7px 11px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:.08em;white-space:nowrap}.state-pill.complete{border-color:#62ab84;color:#b7f0cd}.toolbar{display:flex;gap:9px;flex-wrap:wrap;border-block:1px solid #ffffff12;margin:27px 0 24px;padding:13px 0}.primary,.secondary,.danger{min-height:41px;padding:0 14px;border-radius:8px;border:1px solid transparent;font-size:13px;font-weight:700;transition:transform .2s,background .2s}.primary{background:var(--teal);color:var(--teal-ink)}.primary:hover{background:#a4e9dd;transform:translateY(-1px)}.secondary{background:#202a2f;border-color:#39474b;color:#ebf0ee}.danger{background:#351e19;border-color:#63382e;color:#ffd0bf}.objective{padding:18px 20px;background:#11191d;border:1px solid #2a393a;border-radius:12px}.objective label,.field label,dialog label{display:block;color:#b8c3c0;font-size:12px;font-weight:650;margin-bottom:7px}.objective textarea,.field input,.field textarea,.field select,dialog textarea,dialog input[type=file]{width:100%;min-width:0;background:#0a1013;color:#f1f5f4;border:1px solid #38464a;border-radius:7px;padding:9px 10px;resize:vertical}.columns{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:30px;margin-top:8px}.column{min-width:0}.column h2{margin-top:25px}.row-card{padding:13px 14px;margin-bottom:9px;background:#121a1e;border:1px solid #2b373b;border-radius:10px;animation:slide .32s cubic-bezier(.16,1,.3,1) both}.row-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px}.row-card .field{min-width:0;margin-top:9px}.row-card .field:first-child{margin-top:0}.row-card input,.row-card textarea,.row-card select{font-size:12px}.row-card textarea{min-height:60px}.row-actions{display:flex;justify-content:flex-end;margin-top:7px}.row-actions button,.add{background:transparent;border:0;color:#aee4da;font-size:12px;text-decoration:underline;text-underline-offset:3px;padding:5px}.add{margin:0 0 4px}.status.pending,.status.running,.status.failed,.status.skipped{color:#f3c795}.status.passed{color:#9fe5bb}.resume{margin-top:32px;padding-top:10px;border-top:1px solid #ffffff14}.resume-head{display:flex;justify-content:space-between;align-items:end}.resume-head h2{margin-top:6px}.label{color:#92b5ae;font-size:9px;letter-spacing:.15em;font-weight:700}.diff-row{padding:9px 10px;margin-bottom:7px;background:#131c20;border-radius:7px;color:#c7d1ce;font-size:12px;overflow-wrap:anywhere}.diff-row.added{border:1px solid #325d49}.diff-row.removed{border:1px solid #694038}.diff-row.changed{border:1px solid #61513a}.note{border-top:1px solid #ffffff12;padding-top:17px;margin-top:31px;color:#9aa8a5;font-size:12px}.settings-block{margin-top:26px;border-block:1px solid var(--line);padding:14px 0}.settings-block p{max-width:70ch;color:#aebbb8;font-size:14px}.text-button{background:none;border:0;color:#b9e3dc;text-decoration:underline;text-underline-offset:3px}dialog{width:min(760px,calc(100vw - 22px));max-height:90vh;overflow:auto;background:#141c20;color:white;border:1px solid #3c4b4e;border-radius:14px;padding:24px;box-shadow:0 24px 85px #000b}dialog::backdrop{background:#04090acc;backdrop-filter:blur(4px)}.dialog-head{display:flex;justify-content:space-between;align-items:center}.dialog-head h2{margin:0;font-size:21px}.close{border:0;background:none;color:#e1e8e6;font-size:24px}dialog form>label{margin:18px 0 7px}.preview{max-height:280px;overflow:auto;margin:14px 0}.preview-row{padding:10px;border-bottom:1px solid #ffffff12;font-size:12px;white-space:pre-wrap}.dialog-actions{display:flex;justify-content:flex-end;gap:9px;margin-top:16px}dialog fieldset{border:1px solid #354447;border-radius:8px;margin:16px 0;padding:12px;display:flex;gap:13px;flex-wrap:wrap}dialog legend{font-size:12px;color:#bbc6c3}dialog fieldset label{display:flex;gap:6px;align-items:center;margin:0;font-size:11px}dialog input[type=checkbox]{accent-color:#81d4c7}#toast{position:fixed;bottom:22px;left:50%;transform:translate(-50%,10px);opacity:0;background:#eaf8f1;color:#123025;padding:10px 16px;border-radius:8px;transition:.2s;pointer-events:none}#toast.show{opacity:1;transform:translate(-50%,0)}:focus-visible{outline:3px solid #9de3d7!important;outline-offset:3px}::selection{background:#81d4c7;color:#0b2724}*{scrollbar-width:thin;scrollbar-color:#465957 #11181d}@keyframes slide{from{opacity:.55;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}@media(max-width:800px){main{padding:35px 22px}.columns{gap:18px}}@media(max-width:560px){header{height:62px;padding:0 14px;gap:8px}.brand span:last-child{display:none}.nav{padding:8px;font-size:12px}.local{font-size:0}.local i{width:8px;height:8px}main{padding:28px 14px}.topline{align-items:start}.topline h1{font-size:clamp(32px,9.6vw,40px)}.columns{grid-template-columns:1fr;gap:0}.row-grid{grid-template-columns:1fr}.toolbar button{flex:1}.objective{padding:15px}.resume-head{align-items:start;gap:8px}.note{font-size:11px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}'''

APPJS=r'''const $=s=>document.querySelector(s);let current=null,approved=null,exportPreview=null,resumePreview=null;async function api(path,method='GET',body){const o={method,headers:{'X-Local-Action':TOKEN_PLACEHOLDER}};if(body!==undefined){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}const r=await fetch(path,o);if(!r.ok){const d=await r.json();throw Error(d.error||'Request failed.')}return r.status===204?null:r.json()}function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function toast(v){const t=$('#toast');t.textContent=v;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2300)}function row(group,item){const fields=group==='decisions'?`<div class="field"><label>Decision</label><textarea data-field="text">${esc(item.text)}</textarea></div><div class="field"><label>Evidence</label><textarea data-field="evidence">${esc(item.evidence)}</textarea></div>`:group==='files'?`<div class="field"><label>File path</label><input data-field="path" value="${esc(item.path)}"></div><div class="field"><label>Change</label><textarea data-field="change">${esc(item.change)}</textarea></div>`:group==='checks'?`<div class="field"><label>Check</label><input data-field="text" value="${esc(item.text)}"></div><div class="row-grid"><div class="field"><label>State</label><select data-field="status"><option ${item.status==='pending'?'selected':''}>pending</option><option ${item.status==='running'?'selected':''}>running</option><option ${item.status==='passed'?'selected':''}>passed</option><option ${item.status==='failed'?'selected':''}>failed</option><option ${item.status==='skipped'?'selected':''}>skipped</option></select></div><div class="field"><label>Evidence</label><textarea data-field="evidence">${esc(item.evidence)}</textarea></div></div>`:group==='questions'?`<div class="field"><label>Question</label><textarea data-field="question">${esc(item.question)}</textarea></div><div class="row-grid"><div class="field"><label>State</label><select data-field="status"><option ${item.status==='open'?'selected':''}>open</option><option ${item.status==='resolved'?'selected':''}>resolved</option></select></div><div class="field"><label>Answer</label><textarea data-field="answer">${esc(item.answer)}</textarea></div></div>`:`<div class="field"><label>Approach that failed</label><textarea data-field="summary">${esc(item.summary)}</textarea></div><div class="field"><label>Why it failed</label><textarea data-field="reason">${esc(item.reason)}</textarea></div>`;return `<article class="row-card" data-group="${group}" data-id="${esc(item.id)}">${fields}<div class="row-actions"><button type="button" data-remove="1">Remove</button></div></article>`}function render(){if(!current)return;$('#objective').value=current.objective;for(const group of ['decisions','files','checks','questions','failed_approaches'])$('#'+group).innerHTML=current[group].map(x=>row(group,x)).join('')||'<p class="small-muted">None recorded.</p>';const complete=completion(current)==='complete';$('#completion').textContent=complete?'COMPLETE':'IN PROGRESS';$('#completion').classList.toggle('complete',complete);$('#approvedDate').textContent=approved?.approved_at?new Date(approved.approved_at).toLocaleString():'No approved handoff yet';const changes=approved?.current?diff(approved.current,current):[];$('#diff').innerHTML=changes.map(c=>`<div class="diff-row ${c.state}"><strong>${esc(c.area)} · ${esc(c.state)}</strong>${c.detail?`<br>${esc(c.detail)}`:''}</div>`).join('')||'<p class="small-muted">No changes since the last approved handoff.</p>'}function completion(s){return s.objective&&s.checks.length&&s.checks.every(c=>c.status==='passed'&&c.evidence)&&!s.questions.some(q=>q.status==='open')?'complete':'in_progress'}function diff(a,b){const out=[];if(a.objective!==b.objective)out.push({area:'objective',state:'changed',detail:`${a.objective} → ${b.objective}`});for(const k of ['decisions','files','checks','questions','failed_approaches']){const old=new Map(a[k].map(x=>[x.id,x])),now=new Map(b[k].map(x=>[x.id,x]));for(const id of new Set([...old.keys(),...now.keys()])){if(!old.has(id))out.push({area:k,state:'added',detail:JSON.stringify(now.get(id))});else if(!now.has(id))out.push({area:k,state:'removed',detail:JSON.stringify(old.get(id))});else if(JSON.stringify(old.get(id))!==JSON.stringify(now.get(id)))out.push({area:k,state:'changed',detail:JSON.stringify(now.get(id))})}}return out}function build(){const out={schema_version:'1',objective:$('#objective').value.trim()};for(const group of ['decisions','files','checks','questions','failed_approaches'])out[group]=[...document.querySelectorAll(`#${group} .row-card`)].map(card=>{const x={id:card.dataset.id};card.querySelectorAll('[data-field]').forEach(el=>x[el.dataset.field]=el.value.trim());return x});return out}async function load(){const d=await api('/api/state');current=d.current;approved=d.approved;render()}$('#workspace').addEventListener('click',e=>{const add=e.target.closest('[data-add]');if(add){const group=add.dataset.add;const seed={id:crypto.randomUUID()};const defaults={decisions:{text:'',evidence:''},files:{path:'',change:''},checks:{text:'',status:'pending',evidence:''},questions:{question:'',status:'open',answer:''},failed_approaches:{summary:'',reason:''}};const holder=document.createElement('div');holder.innerHTML=row(group,{...defaults[group],...seed});$('#'+group).append(holder.firstElementChild)}const remove=e.target.closest('[data-remove]');if(remove)remove.closest('.row-card').remove()});$('#saveBtn').onclick=async()=>{try{const d=await api('/api/state','PUT',build());current=d.current;render();toast(d.completion==='complete'?'Draft saved. All checks pass and no questions remain.':'Draft saved. Work remains in progress.')}catch(e){toast(e.message)}};$('#importBtn').onclick=()=>{$('#resumeDialog').showModal();resumePreview=null;$('#resumePreview').textContent='';$('#acceptResume').disabled=true};$('#resumePreviewBtn').onclick=async()=>{const f=$('#resumeFile').files[0];if(!f){toast('Choose a JSON handoff first.');return}try{const input=JSON.parse(await f.text());resumePreview=await api('/api/resume-preview','POST',{snapshot:input});$('#resumePreview').innerHTML=resumePreview.changes.map(c=>`<div class="preview-row"><strong>${esc(c.area)} · ${esc(c.state)}</strong><br>${esc(c.id||'')} ${esc(JSON.stringify(c.after||c.before||''))}</div>`).join('')||'<p>No changes against the approved snapshot.</p>';$('#acceptResume').disabled=false}catch(e){resumePreview=null;$('#resumePreview').textContent=e.message;$('#acceptResume').disabled=true}};$('#acceptResume').onclick=async()=>{if(!resumePreview)return;try{const d=await api('/api/resume','POST',{preview_id:resumePreview.preview_id});current=d.current;render();$('#resumeDialog').close();toast('Resumed handoff loaded. Review the remaining work.')}catch(e){toast(e.message)}};$('#exportBtn').onclick=()=>{$('#exportDialog').showModal();exportPreview=null;$('#exportText').value='';$('#approveExport').disabled=true};$('#buildExport').onclick=async()=>{try{const redact=[...document.querySelectorAll('fieldset input:checked')].map(x=>x.value);exportPreview=await api('/api/export-preview','POST',{redact});$('#exportText').value=JSON.stringify(exportPreview.snapshot,null,2);$('#approveExport').disabled=false}catch(e){toast(e.message)}};$('#approveExport').onclick=async()=>{if(!exportPreview)return;try{const out=await api('/api/approve-export','POST',{preview_id:exportPreview.preview_id});const blob=new Blob([JSON.stringify(out.snapshot,null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='context-handoff.json';a.click();URL.revokeObjectURL(a.href);$('#exportDialog').close();await load();toast('Approved handoff downloaded.')}catch(e){toast(e.message)}};function settings(on){$('#workspace').hidden=on;$('#settings').hidden=!on;$('#settingsTab').classList.toggle('active',on);$('#workspaceTab').classList.toggle('active',!on)}$('#settingsTab').onclick=()=>settings(true);$('#workspaceTab').onclick=()=>settings(false);$('#backBtn').onclick=()=>settings(false);$('#wipeBtn').onclick=async()=>{if(!confirm('Reset current and approved local handoff data?'))return;await api('/api/reset','POST',{});await load();settings(false);toast('Local handoff reset. The synthetic sample is ready.')};load().catch(e=>toast(e.message));'''

# Keep the review baseline in the same shape render() consumes and block export of unsaved edits.
CSS=CSS.replace('@media(max-width:560px){','@media(max-width:560px){.topline{display:block}.topline>div{width:100%}.intro{max-width:none}.state-pill{display:inline-flex;margin-top:10px}')
APPJS=APPJS.replace('current=d.current;approved=d.approved;render()','current=d.current;approved={current:d.approved,approved_at:d.approved_at};window.draftDirty=false;render()')
APPJS=APPJS.replace("current=d.current;render();toast(d.completion", "current=d.current;window.draftDirty=false;render();$('#exportBtn').disabled=false;$('#importBtn').disabled=false;$('#draftNotice').hidden=true;toast(d.completion")
APPJS=APPJS.replace("$('#exportBtn').onclick=()=>{$('#exportDialog').showModal();", "$('#exportBtn').onclick=()=>{if(window.draftDirty){toast('Save the draft before reviewing an export.');return}$('#exportDialog').showModal();")
APPJS=APPJS.replace("$('#importBtn').onclick=()=>{$('#resumeDialog').showModal();", "$('#importBtn').onclick=()=>{if(window.draftDirty){toast('Save the draft before comparing a resumed handoff.');return}$('#resumeDialog').showModal();")
APPJS += r'''row=(function(original){return function(group,item){const box=document.createElement('div');box.innerHTML=original(group,item);box.querySelectorAll('.field').forEach(field=>{const input=field.querySelector('[data-field]'),label=field.querySelector('label');if(input&&label){input.id=`${group}-${item.id}-${input.dataset.field}`;label.htmlFor=input.id}});return box.firstElementChild.outerHTML}})(row);window.draftDirty=false;const notice=document.createElement('p');notice.id='draftNotice';notice.className='small-muted';notice.textContent='Save the draft before reviewing an export or comparing a resumed handoff.';notice.hidden=true;$('.toolbar').after(notice);function markDraftDirty(){window.draftDirty=true;const snapshot=build();$('#completion').textContent=completion(snapshot)==='complete'?'COMPLETE':'IN PROGRESS';$('#completion').classList.toggle('complete',completion(snapshot)==='complete');$('#exportBtn').disabled=true;$('#importBtn').disabled=true;notice.hidden=false}$('#workspace').addEventListener('input',e=>{if(e.target.matches('#objective,[data-field]'))markDraftDirty()});$('#workspace').addEventListener('change',e=>{if(e.target.matches('#objective,[data-field]'))markDraftDirty()});$('#workspace').addEventListener('click',e=>{if(e.target.closest('[data-add],[data-remove]'))markDraftDirty()});$('#saveBtn').addEventListener('click',()=>{setTimeout(()=>{if(!window.draftDirty){$('#exportBtn').disabled=false;$('#importBtn').disabled=false;notice.hidden=true}},0)});document.querySelectorAll('#exportDialog fieldset input').forEach(input=>input.addEventListener('change',()=>{exportPreview=null;$('#approveExport').disabled=true;$('#exportText').value='';$('#exportText').placeholder='Redaction choices changed. Build the preview again.'}));'''

class Handler(BaseHTTPRequestHandler):
    server_version='ContextHandoff/1.0'
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
    def log_message(self,fmt,*args):super().log_message(fmt,*args)
    def safe(self):return self.headers.get('Host','') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
    def send(self,status,data=b'',ctype='application/json; charset=utf-8'):
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");self.send_header('Referrer-Policy','no-referrer');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def json(self,status,obj):self.send(status,json.dumps(obj,ensure_ascii=False).encode())
    def read_json(self):
        n=int(self.headers.get('Content-Length','0'))
        if not 1<=n<=MAX_BODY:raise ValueError('Request is empty or too large.')
        obj=json.loads(self.rfile.read(n))
        if not isinstance(obj,dict):raise ValueError('Expected a JSON object.')
        return obj
    def authorized(self):
        origin=self.headers.get('Origin');allowed=(f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}')
        return self.safe() and self.headers.get('X-Local-Action')==TOKEN and (origin is None or origin in allowed)
    def do_GET(self):
        if not self.safe():return self.send(403,b'Forbidden','text/plain')
        path=urlparse(self.path).path
        if path=='/':
            page=HTML.replace('<div class="toolbar">','<p class="small-muted">Fictional starter task: its changed file, pending check, and open question demonstrate the handoff review.</p><div class="toolbar">')
            if 'desktop=1' in self.path:page=page.replace('</head>','<link rel="stylesheet" href="/desktop.css"></head>')
            return self.send(200,page.encode(),'text/html; charset=utf-8')
        if path=='/desktop.css':return self.send(200,(ROOT/'desktop.css').read_bytes(),'text/css; charset=utf-8')
        if path=='/style.css':return self.send(200,CSS.encode(),'text/css; charset=utf-8')
        if path=='/app.js':return self.send(200,APPJS.replace('TOKEN_PLACEHOLDER',json.dumps(TOKEN)).encode(),'application/javascript; charset=utf-8')
        if path=='/api/state':return self.json(200,read_workspace())
        return self.send(404,b'Not found','text/plain')
    def do_PUT(self):
        if not self.authorized():return self.send(403,b'Forbidden','text/plain')
        try:
            if urlparse(self.path).path!='/api/state':return self.send(404,b'Not found','text/plain')
            snapshot=validate_snapshot(self.read_json());db=database();ws=read_workspace(db);now=datetime.now(timezone.utc).isoformat();db.execute('UPDATE workspace SET current_json=?,updated_at=? WHERE id=?',(json.dumps(snapshot,ensure_ascii=False),now,ws['id']));db.commit();db.close();return self.json(200,{'current':snapshot,'completion':completion(snapshot)})
        except (ValueError,json.JSONDecodeError,sqlite3.Error) as e:return self.json(400,{'error':str(e)})
    def do_POST(self):
        if not self.authorized():return self.send(403,b'Forbidden','text/plain')
        try:
            path=urlparse(self.path).path;obj=self.read_json()
            if path=='/api/resume-preview':
                snapshot=validate_snapshot(obj.get('snapshot'));ws=read_workspace();base=state_hash(ws['approved']);draft_base=state_hash(ws['current']);changes=compare_snapshots(ws['approved'],snapshot);pid=secrets.token_urlsafe(18);store_pending(pid,{'kind':'resume','snapshot':snapshot,'approved':base,'current':draft_base,'expires':datetime.now(timezone.utc).timestamp()+600});return self.json(200,{'preview_id':pid,'changes':changes,'completion':completion(snapshot)})
            if path=='/api/resume':
                pid=obj.get('preview_id');item=PENDING.pop(pid,None) if isinstance(pid,str) else None
                if not item or item['kind']!='resume' or item['expires']<datetime.now(timezone.utc).timestamp():raise ValueError('Resume preview expired. Compare the file again.')
                db=database();ws=read_workspace(db)
                if state_hash(ws['approved'])!=item['approved'] or state_hash(ws['current'])!=item['current']:db.close();raise ValueError('Task state changed. Compare the file again.')
                db.execute('UPDATE workspace SET current_json=?,updated_at=? WHERE id=?',(json.dumps(item['snapshot'],ensure_ascii=False),datetime.now(timezone.utc).isoformat(),ws['id']));db.commit();db.close();return self.json(200,{'current':item['snapshot'],'completion':completion(item['snapshot'])})
            if path=='/api/export-preview':
                redact=obj.get('redact',[])
                redaction_fields={'objective',*LISTS}
                if not isinstance(redact,list) or any(not isinstance(x,str) for x in redact) or set(redact)-redaction_fields:raise ValueError('Redaction list has an unsupported section.')
                ws=read_workspace();snapshot=dict(ws['current'])
                for key in redact:
                    if key=='questions':snapshot[key]=[{'id':q['id'],'question':'[REDACTED]','status':q['status'],'answer':'[REDACTED]' if q['status']=='resolved' else ''} for q in snapshot[key]]
                    else:snapshot[key]=[] if key in LISTS else ''
                pid=secrets.token_urlsafe(18);store_pending(pid,{'kind':'export','snapshot':snapshot,'current':state_hash(ws['current']),'expires':datetime.now(timezone.utc).timestamp()+600});return self.json(200,{'preview_id':pid,'snapshot':snapshot})
            if path=='/api/approve-export':
                pid=obj.get('preview_id');item=PENDING.pop(pid,None) if isinstance(pid,str) else None
                if not item or item['kind']!='export' or item['expires']<datetime.now(timezone.utc).timestamp():raise ValueError('Export preview expired. Build it again.')
                db=database();ws=read_workspace(db)
                if state_hash(ws['current'])!=item['current']:db.close();raise ValueError('Draft changed after preview. Review a new export.')
                db.execute('UPDATE workspace SET approved_json=?,approved_at=?,updated_at=? WHERE id=?',(json.dumps(item['snapshot'],ensure_ascii=False),datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat(),ws['id']));db.commit();db.close();return self.json(200,{'snapshot':item['snapshot']})
            if path=='/api/reset':
                ws=read_workspace();approved=default_snapshot();draft=sample_draft();db=database();now=datetime.now(timezone.utc).isoformat();db.execute('UPDATE workspace SET current_json=?,approved_json=?,approved_at=?,updated_at=? WHERE id=?',(json.dumps(draft),json.dumps(approved),now,now,ws['id']));db.commit();db.close();return self.send(204)
            return self.send(404,b'Not found','text/plain')
        except (ValueError,json.JSONDecodeError,sqlite3.Error) as e:return self.json(400,{'error':str(e)})

def make_server(port=0):
    return _make_desktop_server(Handler, port=port)

def run(port=8347):
    read_workspace();server=make_server(port=port);print(f'Context Handoff listening at http://127.0.0.1:{server.server_port}',flush=True);server.serve_forever()
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8347);run(p.parse_args().port)
