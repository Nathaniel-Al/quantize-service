import hashlib,json,math
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
app=FastAPI(); SAFE=9007199254740991; store={}; REQUIRED=('model.safetensors',)
def kb(x): return x.encode('utf-8')
def finite(x): return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
def integer(x): return isinstance(x,int) and not isinstance(x,bool) and 0<=x<=SAFE
def codes(x): return sorted(set(x),key=kb)
def sha(x): return hashlib.sha256(x).hexdigest()
def make_inventory(files):
 if not isinstance(files,dict) or not files or any(not isinstance(k,str) or not k or not isinstance(v,str) for k,v in files.items()) or len(files)!=len(set(files)): return None
 return [{'name':n,'bytes':len(files[n].encode('utf-8')),'sha256':sha(files[n].encode('utf-8'))} for n in sorted(files,key=kb)]
def package_digest(inv): return sha(json.dumps(inv,separators=(',',':'),ensure_ascii=False).encode('utf-8'))
def freeze(p):
 out=[]
 for c in p['candidates']:
  err=[]; inv=make_inventory(c.get('files'))
  valid=isinstance(c,dict) and isinstance(c.get('name'),str) and bool(c.get('name')) and isinstance(c.get('loadable'),bool) and isinstance(c.get('calibrationDigest'),str) and bool(c['calibrationDigest']) and isinstance(c.get('tokenizerDigest'),str) and bool(c['tokenizerDigest']) and (c.get('unsupportedReason') is None or (isinstance(c.get('unsupportedReason'),str) and bool(c['unsupportedReason'])))
  if not valid: err.append('INVALID_INPUT')
  unsupported=c.get('unsupportedReason') if isinstance(c,dict) else None
  if unsupported is not None:
   if unsupported not in p['allowedUnsupportedReasons']: err.append('UNALLOWED_UNSUPPORTED_REASON')
   status='unsupported' if not err and inv is not None else 'invalid'
  else:
   if c.get('loadable') is not True: err.append('NOT_LOADABLE')
   if c.get('calibrationDigest')!=p['calibrationDigest']: err.append('CALIBRATION_MISMATCH')
   if c.get('tokenizerDigest')!=p['tokenizerDigest']: err.append('TOKENIZER_MISMATCH')
   status='frozen' if not err and inv is not None else 'invalid'
  out.append({'name':c.get('name'),'status':status,'inventory':inv or [],'totalBytes':sum(x['bytes'] for x in inv) if inv is not None else None,'packageDigest':package_digest(inv) if inv is not None else None,'reasonCodes':codes(err)})
 return {'freezeId':p['freezeId'],'candidates':sorted(out,key=lambda x:kb(x['name'] or ''))}
