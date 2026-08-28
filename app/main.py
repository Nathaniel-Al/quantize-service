import hashlib,json,math
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
app=FastAPI(); SAFE=9007199254740991; store={}; ORDERKEY=lambda x:x.encode('utf-8')
def finite(x): return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
def integer(x): return isinstance(x,int) and not isinstance(x,bool) and 0<=x<=SAFE
def codes(xs): return sorted(set(xs),key=ORDERKEY)
def digest(x): return hashlib.sha256(x).hexdigest()
def inventory(files):
 if not isinstance(files,dict) or not files or any(not isinstance(n,str) or not n or not isinstance(v,str) for n,v in files.items()) or len(files)!=len(set(files)): return None
 a=[{'name':n,'bytes':len(files[n].encode()),'sha256':digest(files[n].encode())} for n in sorted(files,key=ORDERKEY)]
 return a
def freeze(p):
 fid=p['freezeId']; out=[]
 for c in p['candidates']:
  r=[]; inv=inventory(c.get('files')); valid=inv is not None
  if not isinstance(c,dict) or not isinstance(c.get('name'),str) or not c['name'] or not isinstance(c.get('loadable'),bool) or not isinstance(c.get('calibrationDigest'),str) or not c['calibrationDigest'] or not isinstance(c.get('tokenizerDigest'),str) or not c['tokenizerDigest'] or (c.get('unsupportedReason') is not None and (not isinstance(c.get('unsupportedReason'),str) or not c['unsupportedReason'])): valid=False
  if c.get('unsupportedReason') is not None:
   if c['unsupportedReason'] not in p['allowedUnsupportedReasons']: r.append('UNALLOWED_UNSUPPORTED_REASON')
   status='unsupported' if not r and valid else 'invalid'
  else:
   if not c.get('loadable'): r.append('NOT_LOADABLE')
   if c.get('calibrationDigest')!=p['calibrationDigest']: r.append('CALIBRATION_MISMATCH')
   if c.get('tokenizerDigest')!=p['tokenizerDigest']: r.append('TOKENIZER_MISMATCH')
   status='frozen' if not r and valid else 'invalid'
  out.append({'name':c.get('name'),'status':status,'inventory':inv or [],'totalBytes':sum(x['bytes'] for x in inv) if inv is not None else None,'packageDigest':digest(json.dumps(inv,separators=(',',':'),ensure_ascii=False).encode()) if inv is not None else None,'reasonCodes':codes(r)})
 out.sort(key=lambda x:ORDERKEY(x['name'] or '')); return {'freezeId':fid,'candidates':out}
