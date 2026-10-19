import pytest
from fastapi.testclient import TestClient
from app.main import app
@pytest.fixture
def client(tmp_path,monkeypatch):
 monkeypatch.setenv('REVIEW_DB',str(tmp_path/'review.db'));monkeypatch.setenv('HF_TOKEN','');monkeypatch.setenv('API_KEY','')
 return TestClient(app)
def test_policy_draft_approval_idempotency_and_isolation(client):
 ticket={'ticket_id':'T-REVIEW-1','subject':'Refund processing time','body':'How long does a refund take after my return arrives at the warehouse?'}
 r=client.post('/api/review/tickets',json=ticket);assert r.status_code==200;d=r.json()['ticket'];assert d['sources'];assert d['status']=='needs-review';assert d['outbox'] is None;assert not d['blocked']
 assert client.post('/api/review/tickets',json=ticket).json()['duplicate']
 decision={'action':'approve','reply':d['draft'],'reviewer':'Tester','version':1}
 other=TestClient(app);assert other.get('/api/review/tickets').json()==[];assert other.post('/api/review/tickets/'+d['id']+'/decision',json=decision).status_code==404
 approved=client.post('/api/review/tickets/'+d['id']+'/decision',json=decision).json();assert approved['status']=='approved';assert 'not sent' in approved['outbox']['delivery'];assert client.post('/api/review/tickets/'+d['id']+'/decision',json=decision).status_code==409
 assert 'approved' in client.get('/api/review/export.json').text
def test_hard_escalation_cannot_be_approved(client):
 d=client.post('/api/review/tickets',json={'subject':'Hacked account fraud','body':'My account was stolen.'}).json()['ticket'];assert d['blocked']
 decision={'action':'approve','reply':'Unsafe promise','reviewer':'Tester','version':1}
 assert client.post('/api/review/tickets/'+d['id']+'/decision',json=decision).status_code==422
 decision['action']='escalate';assert client.post('/api/review/tickets/'+d['id']+'/decision',json=decision).json()['status']=='escalated'
