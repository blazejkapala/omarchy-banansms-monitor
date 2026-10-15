#!/usr/bin/env python3
"""Read-only SerwerSMS monitor. Only two fixed read endpoints are reachable."""
import ctypes
import json
import os
from pathlib import Path
import re
import resource
import secrets
import select
import stat
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault import Database, VaultError

STATE = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state'))) / 'banansms-monitor'
ZONE = ZoneInfo('Europe/Warsaw')
DEFAULTS = dict(timeout=900, interval=300, low_limit=100, failure_percent=10, pending_minutes=15,
                notify=True, show_limits=True, show_delivery=True, show_cost=True)
READ_ENDPOINTS = frozenset(('account/limits', 'messages/reports'))
STATUSES = frozenset(('delivered','undelivered','sent','unsent','pending','in_progress','saved'))
REASONS = frozenset(('message_expired','wrong_number','unsupported_number','message_rejected','operator_error',
                    'missed_call','limit_exhausted','lock_send','wrong_message','wrong_sender_name',
                    'number_is_blacklisted','sending_to_foreign_networks_is_locked','other_error'))

class Error(Exception):
    def __init__(self, message, locked=False):
        super().__init__(message); self.locked = locked

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def api(token, endpoint, params=None):
    if endpoint not in READ_ENDPOINTS:
        raise Error('Only monitoring endpoints are allowed.')
    req = Request('https://api2.serwersms.pl/' + endpoint + '.json',
                  data=urlencode(params or {}).encode(),
                  headers={'Authorization':'Bearer '+token, 'Accept':'application/json',
                           'Content-Type':'application/x-www-form-urlencoded', 'User-Agent':'BananSMS-Monitor/0.1'})
    try:
        with build_opener(NoRedirect()).open(req, timeout=20) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise Error('Report response exceeds the safety limit. No partial totals were saved.')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise Error('Unexpected API response.')
        if data.get('error') or data.get('success') is False:
            # Provider error text can contain phone numbers or credentials.
            code = str((data.get('error') or {}).get('code','')) if isinstance(data.get('error'),dict) else ''
            raise Error('SerwerSMS rejected the request' + (' (code '+code+').' if re.fullmatch(r'\d{1,8}',code) else '. Check API permissions.'))
        return data
    except HTTPError as e:
        raise Error('API authentication failed. Check your token.' if e.code in (401,403) else 'API returned HTTP '+str(e.code)+'.') from None
    except (URLError, TimeoutError, OSError):
        raise Error('Cannot reach the API. Check your connection and try again.') from None
    except (ValueError, TypeError):
        raise Error('Cannot read the API response.') from None

def items(data):
    value = data.get('items')
    if not isinstance(value, list) or any(not isinstance(row,dict) for row in value):
        raise Error('API did not return a valid item list.')
    return value

def limits_summary(data):
    out=[]
    for row in items(data):
        kind = str(row.get('type','')).lower()
        if not re.fullmatch(r'[a-z0-9_+-]{1,24}',kind):
            raise Error('Unexpected service type in limits response.')
        value = str(row.get('value',''))
        if value not in ('no_limit','not_available') and not re.fullmatch(r'\d{1,12}',value):
            raise Error('Unexpected service limit value.')
        out.append({'type':kind, 'value':value, 'remaining':int(value) if value.isdigit() else None})
    return out

def timestamp(value):
    try:
        dt=datetime.fromisoformat(str(value))
        return (dt.replace(tzinfo=ZONE) if dt.tzinfo is None else dt).timestamp()
    except (ValueError,TypeError,OverflowError):
        return None