def select(p):
 frozen=store.get(p['freezeId']); stored=frozen[1]; base=stored['candidates']; submitted=p['candidates']; policy=p['policy']; rows=p['rows']; by={x['name']:x for x in base}; order=policy.get('candidateOrder',[]); err=[]
 if not isinstance(policy.get('requiredSlices'),dict) or not finite(policy.get('maxBytes')) or policy['maxBytes']<0 or not finite(policy.get('maxLatencyMs')) or policy['maxLatencyMs']<0 or not finite(policy.get('aggregateFloor')) or not 0<=policy['aggregateFloor']<=1: err.append('INVALID_POLICY')
 if sorted(order,key=kb)!=sorted(by,key=kb) or len(order)!=len(by): err.append('INVALID_POLICY')
 result=[]
 for n in order:
  r=[]; f=by.get(n); sub=next((x for x in submitted if isinstance(x,dict) and x.get('name')==n),None)
  if not f or f['status']!='frozen': r.append('NOT_FROZEN')
  if sub!=f: r.append('INVALID_LINEAGE')
  inv=make_inventory(sub.get('files')) if isinstance(sub,dict) else None
  if inv is None or (f and inv!=f['inventory']) or (f and package_digest(inv)!=f['packageDigest']): r.append('INVALID_MANIFEST')
  total=sum(x['bytes'] for x in inv) if inv is not None else None
  lat=p.get('latencies',{}).get(n) if isinstance(p.get('latencies'),dict) else None
  if not integer(total): total=None; r.append('INVALID_MANIFEST')
  if not finite(lat) or lat<0: lat=None; r.append('INVALID_MANIFEST')
  vals=[]; groups={}; valid=True
  for row in rows:
   pred=row.get('predictions',{}).get(n) if isinstance(row,dict) and isinstance(row.get('predictions'),dict) else None
   if not isinstance(row,dict) or row.get('label') not in (0,1) or not isinstance(row.get('slice'),str) or not row['slice'] or pred not in (0,1): valid=False; break
   ok=pred==row['label']; vals.append(ok); groups.setdefault(row['slice'],[]).append(ok)
  agg=round(sum(vals)/len(vals),12) if valid and vals else None; sl={k:round(sum(v)/len(v),12) for k,v in groups.items()} if valid else {}
  if not valid:r.append('INVALID_PREDICTIONS')
  if agg is not None and agg<policy.get('aggregateFloor',0):r.append('AGGREGATE_FLOOR')
  for s,floor in policy.get('requiredSlices',{}).items() if isinstance(policy.get('requiredSlices'),dict) else []:
   if s not in sl:r.append('MISSING_SLICE:'+s)
   elif sl[s]<floor:r.append('SLICE_FLOOR:'+s)
  if total is not None and total>policy.get('maxBytes',float('inf')):r.append('SIZE_LIMIT')
  if lat is not None and lat>policy.get('maxLatencyMs',float('inf')):r.append('LATENCY_LIMIT')
  result.append({'name':n,'aggregate':agg,'slices':sl,'totalBytes':total,'latencyMs':lat,'admitted':not r,'reasonCodes':codes(r)})
 winners=[(x['totalBytes'],x['latencyMs'],i,x) for i,x in enumerate(result) if x['admitted']]; winner=min(winners)[3] if winners else None
 manifest=by[winner['name']] if winner else None
 return {'freezeId':p['freezeId'],'selected':winner['name'] if winner else None,'results':result,'packageManifest':manifest}
@app.post('/quantize')
async def endpoint(req:Request):
 try:p=await req.json()
 except Exception:return JSONResponse({'error':'INVALID_INPUT'},status_code=400)
 if not isinstance(p,dict) or p.get('phase') not in ('freeze','select'):return JSONResponse({'error':'INVALID_INPUT'},status_code=400)
 if p['phase']=='freeze':
  if not isinstance(p.get('freezeId'),str) or not p['freezeId'] or len(p['freezeId'])>128 or not isinstance(p.get('calibrationDigest'),str) or not p['calibrationDigest'] or not isinstance(p.get('tokenizerDigest'),str) or not p['tokenizerDigest'] or not isinstance(p.get('allowedUnsupportedReasons'),list) or not isinstance(p.get('candidates'),list) or not p['candidates'] or len(set(p['allowedUnsupportedReasons']))!=len(p['allowedUnsupportedReasons']) or len({c.get('name') for c in p['candidates'] if isinstance(c,dict)})!=len(p['candidates']):return JSONResponse({'error':'INVALID_INPUT'},status_code=400)
  if p['freezeId'] in store:
   if store[p['freezeId']][0]!=p:return JSONResponse({'error':'FREEZE_ID_CONFLICT'},status_code=409)
   return store[p['freezeId']][1]
  out=freeze(p); store[p['freezeId']]=(p,out); return out
 if not isinstance(p.get('freezeId'),str) or not isinstance(p.get('candidates'),list) or not isinstance(p.get('rows'),list) or not p['rows'] or not isinstance(p.get('policy'),dict):return JSONResponse({'error':'INVALID_INPUT'},status_code=400)
 if p['freezeId'] not in store:return {'freezeId':p['freezeId'],'selected':None,'results':[],'packageManifest':None}
 return select(p)
