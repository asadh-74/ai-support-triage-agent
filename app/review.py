"""Human review and local outbox. No email or helpdesk delivery is performed."""
import json, os, re, secrets, sqlite3, threading, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from .schemas import TicketIn
from .agent import handle_ticket, redact_pii, guardrail_reason
from .tools import get_kb
router=APIRouter();lock=threading.RLock();STATIC=Path(__file__).parent/'static'
def now():return datetime.now(timezone.utc).isoformat()
@contextmanager
def db():
 c=sqlite3.connect(os.getenv('REVIEW_DB','/tmp/support-review.sqlite3'),timeout=10)
 try:
  c.execute('CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY,sid TEXT,ticket_id TEXT,created REAL,data TEXT,UNIQUE(sid,ticket_id))');c.execute('DELETE FROM reviews WHERE created<?',(time.time()-86400,));yield c;c.commit()
 finally:c.close()
def session(req,res):
 sid=req.cookies.get('support_session','')
 if not re.fullmatch('[a-f0-9]{48}',sid):sid=secrets.token_hex(24);res.set_cookie('support_session',sid,httponly=True,samesite='lax',secure=bool(os.getenv('RENDER')),max_age=86400)
 return sid
def find(c,id,sid):
 row=c.execute('SELECT data FROM reviews WHERE id=? AND sid=?',(id,sid)).fetchone()
 if not row:raise HTTPException(404,'Ticket not found in this session')
 return json.loads(row[0])
@router.get('/review',include_in_schema=False)
def page():return FileResponse(STATIC/'review.html')
@router.get('/review/assets/{name}',include_in_schema=False)
def asset(name:str):
 if name not in ('workspace.css','review.js'):raise HTTPException(404)
 return FileResponse(STATIC/name,media_type='text/css' if name.endswith('css') else 'application/javascript')
@router.get('/api/review/tickets')
def listing(req:Request,res:Response):
 sid=session(req,res)
 with db() as c:return [json.loads(x[0]) for x in c.execute('SELECT data FROM reviews WHERE sid=? ORDER BY created DESC LIMIT 50',(sid,))]
@router.post('/api/review/tickets')
def create(ticket:TicketIn,req:Request,res:Response):
 sid=session(req,res);safe_subject=redact_pii(ticket.subject).strip();safe_body=redact_pii(ticket.body).strip()
 if not safe_subject or not safe_body:raise HTTPException(422,'Subject and body cannot be blank')
 with lock,db() as c:
  existing=c.execute('SELECT data FROM reviews WHERE sid=? AND ticket_id=?',(sid,ticket.ticket_id)).fetchone()
  if existing:
   old=json.loads(existing[0])
   if old['subject']!=safe_subject or old['body']!=safe_body:raise HTTPException(409,'Ticket ID was already used for a different request')
   return {'ticket':old,'duplicate':True}
  if c.execute('SELECT count(*) FROM reviews WHERE sid=?',(sid,)).fetchone()[0]>=50 or c.execute('SELECT count(*) FROM reviews').fetchone()[0]>=1000:raise HTTPException(429,'Demo ticket capacity reached')
 from .main import _rate_limit
 _rate_limit(req)
 # Evidence retrieval always available without an AI key; do not send raw PII to the model.
 hits=[h for h in get_kb()._keyword_search(safe_subject+' '+safe_body,3) if h['score']>=0.5]
 blocked=guardrail_reason(safe_subject+' '+safe_body)
 if blocked or not hits:
  draft='Thank you for contacting support. Your request requires a specialist review. A team member must review it before any response is sent.';mode='policy-escalation';reason=blocked or 'No sufficiently relevant policy found';category='specialist';confidence=0
 else:
  result=handle_ticket(TicketIn(ticket_id=ticket.ticket_id,subject=safe_subject,body=safe_body))
  if result.action=='auto_reply':draft=result.reply;mode=result.mode;reason=result.reason;confidence=result.confidence;category=result.category
  else:
   draft='Thank you for contacting support. Our policy states:\n\n'+hits[0]['text']+'\n\nPlease confirm the details of your request so our team can assist.';mode='policy-template (not an AI response)';reason='Draft prepared from retrieved policy for human review; AI did not provide an approved suggestion';category=result.category;confidence=hits[0]['score']
 d={'id':str(uuid.uuid4()),'ticketId':ticket.ticket_id,'subject':safe_subject,'body':safe_body,'draft':draft,'category':category,'mode':mode,'confidence':confidence,'reason':reason,'blocked':bool(blocked or not hits),'sources':[{'source':h['source'],'text':h['text'],'score':h['score']} for h in hits],'status':'needs-review','version':1,'created':now(),'outbox':None,'audit':[{'at':now(),'event':'received-policy-retrieved-draft-created'}]}
 with lock,db() as c:
  try:c.execute('INSERT INTO reviews VALUES(?,?,?,?,?)',(d['id'],sid,ticket.ticket_id,time.time(),json.dumps(d)))
  except sqlite3.IntegrityError:
   old=json.loads(c.execute('SELECT data FROM reviews WHERE sid=? AND ticket_id=?',(sid,ticket.ticket_id)).fetchone()[0])
   if old['subject']!=safe_subject or old['body']!=safe_body:raise HTTPException(409,'Ticket ID conflict')
   return {'ticket':old,'duplicate':True}
 return {'ticket':d,'duplicate':False}
class Decision(BaseModel):
 action:Literal['approve','reject','escalate'];reply:str=Field(max_length=4000);reviewer:str=Field(min_length=2,max_length=80);version:int
@router.post('/api/review/tickets/{id}/decision')
def decide(id:str,body:Decision,req:Request,res:Response):
 sid=session(req,res)
 if not body.reviewer.strip():raise HTTPException(422,'Enter a reviewer name')
 with lock,db() as c:
  d=find(c,id,sid)
  if d['version']!=body.version:raise HTTPException(409,'Version changed. Reload the ticket before deciding.')
  if d['status']!='needs-review':raise HTTPException(409,'Ticket already reviewed. No duplicate outbox entry was created.')
  if body.action=='approve':
   if d['blocked']:raise HTTPException(422,'Specialist or missing-policy tickets must be escalated, not approved in this workspace')
   if not body.reply.strip():raise HTTPException(422,'Approved reply cannot be empty')
   d['outbox']={'id':str(uuid.uuid4()),'reply':body.reply.strip(),'delivery':'local-only; not sent','approvedBy':body.reviewer.strip(),'at':now()};d['status']='approved'
  else:d['status']='rejected' if body.action=='reject' else 'escalated'
  d['draft']=body.reply.strip();d['version']+=1;d['audit'].append({'at':now(),'event':d['status'],'reviewer':body.reviewer.strip()});c.execute('UPDATE reviews SET data=? WHERE id=? AND sid=?',(json.dumps(d),id,sid))
 return d
@router.get('/api/review/export.json')
def export(req:Request,res:Response):
 sid=session(req,res)
 with db() as c:rows=[json.loads(x[0]) for x in c.execute('SELECT data FROM reviews WHERE sid=? ORDER BY created',(sid,))]
 return Response(json.dumps({'delivery':'Local demo outbox only; nothing was sent','tickets':rows},indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="support-review-audit.json"'})