def report_summary(data, now, pending_minutes):
    counts=Counter(); reasons=Counter(); seen=set(); cost=Decimal(0); cost_count=0; delays=[]; old_pending=0; parts=0
    for row in items(data):
        rid=str(row.get('id',''))
        if not rid or len(rid)>128:
            raise Error('Report missing a valid ID. Totals were not updated.')
        if rid in seen: continue
        seen.add(rid)
        status=row.get('status','unknown')
        if status not in STATUSES: status='unknown'
        counts[status]+=1
        if status in ('undelivered','unsent'):
            reason=row.get('reason','other_error')
            reasons[reason if reason in REASONS else 'other_error']+=1
        queued=timestamp(row.get('queued')); sent=timestamp(row.get('sent')); delivered=timestamp(row.get('delivered'))
        if status in ('sent','pending','in_progress') and queued is not None and now-queued > pending_minutes*60:
            old_pending+=1
        if status=='delivered' and sent is not None and delivered is not None and delivered>=sent:
            delays.append(delivered-sent)
        try:
            value=Decimal(str(row.get('cost','')))
            if value.is_finite() and value>=0:
                cost+=value; cost_count+=1
        except InvalidOperation: pass
        try: parts+=max(0,int(row.get('parts',0)))
        except (ValueError,TypeError): pass
    failed=counts['undelivered']+counts['unsent']; completed=counts['delivered']+failed
    return {'total':len(seen),'counts':dict(counts),'failed':failed,'completed':completed,
            'delivery_percent':round(100*counts['delivered']/completed,1) if completed else None,
            'failure_percent':round(100*failed/completed,1) if completed else None,
            'pending':sum(counts[k] for k in ('sent','pending','in_progress')), 'old_pending':old_pending,
            'reasons':[{'reason':key,'count':value} for key,value in reasons.most_common()],
            'cost':str(cost) if cost_count else None,'cost_count':cost_count,'parts':parts,
            'mean_delay':round(sum(delays)/len(delays),1) if delays else None,'delay_count':len(delays)}

def private_dir(path):
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid(): raise Error('Unsafe storage directory.')
    os.chmod(path,0o700)

