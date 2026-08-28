from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import json
import hashlib

app = FastAPI()

# In-memory store for stateful freezeId persistence.
# For a multi-instance production setup, this would be Redis/Postgres.
STORE = {}

def handle_freeze(body):
    freezeId = body.get("freezeId")
    if not isinstance(freezeId, str) or not (0 < len(freezeId) <= 128):
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

    calDigest = body.get("calibrationDigest")
    tokDigest = body.get("tokenizerDigest")
    if not isinstance(calDigest, str) or len(calDigest) == 0:
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
    if not isinstance(tokDigest, str) or len(tokDigest) == 0:
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

    allowed = body.get("allowedUnsupportedReasons")
    if not isinstance(allowed, list):
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
    for a in allowed:
        if not isinstance(a, str) or len(a) == 0:
            return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
    if len(set(allowed)) != len(allowed):
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

    candidates_in = body.get("candidates")
    if not isinstance(candidates_in, list) or len(candidates_in) == 0:
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

    cand_names = []
    for c in candidates_in:
        if not isinstance(c, dict):
            return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        c_name = c.get("name")
        if not isinstance(c_name, str) or len(c_name) == 0:
            return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        cand_names.append(c_name)

    if len(set(cand_names)) != len(cand_names):
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

    # Replay Check
    body_str = json.dumps(body, sort_keys=True)
    if freezeId in STORE:
        if STORE[freezeId]["req"] == body_str:
            return JSONResponse(STORE[freezeId]["res"])
        else:
            return JSONResponse({"error": "FREEZE_ID_CONFLICT"}, status_code=409)

    out_candidates = []
    for c in candidates_in:
        c_name = c["name"]
        reasonCodes = []

        files = c.get("files")
        files_valid = False
        if isinstance(files, dict) and len(files) > 0:
            if all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
                files_valid = True

        inventory = []
        totalBytes = None
        packageDigest = None

        if files_valid:
            for k, v in files.items():
                b = len(v.encode("utf-8"))
                h = hashlib.sha256(v.encode("utf-8")).hexdigest().lower()
                inventory.append({"name": k, "bytes": b, "sha256": h})
            inventory.sort(key=lambda x: x["name"].encode("utf-8"))
            totalBytes = sum(x["bytes"] for x in inventory)
            
            # Exact JSON formatting for SHA-256 (compact + exact key order)
            ordered_inv = [{"name": x["name"], "bytes": x["bytes"], "sha256": x["sha256"]} for x in inventory]
            packageDigest = hashlib.sha256(json.dumps(ordered_inv, separators=(',', ':')).encode("utf-8")).hexdigest().lower()
        else:
            reasonCodes.append("INVALID_INPUT")

        is_allowed_unsupported = False
        unsupportedReason = c.get("unsupportedReason")
        if unsupportedReason is not None:
            if unsupportedReason in allowed:
                is_allowed_unsupported = True
            else:
                reasonCodes.append("UNALLOWED_UNSUPPORTED_REASON")

        if not is_allowed_unsupported:
            if c.get("loadable") is not True:
                reasonCodes.append("NOT_LOADABLE")
            if c.get("calibrationDigest") != calDigest:
                reasonCodes.append("CALIBRATION_MISMATCH")
            if c.get("tokenizerDigest") != tokDigest:
                reasonCodes.append("TOKENIZER_MISMATCH")

        if reasonCodes:
            status = "invalid"
        elif is_allowed_unsupported:
            status = "unsupported"
        else:
            status = "frozen"

        reasonCodes = sorted(list(set(reasonCodes)), key=lambda x: x.encode("utf-8"))

        out_candidates.append({
            "name": c_name,
            "status": status,
            "inventory": inventory,
            "totalBytes": totalBytes,
            "packageDigest": packageDigest,
            "reasonCodes": reasonCodes
        })

    out_candidates.sort(key=lambda x: x["name"].encode("utf-8"))

    res_body = {
        "freezeId": freezeId,
        "candidates": out_candidates
    }

    # Store for reuse
    STORE[freezeId] = {
        "req": body_str,
        "res": res_body
    }

    return JSONResponse(res_body)

