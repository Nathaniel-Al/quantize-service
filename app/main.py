import hashlib
import json
import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
SAFE = 9_007_199_254_740_991
CANDIDATE_ORDER = ("int4", "int8", "int16", "fp16")
FREEZES: dict[str, tuple[dict, dict]] = {}


def utf8(value: str) -> bytes:
    return value.encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def safe_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= SAFE


def sorted_codes(values: list[str]) -> list[str]:
    return sorted(set(values), key=utf8)


def make_inventory(files: Any) -> list[dict] | None:
    if not isinstance(files, dict) or not files:
        return None
    if any(not isinstance(name, str) or not name or not isinstance(text, str) for name, text in files.items()):
        return None
    if len(files) != len(set(files)):
        return None
    inventory = []
    for name in sorted(files, key=utf8):
        data = utf8(files[name])
        inventory.append({"name": name, "bytes": len(data), "sha256": sha256(data)})
    return inventory


def package_digest(inventory: list[dict]) -> str:
    data = json.dumps(inventory, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha256(data)


def valid_freeze_request(payload: dict) -> bool:
    allowed = payload.get("allowedUnsupportedReasons")
    candidates = payload.get("candidates")
    if not isinstance(payload.get("freezeId"), str) or not payload["freezeId"] or len(payload["freezeId"]) > 128:
        return False
    if not isinstance(payload.get("calibrationDigest"), str) or not payload["calibrationDigest"]:
        return False
    if not isinstance(payload.get("tokenizerDigest"), str) or not payload["tokenizerDigest"]:
        return False
    if not isinstance(allowed, list) or any(not isinstance(x, str) or not x for x in allowed):
        return False
    if len(allowed) != len(set(allowed)):
        return False
    if not isinstance(candidates, list) or not candidates:
        return False
    if any(not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c["name"] for c in candidates):
        return False
    return len({c["name"] for c in candidates}) == len(candidates)


def freeze(payload: dict) -> dict:
    result = []
    allowed = payload["allowedUnsupportedReasons"]
    for candidate in payload["candidates"]:
        reasons = []
        files = candidate.get("files")
        inventory = make_inventory(files)
        structurally_valid = (
            isinstance(candidate.get("loadable"), bool)
            and isinstance(candidate.get("calibrationDigest"), str)
            and bool(candidate["calibrationDigest"])
            and isinstance(candidate.get("tokenizerDigest"), str)
            and bool(candidate["tokenizerDigest"])
            and (candidate.get("unsupportedReason") is None or (
                isinstance(candidate.get("unsupportedReason"), str)
                and bool(candidate["unsupportedReason"])
            ))
        )
        if not structurally_valid:
            reasons.append("INVALID_INPUT")

        unsupported = candidate.get("unsupportedReason")
        if unsupported is not None:
            if unsupported not in allowed:
                reasons.append("UNALLOWED_UNSUPPORTED_REASON")
            status = "unsupported" if inventory is not None and not reasons else "invalid"
        else:
            if candidate.get("loadable") is not True:
                reasons.append("NOT_LOADABLE")
            if candidate.get("calibrationDigest") != payload["calibrationDigest"]:
                reasons.append("CALIBRATION_MISMATCH")
            if candidate.get("tokenizerDigest") != payload["tokenizerDigest"]:
                reasons.append("TOKENIZER_MISMATCH")
            status = "frozen" if inventory is not None and not reasons else "invalid"

        result.append({
            "name": candidate["name"],
            "status": status,
            "inventory": inventory or [],
            "totalBytes": sum(x["bytes"] for x in inventory) if inventory is not None else None,
            "packageDigest": package_digest(inventory) if inventory is not None else None,
            "reasonCodes": sorted_codes(reasons),
        })
    result.sort(key=lambda x: utf8(x["name"]))
    return {"freezeId": payload["freezeId"], "candidates": result}


def valid_select_request(payload: dict) -> bool:
    return (
        isinstance(payload.get("freezeId"), str)
        and isinstance(payload.get("candidates"), list)
        and isinstance(payload.get("rows"), list)
        and bool(payload["rows"])
        and isinstance(payload.get("policy"), dict)
    )


def select(payload: dict) -> dict:
    stored_input, frozen_response = FREEZES[payload["freezeId"]]
    frozen = {c["name"]: c for c in frozen_response["candidates"]}
    submitted = {c.get("name"): c for c in payload["candidates"] if isinstance(c, dict)}
    policy = payload["policy"]
    required = policy.get("requiredSlices")
    order = policy.get("candidateOrder")
    global_errors = []

    if (
        not isinstance(required, dict)
        or any(not isinstance(k, str) or not k or not finite(v) or not 0 <= v <= 1 for k, v in required.items())
        or not finite(policy.get("maxBytes"))
        or policy["maxBytes"] < 0
        or not finite(policy.get("aggregateFloor"))
        or not 0 <= policy["aggregateFloor"] <= 1
        or not finite(policy.get("maxLatencyMs"))
        or policy["maxLatencyMs"] < 0
        or not isinstance(order, list)
        or len(order) != len(set(order))
        or any(not isinstance(x, str) or not x for x in order)
        or set(order) != set(frozen)
    ):
        global_errors.append("INVALID_POLICY")

    results = []
    for name in order if isinstance(order, list) else sorted(frozen, key=utf8):
        errors = list(global_errors)
        frozen_candidate = frozen.get(name)
        submitted_candidate = submitted.get(name)
        if frozen_candidate is None or frozen_candidate["status"] != "frozen":
            errors.append("NOT_FROZEN")
        if submitted_candidate is None or submitted_candidate != frozen_candidate:
            errors.append("INVALID_LINEAGE")

        inventory = make_inventory(submitted_candidate.get("files")) if submitted_candidate else None
        if (
            inventory is None
            or frozen_candidate is None
            or inventory != frozen_candidate["inventory"]
            or package_digest(inventory) != frozen_candidate["packageDigest"]
        ):
            errors.append("INVALID_MANIFEST")

        total_bytes = sum(x["bytes"] for x in inventory) if inventory is not None else None
        latency = payload.get("latencies", {}).get(name) if isinstance(payload.get("latencies"), dict) else None
        if not safe_int(total_bytes):
            total_bytes = None
            errors.append("INVALID_MANIFEST")
        if not finite(latency) or latency < 0:
            latency = None
            errors.append("INVALID_MANIFEST")

        correct = []
        slices = {}
        valid_predictions = True
        for row in payload["rows"]:
            prediction = row.get("predictions", {}).get(name) if isinstance(row, dict) and isinstance(row.get("predictions"), dict) else None
            if (
                not isinstance(row, dict)
                or row.get("label") not in (0, 1)
                or not isinstance(row.get("slice"), str)
                or not row["slice"]
                or prediction not in (0, 1)
            ):
                valid_predictions = False
                break
            value = prediction == row["label"]
            correct.append(value)
            slices.setdefault(row["slice"], []).append(value)

        aggregate = round(sum(correct) / len(correct), 12) if valid_predictions and correct else None
        slice_values = {k: round(sum(v) / len(v), 12) for k, v in slices.items()} if valid_predictions else {}
        if not valid_predictions:
            errors.append("INVALID_PREDICTIONS")
        if aggregate is not None and aggregate < policy.get("aggregateFloor", 0):
            errors.append("AGGREGATE_FLOOR")
        for slice_name, floor in required.items() if isinstance(required, dict) else []:
            if slice_name not in slice_values:
                errors.append("MISSING_SLICE:" + slice_name)
            elif slice_values[slice_name] < floor:
                errors.append("SLICE_FLOOR:" + slice_name)
        if total_bytes is not None and total_bytes > policy.get("maxBytes", float("inf")):
            errors.append("SIZE_LIMIT")
        if latency is not None and latency > policy.get("maxLatencyMs", float("inf")):
            errors.append("LATENCY_LIMIT")
        result = {
            "name": name,
            "aggregate": aggregate,
            "slices": slice_values,
            "totalBytes": total_bytes,
            "latencyMs": latency,
            "admitted": not errors,
            "reasonCodes": sorted_codes(errors),
        }
        results.append(result)

    winners = [(r["totalBytes"], r["latencyMs"], i, r) for i, r in enumerate(results) if r["admitted"]]
    winner = min(winners)[3] if winners else None
    manifest = frozen.get(winner["name"]) if winner else None
    return {"freezeId": payload["freezeId"], "selected": winner["name"] if winner else None, "results": results, "packageManifest": manifest}


@app.post("/quantize")
async def quantize(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
    if not isinstance(payload, dict) or payload.get("phase") not in ("freeze", "select"):
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
    if payload["phase"] == "freeze":
        if not valid_freeze_request(payload):
            return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        freeze_id = payload["freezeId"]
        if freeze_id in FREEZES:
            previous, response = FREEZES[freeze_id]
            if previous != payload:
                return JSONResponse({"error": "FREEZE_ID_CONFLICT"}, status_code=409)
            return response
        response = freeze(payload)
        FREEZES[freeze_id] = (payload, response)
        return response
    if not valid_select_request(payload):
        return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
    if payload["freezeId"] not in FREEZES:
        return {"freezeId": payload["freezeId"], "selected": None, "results": [], "packageManifest": None}
    return select(payload)