def safe_file(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise Error('Private file must be owned by you with permissions 600; symlinks are not allowed.')

def sync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

class Monitor:
    def __init__(self, state=STATE):
        self.state=Path(state); self.path=self.state/'vault.db'; self.db=None; self.deadline=0
    def exists(self): return self.path.exists() or self.path.is_symlink()
    def close(self):
        if self.db: self.db.close()
        self.db=None; self.deadline=0
    def require(self):
        if not self.db or time.monotonic()>=self.deadline:
            self.close(); raise Error('Vault is locked. Enter your master password.',locked=True)
    def settings(self): return {**DEFAULTS, **self.db.get('settings',{})}
    def touch(self): self.deadline=time.monotonic()+self.settings()['timeout']
    def password(self, value):
        if not isinstance(value,str) or not 8<=len(value)<=128: raise Error('Choose a master password of 8–128 characters.')
    def attempts(self, failure=False, reset=False):
        path=self.state/'attempts.json'; data={}
        if path.exists() or path.is_symlink():
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd) as f:
                info=os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
                    raise Error('Unsafe password-attempt counter file.')
                raw=f.read(4097)
                if len(raw)>4096: raise Error('Invalid attempt counter file.')
                data=json.loads(raw)
        if not failure and not reset:
            if data.get('next',0)>time.time(): raise Error('Please wait before trying your password again.')
            return
        count=data.get('count',0)+1
        value={} if reset else {'count':count,'next':time.time()+min(30,2**min(count,5))}
        fd,tmp=tempfile.mkstemp(prefix='.attempts-',dir=self.state)
        try:
            with os.fdopen(fd,'w') as f: json.dump(value,f); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    def open_password(self, password):
        if not isinstance(password,str) or not 1<=len(password)<=128: raise Error('Enter your master password.')
        self.attempts(); safe_file(self.path)
        try: db=Database(self.path,password)
        except VaultError:
            self.attempts(failure=True); raise
        try:
            self.attempts(reset=True)
        except Exception:
            db.close(); raise
        return db
    def create(self, password):
        self.password(password)
        private_dir(self.state)
        if self.exists(): raise Error('A vault already exists. Unlock it instead.')
        fd,tmp=tempfile.mkstemp(prefix='.new-vault-',suffix='.db',dir=self.state); os.close(fd)
        db=None
        try:
            db=Database(tmp,password,create=True); db.put('settings',DEFAULTS); db.put('accounts',[]); db.close()
            db=Database(tmp,password)
            if db.get('settings',{})!=DEFAULTS or db.query('PRAGMA integrity_check')!=['ok'] or db.query('PRAGMA cipher_integrity_check'):
                raise Error('Vault verification failed.')
            db.close(); db=None
            os.link(tmp,self.path); sync_dir(self.state)
            self.db=Database(self.path,password); self.touch()
        finally:
            if db: db.close()
            os.unlink(tmp)
    def snapshot(self, refresh=False):
        self.require(); settings=self.settings(); output=[]; notifications=[]
        for account in self.db.get('accounts',[]):
            key='cache/'+account['id']; cache=self.db.get(key,{})
            now=time.time(); changed=False
            for section,endpoint,ttl in [('limits','account/limits',3600),('reports','messages/reports',settings['interval'])]:
                previous=cache.get(section,{})
                last_attempt=previous.get('attempt_at',0)
                if now-last_attempt < (30 if refresh else ttl): continue
                start=time.monotonic()
                try:
                    params={'show_type':'true'} if section=='limits' else {
                        'date_from':datetime.fromtimestamp(now-86400,ZONE).strftime('%Y-%m-%d %H:%M:%S'),
                        'date_to':datetime.fromtimestamp(now,ZONE).strftime('%Y-%m-%d %H:%M:%S')}
                    response=api(account['token'],endpoint,params)
                    summary=limits_summary(response) if section=='limits' else report_summary(response,now,settings['pending_minutes'])
                    cache[section]={'data':summary,'at':time.time(),'attempt_at':now,'error':'','latency_ms':round((time.monotonic()-start)*1000)}
                except Error as e:
                    cache[section]={**previous,'attempt_at':now,'error':str(e),'latency_ms':None}
                changed=True
            alerts=[]
            for section in ('limits','reports'):
                if cache.get(section,{}).get('error'): alerts.append(section+'_api')
            limits=cache.get('limits',{}); reports=cache.get('reports',{})
            if not limits.get('error'):
                for item in limits.get('data',[]):
                    if item['remaining'] is not None and item['remaining']<=settings['low_limit']: alerts.append('low_'+item['type'])
            summary=reports.get('data',{})
            if not reports.get('error'):
                if summary.get('completed',0)>=5 and (summary.get('failure_percent') or 0)>=settings['failure_percent']: alerts.append('delivery_failures')
                if summary.get('old_pending',0)>0: alerts.append('pending_too_long')
            previous_alerts=cache.get('alerts',[])
            new_alerts=[a for a in alerts if a not in previous_alerts]
            if new_alerts and settings['notify']: notifications.append('Monitoring needs attention. Open BananSMS Monitor for details.')
            cache['alerts']=alerts
            if changed or alerts!=previous_alerts: self.db.put(key,cache)
            output.append({'id':account['id'],'name':account['name'],**cache})
        self.require()
        return {'accounts':output,'settings':settings,'notify':bool(notifications)}
    def dispatch(self, data):
        action=data.get('action')
        if action=='status': return {'exists':self.exists(),'locked':not bool(self.db)}
        if action=='create':
            self.create(data.get('password','')); return {'unlocked':True,'settings':self.settings()}
        if action=='unlock':
            self.close(); private_dir(self.state)
            self.db=self.open_password(data.get('password','')); self.touch()
            return {'unlocked':True,'settings':self.settings()}
        self.require()
        if action=='touch': self.touch(); return {}
        if action=='snapshot': return self.snapshot(bool(data.get('refresh')))
        if action=='save_account':
            name=str(data.get('name','')).strip(); token=str(data.get('token','')).strip(); aid=data.get('account')
            if not name or len(name)>60: raise Error('Enter an account label of 1–60 characters.')
            if not re.fullmatch(r'[A-Za-z0-9._~+/-]{16,512}={0,2}',token): raise Error('Enter a valid API token.')
            accounts=self.db.get('accounts',[])
            if aid and not any(a['id']==aid for a in accounts): raise Error('Account no longer exists.')
            if not aid and len(accounts)>=5: raise Error('Up to five accounts are supported.')
            limits=limits_summary(api(token,'account/limits',{'show_type':'true'}))
            self.require()
            aid=aid or secrets.token_hex(12)
            accounts=[a for a in accounts if a['id']!=aid]+[{'id':aid,'name':name,'token':token}]
            self.db.query('BEGIN IMMEDIATE')
            try:
                self.db.put('accounts',accounts)
                self.db.put('cache/'+aid,{'limits':{'data':limits,'at':time.time(),'attempt_at':time.time(),'error':''}})
                self.db.query('COMMIT')
            except Exception: self.db.query('ROLLBACK'); raise
            return {}
        if action=='remove_account':
            aid=str(data.get('account',''))
            self.db.query('BEGIN IMMEDIATE')
            try:
                self.db.put('accounts',[a for a in self.db.get('accounts',[]) if a['id']!=aid])
                self.db.delete('cache/'+aid); self.db.query('COMMIT')
            except Exception: self.db.query('ROLLBACK'); raise
            return {}
        if action=='settings':
            values=self.settings()
            for key,low,high in [('timeout',30,86400),('interval',60,3600),('low_limit',0,1000000),('failure_percent',1,100),('pending_minutes',1,1440)]:
                try: value=int(data.get(key,values[key]))
                except (ValueError,TypeError): raise Error('Invalid setting: '+key)
                if not low<=value<=high: raise Error('Setting out of range: '+key)
                values[key]=value
            for key in ('notify','show_limits','show_delivery','show_cost'):
                if key in data:
                    if not isinstance(data[key],bool): raise Error('Invalid display setting.')
                    values[key]=data[key]
            self.db.put('settings',values); self.touch()
            return {'settings':values}
        if action=='password':
            password=data.get('password',''); self.password(password)
            check=self.open_password(data.get('current_password','')); check.close()
            self.db.key(password,rekey=True); self.touch(); return {}
        raise Error('Unknown action. This plugin only monitors services.')