def handle_select(body):
    candidates_in = body.get("candidates")
    rows = body.get("rows")
    policy = body.get("policy")

    if not isinstance(candidates_in, list) or not isinstance(rows, list) or not isinstance(policy, dict):
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

    freezeId = body.get("freezeId")
    latencies = body.get("latencies", {})
    if not isinstance(latencies, dict):
        latencies = {}

    # Validate Policy
    valid_policy = True
    maxBytes = policy.get("maxBytes")
    if maxBytes is not None and (not isinstance(maxBytes, int) or maxBytes < 0):
        valid_policy = False
    
    aggFloor = policy.get("aggregateFloor")
    if aggFloor is not None and (not isinstance(aggFloor, (int, float)) or not (0 <= aggFloor <= 1)):
        valid_policy = False
    
    req_slices = policy.get("requiredSlices", {})
    if not isinstance(req_slices, dict):
        valid_policy = False
    else:
        for v in req_slices.values():
            if not isinstance(v, (int, float)) or not (0 <= v <= 1):
                valid_policy = False
                
    maxLat = policy.get("maxLatencyMs")
    if maxLat is not None and (not isinstance(maxLat, (int, float)) or maxLat < 0):
        valid_policy = False
        
    candOrder = policy.get("candidateOrder")
    if not isinstance(candOrder, list):
        valid_policy = False
    else:
        if len(set(candOrder)) != len(candOrder):
            valid_policy = False
        names_in_candidates = [c["name"] for c in candidates_in if isinstance(c, dict) and "name" in c]
        if set(candOrder) != set(names_in_candidates) or len(set(names_in_candidates)) != len(names_in_candidates):
            valid_policy = False

    stored_freeze = STORE.get(freezeId)
    stored_cands = {sc["name"]: sc for sc in stored_freeze["res"]["candidates"]} if stored_freeze else {}

    results = []
    for c in candidates_in:
        if not isinstance(c, dict): continue
        c_name = c.get("name")
        if not isinstance(c_name, str): continue

        reasonCodes = []
        if not valid_policy:
            reasonCodes.append("INVALID_POLICY")

        stored_c = stored_cands.get(c_name)
        if not stored_c:
            reasonCodes.append("NOT_FROZEN")
            reasonCodes.append("INVALID_LINEAGE")
        else:
            if stored_c.get("status") != "frozen":
                reasonCodes.append("NOT_FROZEN")
            # Lineage Exact Match
            if json.dumps(c, sort_keys=True) != json.dumps(stored_c, sort_keys=True):
                reasonCodes.append("INVALID_LINEAGE")

        # Manifest Verification
        inv = c.get("inventory")
        recomputed_bytes = None
        if isinstance(inv, list):
            try:
                ordered_inv = [{"name": str(x["name"]), "bytes": int(x["bytes"]), "sha256": str(x["sha256"])} for x in inv]
                ordered_inv.sort(key=lambda x: x["name"].encode("utf-8"))
                recomputed_bytes = sum(x["bytes"] for x in ordered_inv)
                recomputed_digest = hashlib.sha256(json.dumps(ordered_inv, separators=(',', ':')).encode("utf-8")).hexdigest().lower()
                
                if recomputed_bytes != c.get("totalBytes") or recomputed_digest != c.get("packageDigest"):
                    reasonCodes.append("INVALID_MANIFEST")
            except:
                reasonCodes.append("INVALID_MANIFEST")
                recomputed_bytes = None
        else:
            reasonCodes.append("INVALID_MANIFEST")

        # Row Predictions & Slices
        valid_preds = True
        agg_sum, agg_count = 0, 0
        slice_sums, slice_counts = {}, {}

        for row in rows:
            if not isinstance(row, dict):
                valid_preds = False
                break
            preds = row.get("predictions", {})
            if not isinstance(preds, dict) or c_name not in preds or preds[c_name] not in (0, 1):
                valid_preds = False
                break

            is_correct = 1 if preds[c_name] == row.get("label") else 0
            agg_sum += is_correct
            agg_count += 1
            
            s_name = row.get("slice")
            if s_name:
                slice_sums[s_name] = slice_sums.get(s_name, 0) + is_correct
                slice_counts[s_name] = slice_counts.get(s_name, 0) + 1

        agg_acc = None
        slice_accs = {}
        if not valid_preds:
            reasonCodes.append("INVALID_PREDICTIONS")
        else:
            agg_acc = round(agg_sum / agg_count, 12) if agg_count > 0 else 0.0
            if aggFloor is not None and agg_acc < aggFloor:
                reasonCodes.append("AGGREGATE_FLOOR")

            for req_s_name, req_s_floor in req_slices.items():
                if req_s_name not in slice_counts:
                    reasonCodes.append(f"MISSING_SLICE:{req_s_name}")
                else:
                    s_acc = round(slice_sums[req_s_name] / slice_counts[req_s_name], 12)
                    slice_accs[req_s_name] = s_acc
                    if s_acc < req_s_floor:
                        reasonCodes.append(f"SLICE_FLOOR:{req_s_name}")

        # Limits Check
        if recomputed_bytes is None or (maxBytes is not None and recomputed_bytes > maxBytes):
            reasonCodes.append("SIZE_LIMIT")

        lat = latencies.get(c_name)
        if lat is None or not isinstance(lat, (int, float)) or lat < 0:
            reasonCodes.append("LATENCY_LIMIT")
            lat = None
        elif maxLat is not None and lat > maxLat:
            reasonCodes.append("LATENCY_LIMIT")

        admitted = (len(reasonCodes) == 0)
        reasonCodes = sorted(list(set(reasonCodes)), key=lambda x: x.encode("utf-8"))

        results.append({
            "name": c_name,
            "aggregate": agg_acc if valid_preds else None,
            "slices": slice_accs if valid_preds else {},
            "totalBytes": recomputed_bytes if "INVALID_MANIFEST" not in reasonCodes else None,
            "latencyMs": lat,
            "admitted": admitted,
            "reasonCodes": reasonCodes
        })

    # Sort Results output
    order_map = {name: i for i, name in enumerate(candOrder)} if isinstance(candOrder, list) else {}
    results.sort(key=lambda x: (order_map.get(x["name"], float('inf')), x["name"].encode("utf-8")))

    # Pick Winner
    admitted_cands = [r for r in results if r["admitted"]]
    if admitted_cands:
        admitted_cands.sort(key=lambda x: (
            x["totalBytes"],
            x["latencyMs"],
            order_map.get(x["name"], float('inf')),
            x["name"].encode("utf-8")
        ))
        winner_name = admitted_cands[0]["name"]
        packageManifest = next((c for c in candidates_in if c.get("name") == winner_name), None)
    else:
        winner_name = None
        packageManifest = None

    return JSONResponse({
        "freezeId": freezeId,
        "selected": winner_name,
        "results": results,
        "packageManifest": packageManifest
    })

@app.post("/quantize")
async def quantize(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)

    phase = body.get("phase")
    if phase == "freeze":
        return handle_freeze(body)
    elif phase == "select":
        return handle_select(body)
    else:
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