def select(p):
 fid=p['freezeId']; base=store.get(fid); cs=p['candidates']; pol=p['policy']; rows=p['rows']; names=[x['name'] for x in base[1]['candidates']]
 bad=[]
 if not isinstance(pol,dict) or not isinstance(cs,list) or not isinstance(rows,list) or not rows or not finite(pol.get('maxBytes')) or pol['maxBytes']<0 or not finite(pol.get('aggregateFloor')) or not 0<=pol['aggregateFloor']<=1 or not finite(pol.get('maxLatencyMs')) or pol['maxLatencyMs']<0 or not isinstance(pol.get('requiredSlices'),dict): bad=['INVALID_POLICY']
 order=pol.get('candidateOrder',[]) if isinstance(pol,dict) else []
 if sorted(order,key=ORDERKEY)!=sorted(set(names),key=ORDERKEY) or len(order)!=len(names): bad.append('INVALID_POLICY')
 results=[]
 for n in order:
  r=[]; frozen=next((x for x in base[1]['candidates'] if x['name']==n),None); sub=next((x for x in cs if isinstance(x,dict) and x.get('name')==n),None)
  if not frozen or frozen['status']!='frozen': r.append('NOT_FROZEN')
  if not sub or sub!=frozen: r.append('INVALID_LINEAGE')
  if not frozen or (sub and sub.get('packageDigest')!=frozen['packageDigest']): r.append('INVALID_MANIFEST')
  total=frozen['totalBytes'] if frozen and isinstance(frozen.get('totalBytes'),int) and frozen['totalBytes']>=0 else None
  lat=p.get('latencies',{}).get(n) if isinstance(p.get('latencies'),dict) else None
  if not integer(total): total=None; r.append('INVALID_MANIFEST')
  if not finite(lat) or lat<0: lat=None; r.append('INVALID_MANIFEST')
  vals=[]; groups={}; validpred=True
  for row in rows:
   x=row.get('predictions',{}).get(n) if isinstance(row,dict) and isinstance(row.get('predictions'),dict) else None
   if not isinstance(row,dict) or row.get('label') not in (0,1) or not isinstance(row.get('slice'),str) or not row['slice'] or x not in (0,1): validpred=False; break
   q=x==row['label']; vals.append(q); groups.setdefault(row['slice'],[]).append(q)
  agg=round(sum(vals)/len(vals),12) if validpred and vals else None; slices={k:round(sum(v)/len(v),12) for k,v in groups.items()} if validpred else {}
  if not validpred: r.append('INVALID_PREDICTIONS')
  if agg is not None and agg<pol.get('aggregateFloor',0): r.append('AGGREGATE_FLOOR')
  for s,f in pol.get('requiredSlices',{}).items() if isinstance(pol.get('requiredSlices'),dict) else []:
   if s not in groups:r.append('MISSING_SLICE:'+s)
   elif slices[s]<f:r.append('SLICE_FLOOR:'+s)
  if total is not None and total>pol.get('maxBytes',float('inf')):r.append('SIZE_LIMIT')
  if lat is not None and lat>pol.get('maxLatencyMs',float('inf')):r.append('LATENCY_LIMIT')
  results.append({'name':n,'aggregate':agg,'slices':slices,'totalBytes':total,'latencyMs':lat,'admitted':not r,'reasonCodes':codes(r)})
 winner=None
 for i,x in enumerate(results):
  if x['admitted'] and (winner is None or (x['totalBytes'],x['latencyMs'],i)<(winner[1]['totalBytes'],winner[1]['latencyMs'],winner[0])): winner=(i,x)
 manifest=None if winner is None else next(x for x in base[1]['candidates'] if x['name']==winner[1]['name'])
 return {'freezeId':fid,'selected':None if winner is None else winner[1]['name'],'results':results,'packageManifest':manifest}
@app.post('/quantize')
async def endpoint(req:Request):
 try:p=await req.json()
 except Exception:return JSONResponse({'error':'INVALID_INPUT'},status_code=400)
 if not isinstance(p,dict) or p.get('phase') not in ('freeze','select'):return JSONResponse({'error':'INVALID_INPUT'},status_code=400)
 if p['phase']=='freeze':
  if not isinstance(p.get('freezeId'),str) or not p['freezeId'] or len(p['freezeId'])>128 or not isinstance(p.get('candidates'),list) or not p['candidates'] or not isinstance(p.get('allowedUnsupportedReasons'),list) or len(set(p['allowedUnsupportedReasons']))!=len(p['allowedUnsupportedReasons']) or any(not isinstance(x,str) or not x for x in p['allowedUnsupportedReasons']):return JSONResponse({'error':'INVALID_INPUT'},status_code=400)
  if any(not isinstance(x,dict) or not isinstance(x.get('name'),str) or not x['name'] for x in p['candidates']) or len({x['name'] for x in p['candidates']})!=len(p['candidates']):return JSONResponse({'error':'INVALID_INPUT'},status_code=400)
  out=freeze(p); store[p['freezeId']]=(p,out); return out
 if not isinstance(p.get('freezeId'),str) or not isinstance(p.get('candidates'),list) or not isinstance(p.get('rows'),list) or not isinstance(p.get('policy'),dict):return JSONResponse({'error':'INVALID_INPUT'},status_code=400)
 if p['freezeId'] not in store:return {'freezeId':p['freezeId'],'selected':None,'results':[],'packageManifest':None}
 return select(p)