def demo_snapshot():
    now=time.time()
    reports=report_summary({'items':[
        {'id':str(i),'status':'delivered' if i<36 else 'undelivered' if i<38 else 'pending',
         'cost':'0.12','parts':1,'reason':'operator_error',
         'queued':datetime.fromtimestamp(now-600,ZONE).isoformat(),
         'sent':datetime.fromtimestamp(now-590,ZONE).isoformat(),
         'delivered':datetime.fromtimestamp(now-580,ZONE).isoformat()} for i in range(40)]},now,15)
    return {'settings':DEFAULTS,'accounts':[{'id':'demo','name':'Demo workspace','alerts':['low_full'],
        'limits':{'data':[{'type':'full','value':'80','remaining':80},{'type':'eco','value':'no_limit','remaining':None},
                          {'type':'mms','value':'250','remaining':250},{'type':'voice','value':'not_available','remaining':None}],
                  'at':now,'attempt_at':now,'latency_ms':118,'error':''},
        'reports':{'data':reports,'at':now,'attempt_at':now,'latency_ms':142,'error':''}}],'notify':False}

def main():
    resource.setrlimit(resource.RLIMIT_CORE,(0,0)); os.umask(0o077)
    ctypes.CDLL(None).prctl(4,0,0,0,0)
    if sys.argv[1:] not in (['--serve'],['--serve','--demo']): return
    demo='--demo' in sys.argv[1:]; monitor=None if demo else Monitor()
    def watchdog():
        while True:
            time.sleep(.25)
            if monitor.db and monitor.deadline>0 and time.monotonic()>=monitor.deadline:
                os.write(1,b'{"event":"locked"}\n'); os._exit(0)
    if monitor: threading.Thread(target=watchdog,daemon=True).start()
    buffer=b''
    try:
        while True:
            if b'\n' not in buffer:
                ready,_,_=select.select([sys.stdin],[],[],.25)
                if not ready: continue
                chunk=os.read(0,65536)
                if not chunk: return
                buffer+=chunk
                if len(buffer)>65536: return
                continue
            raw,buffer=buffer.split(b'\n',1); data={}
            try:
                if len(raw)>8192: raise Error('Request too large.')
                data=json.loads(raw)
                if not isinstance(data,dict): data={}; raise Error('Invalid request.')
                if demo:
                    if data.get('action')=='snapshot': result=demo_snapshot()
                    elif data.get('action')=='status': result={'exists':False,'locked':False,'demo':True}
                    else: raise Error('Switch off demo to configure your encrypted monitor.')
                else: result=monitor.dispatch(data)
                response={'ok':True,**result}
            except Error as e: response={'ok':False,'error':str(e),'locked':e.locked}
            except VaultError as e: response={'ok':False,'error':str(e)}
            except Exception: response={'ok':False,'error':'Cannot complete the request. Check local storage and configuration.'}
            response['id']=data.get('id'); print(json.dumps(response),flush=True)
            raw=b''; data={}; result=None; response=None
    finally:
        if monitor: monitor.close()

if __name__=='__main__': main()
