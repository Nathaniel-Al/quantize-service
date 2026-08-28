from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import hashlib, json, math

app = FastAPI()
SAFE = 9007199254740991
FREEZES = {}
ORDER = ("int4", "int8", "int16", "fp16")

def b(x): return x.encode("utf-8")
def finite(x): return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
def integer(x): return isinstance(x,int) and not isinstance(x,bool) and 0 <= x <= SAFE
def codes(xs): return sorted(set(xs), key=b)
def sha(x): return hashlib.sha256(x).hexdigest()
def inv(files):
    if not isinstance(files,dict) or not files or any(not isinstance(k,str) or not k or not isinstance(v,str) for k,v in files.items()): return None
    return [{"name":n,"bytes":len(files[n].encode()),"sha256":sha(files[n].encode())} for n in sorted(files,key=b)]
def pd(i): return sha(json.dumps(i,separators=(",",":"),ensure_ascii=False).encode())

def freeze(p):
    ans=[]
    for c in p["candidates"]:
        e=[]; i=inv(c.get("files")); ok=isinstance(c,dict) and isinstance(c.get("loadable"),bool) and isinstance(c.get("calibrationDigest"),str) and bool(c["calibrationDigest"]) and isinstance(c.get("tokenizerDigest"),str) and bool(c["tokenizerDigest"]) and (c.get("unsupportedReason") is None or (isinstance(c.get("unsupportedReason"),str) and bool(c["unsupportedReason"])))
        if not ok:e.append("INVALID_INPUT")
        u=c.get("unsupportedReason")
        if u is not None:
            if u not in p["allowedUnsupportedReasons"]:e.append("UNALLOWED_UNSUPPORTED_REASON")
            status="unsupported" if i is not None and not e else "invalid"
        else:
            if c.get("loadable") is not True:e.append("NOT_LOADABLE")
            if c.get("calibrationDigest")!=p["calibrationDigest"]:e.append("CALIBRATION_MISMATCH")
            if c.get("tokenizerDigest")!=p["tokenizerDigest"]:e.append("TOKENIZER_MISMATCH")
            status="frozen" if i is not None and not e else "invalid"
        ans.append({"name":c["name"],"status":status,"inventory":i or [],"totalBytes":sum(x["bytes"] for x in i) if i is not None else None,"packageDigest":pd(i) if i is not None else None,"reasonCodes":codes(e)})
    ans.sort(key=lambda x:b(x["name"]))
    return {"freezeId":p["freezeId"],"candidates":ans}

def select(p):
    frozen=FREEZES[p["freezeId"]][1]; base={x["name"]:x for x in frozen["candidates"]}; pol=p["policy"]; submitted={x.get("name"):x for x in p["candidates"] if isinstance(x,dict)}; order=pol.get("candidateOrder",[]); out=[]
    bad=[]; req=pol.get("requiredSlices")
    if not isinstance(req,dict) or not finite(pol.get("maxBytes")) or pol["maxBytes"]<0 or not finite(pol.get("aggregateFloor")) or not 0<=pol["aggregateFloor"]<=1 or not finite(pol.get("maxLatencyMs")) or pol["maxLatencyMs"]<0 or not isinstance(order,list) or len(order)!=len(set(order)) or set(order)!=set(base):bad.append("INVALID_POLICY")
    for n in order:
        e=list(bad); f=base.get(n); s=submitted.get(n)
        if not f or f["status"]!="frozen":e.append("NOT_FROZEN")
        if s!=f:e.append("INVALID_LINEAGE")
        i=inv(s.get("files")) if s else None
        if i is None or not f or i!=f["inventory"] or pd(i)!=f["packageDigest"]:e.append("INVALID_MANIFEST")
        total=sum(x["bytes"] for x in i) if i is not None else None; lat=p.get("latencies",{}).get(n) if isinstance(p.get("latencies"),dict) else None
        if not integer(total):total=None;e.append("INVALID_MANIFEST")
        if not finite(lat) or lat<0:lat=None;e.append("INVALID_MANIFEST")
        good=True; vals=[]; groups={}
        for row in p["rows"]:
            pred=row.get("predictions",{}).get(n) if isinstance(row,dict) and isinstance(row.get("predictions"),dict) else None
            if not isinstance(row,dict) or row.get("label") not in (0,1) or not isinstance(row.get("slice"),str) or not row["slice"] or pred not in (0,1):good=False;break
            q=pred==row["label"];vals.append(q);groups.setdefault(row["slice"],[]).append(q)
        agg=round(sum(vals)/len(vals),12) if good and vals else None; slices={k:round(sum(v)/len(v),12) for k,v in groups.items()} if good else {}
        if not good:e.append("INVALID_PREDICTIONS")
        if agg is not None and agg<pol.get("aggregateFloor",0):e.append("AGGREGATE_FLOOR")
        for sn,floor in req.items():
            if sn not in slices:e.append("MISSING_SLICE:"+sn)
            elif slices[sn]<floor:e.append("SLICE_FLOOR:"+sn)
        if total is not None and total>pol.get("maxBytes",float("inf")):e.append("SIZE_LIMIT")
        if lat is not None and lat>pol.get("maxLatencyMs",float("inf")):e.append("LATENCY_LIMIT")
        out.append({"name":n,"aggregate":agg,"slices":slices,"totalBytes":total,"latencyMs":lat,"admitted":not e,"reasonCodes":codes(e)})
    winners=[(x["totalBytes"],x["latencyMs"],i,x) for i,x in enumerate(out) if x["admitted"]]; winner=min(winners)[3] if winners else None
    return {"freezeId":p["freezeId"],"selected":winner["name"] if winner else None,"results":out,"packageManifest":base.get(winner["name"]) if winner else None}

@app.post("/quantize")
async def quantize(req:Request):
    try:p=await req.json()
    except Exception:return JSONResponse({"error":"INVALID_INPUT"},status_code=400)
    if not isinstance(p,dict) or p.get("phase") not in ("freeze","select"):return JSONResponse({"error":"INVALID_INPUT"},status_code=400)
    if p["phase"]=="freeze":
        a=p.get("allowedUnsupportedReasons"); cs=p.get("candidates")
        ok=isinstance(p.get("freezeId"),str) and bool(p["freezeId"]) and len(p["freezeId"])<=128 and isinstance(p.get("calibrationDigest"),str) and bool(p["calibrationDigest"]) and isinstance(p.get("tokenizerDigest"),str) and bool(p["tokenizerDigest"]) and isinstance(a,list) and all(isinstance(x,str) and x for x in a) and len(a)==len(set(a)) and isinstance(cs,list) and bool(cs) and all(isinstance(x,dict) and isinstance(x.get("name"),str) and bool(x["name"]) for x in cs) and len({x["name"] for x in cs})==len(cs)
        if not ok:return JSONResponse({"error":"INVALID_INPUT"},status_code=400)
        fid=p["freezeId"]
        if fid in FREEZES:
            if FREEZES[fid][0]!=p:return JSONResponse({"error":"FREEZE_ID_CONFLICT"},status_code=409)
            return FREEZES[fid][1]
        result=freeze(p);FREEZES[fid]=(p,result);return result
    if not isinstance(p.get("freezeId"),str) or not isinstance(p.get("candidates"),list) or not isinstance(p.get("rows"),list) or not p["rows"] or not isinstance(p.get("policy"),dict):return JSONResponse({"error":"INVALID_INPUT"},status_code=400)
    if p["freezeId"] not in FREEZES:return {"freezeId":p["freezeId"],"selected":None,"results":[],"packageManifest":None}
    return select(p)
