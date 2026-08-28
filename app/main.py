from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# SQLite gives us process-safe state and survives normal process restarts when the
# container filesystem is retained. Render's ephemeral filesystem can still be
# reset by a new deploy/instance; the API contract itself requires state within
# the running service.
DB_PATH = "/tmp/quantize_state.sqlite3"
DB_LOCK = threading.Lock()

CODES_FREEZE = {
    "INVALID_INPUT",
    "UNALLOWED_UNSUPPORTED_REASON",
    "NOT_LOADABLE",
    "CALIBRATION_MISMATCH",
    "TOKENIZER_MISMATCH",
}
CODES_SELECT = {
    "NOT_FROZEN",
    "INVALID_LINEAGE",
    "INVALID_POLICY",
    "INVALID_PREDICTIONS",
    "INVALID_MANIFEST",
    "AGGREGATE_FLOOR",
    "SIZE_LIMIT",
    "LATENCY_LIMIT",
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS freezes (freeze_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, response_json TEXT NOT NULL)")
    conn.commit()
    return conn


DB = db()


def compact_json_bytes(value: Any) -> bytes:
    # ensure_ascii=False makes the JSON UTF-8 representation use the actual
    # Unicode characters, while separators produce JSON.stringify-like compact JSON.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utf8_key(s: str) -> bytes:
    return s.encode("utf-8")


def sorted_utf8_strings(values: list[str]) -> list[str]:
    return sorted(values, key=utf8_key)


def is_string(x: Any) -> bool:
    return isinstance(x, str)


def is_finite_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def is_safe_nonnegative_integer(x: Any) -> bool:
    # JavaScript safe integer maximum is 2^53 - 1.
    return isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 9007199254740991


def code_sort(codes: set[str] | list[str]) -> list[str]:
    return sorted(set(codes), key=utf8_key)


def invalid_input() -> JSONResponse:
    return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)


def conflict() -> JSONResponse:
    return JSONResponse({"error": "FREEZE_ID_CONFLICT"}, status_code=409)


def valid_digest(x: Any) -> bool:
    return isinstance(x, str) and len(x) > 0


def validate_unique_nonempty_strings(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    if not all(isinstance(x, str) and len(x) > 0 for x in values):
        return False
    return len(values) == len(set(values))


def validate_files(files: Any) -> tuple[bool, list[dict[str, Any]], int | None, str | None]:
    if not isinstance(files, dict) or not files:
        return False, [], None, None

    entries: list[dict[str, Any]] = []
    for name, text in files.items():
        if not isinstance(name, str) or not isinstance(text, str):
            return False, [], None, None
        try:
            raw = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return False, [], None, None
        entries.append({"name": name, "bytes": len(raw), "sha256": sha256_hex(raw)})

    entries.sort(key=lambda e: utf8_key(e["name"]))
    total = sum(e["bytes"] for e in entries)
    package_digest = sha256_hex(compact_json_bytes(entries))
    return True, entries, total, package_digest


def parse_json_string(raw: bytes) -> Any:
    # Strict UTF-8 is required for application/json input.
    text = raw.decode("utf-8", errors="strict")
    return json.loads(text)


def freeze_request_valid(req: Any) -> bool:
    if not isinstance(req, dict) or req.get("phase") != "freeze":
        return False
    if not isinstance(req.get("freezeId"), str) or not (0 < len(req["freezeId"]) <= 128):
        return False
    if not valid_digest(req.get("calibrationDigest")) or not valid_digest(req.get("tokenizerDigest")):
        return False
    if not validate_unique_nonempty_strings(req.get("allowedUnsupportedReasons")):
        return False
    candidates = req.get("candidates")
    if not isinstance(candidates, list) or len(candidates) == 0:
        return False
    names: list[str] = []
    for c in candidates:
        if not isinstance(c, dict):
            return False
        if not isinstance(c.get("name"), str) or not c["name"]:
            return False
        names.append(c["name"])
        if "files" not in c:
            return False
        if "loadable" not in c or not isinstance(c["loadable"], bool):
            return False
        if not valid_digest(c.get("calibrationDigest")) or not valid_digest(c.get("tokenizerDigest")):
            return False
        if "unsupportedReason" in c and not isinstance(c["unsupportedReason"], str):
            return False
        if "unsupportedReason" in c and c["unsupportedReason"] == "":
            return False
        # File-level validation is handled per candidate so an invalid manifest
        # produces the required empty inventory rather than rejecting the whole freeze.
        if not isinstance(c["files"], dict):
            continue
    return len(names) == len(set(names))


def build_freeze(req: dict[str, Any]) -> dict[str, Any]:
    allowed = set(req["allowedUnsupportedReasons"])
    out_candidates = []
    for c in req["candidates"]:
        file_ok, inventory, total, digest = validate_files(c["files"])
        reason_codes: set[str] = set()
        unsupported_reason = c.get("unsupportedReason")

        if not file_ok:
            reason_codes.add("INVALID_INPUT")
        else:
            if unsupported_reason is not None and unsupported_reason in allowed:
                # An explicitly allowed unsupported reason makes the candidate
                # unsupported; loadability and lineage digests are not required.
                status = "unsupported"
            else:
                status = "frozen"
                if unsupported_reason is not None:
                    reason_codes.add("UNALLOWED_UNSUPPORTED_REASON")
                if not c["loadable"]:
                    reason_codes.add("NOT_LOADABLE")
                if c["calibrationDigest"] != req["calibrationDigest"]:
                    reason_codes.add("CALIBRATION_MISMATCH")
                if c["tokenizerDigest"] != req["tokenizerDigest"]:
                    reason_codes.add("TOKENIZER_MISMATCH")
                if reason_codes:
                    status = "invalid"

        if not file_ok:
            inventory_out: list[dict[str, Any]] = []
            total_out = None
            digest_out = None
            status = "invalid"
        else:
            inventory_out = inventory
            total_out = total
            digest_out = digest

        out_candidates.append({
            "name": c["name"],
            "status": status,
            "inventory": inventory_out,
            "totalBytes": total_out,
            "packageDigest": digest_out,
            "reasonCodes": code_sort(reason_codes),
        })

    out_candidates.sort(key=lambda x: utf8_key(x["name"]))
    return {"freezeId": req["freezeId"], "candidates": out_candidates}


def get_freeze(freeze_id: str) -> tuple[Any, Any] | None:
    row = DB.execute("SELECT request_json, response_json FROM freezes WHERE freeze_id = ?", (freeze_id,)).fetchone()
    if row is None:
        return None
    return json.loads(row[0]), json.loads(row[1])


def save_freeze(req: dict[str, Any], response: dict[str, Any]) -> None:
    request_json = json.dumps(req, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    response_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    with DB_LOCK:
        DB.execute(
            "INSERT INTO freezes(freeze_id, request_json, response_json) VALUES (?, ?, ?)",
            (req["freezeId"], request_json, response_json),
        )
        DB.commit()


def validate_policy(policy: Any) -> tuple[bool, dict[str, Any] | None]:
    if not isinstance(policy, dict):
        return False, None
    for k in ("maxBytes", "aggregateFloor", "requiredSlices", "maxLatencyMs", "candidateOrder"):
        if k not in policy:
            return False, None
    if not is_safe_nonnegative_integer(policy["maxBytes"]):
        return False, None
    if not is_finite_number(policy["aggregateFloor"]) or not 0 <= float(policy["aggregateFloor"]) <= 1:
        return False, None
    if not is_finite_number(policy["maxLatencyMs"]) or float(policy["maxLatencyMs"]) < 0:
        return False, None
    if not isinstance(policy["requiredSlices"], dict):
        return False, None
    for name, floor in policy["requiredSlices"].items():
        if not isinstance(name, str) or not name or not is_finite_number(floor) or not 0 <= float(floor) <= 1:
            return False, None
    if not validate_unique_nonempty_strings(policy["candidateOrder"]):
        return False, None
    return True, policy


def validate_select_request_shape(req: Any) -> bool:
    if not isinstance(req, dict) or req.get("phase") != "select":
        return False
    if not isinstance(req.get("freezeId"), str) or not req["freezeId"]:
        return False
    if not isinstance(req.get("candidates"), list) or not isinstance(req.get("rows"), list) or not isinstance(req.get("policy"), dict):
        return False
    return True


def recompute_manifest(candidate: dict[str, Any]) -> tuple[bool, int | None, str | None]:
    inv = candidate.get("inventory")
    if not isinstance(inv, list):
        return False, None, None
    names: list[str] = []
    recomputed: list[dict[str, Any]] = []
    for item in inv:
        if not isinstance(item, dict) or set(item.keys()) != {"name", "bytes", "sha256"}:
            return False, None, None
        if not isinstance(item["name"], str) or not item["name"]:
            return False, None, None
        if not is_safe_nonnegative_integer(item["bytes"]):
            return False, None, None
        if not isinstance(item["sha256"], str) or len(item["sha256"]) != 64 or any(ch not in "0123456789abcdef" for ch in item["sha256"]):
            return False, None, None
        names.append(item["name"])
        recomputed.append({"name": item["name"], "bytes": item["bytes"], "sha256": item["sha256"]})
    if len(names) != len(set(names)):
        return False, None, None
    if recomputed != sorted(recomputed, key=lambda e: utf8_key(e["name"])):
        return False, None, None
    total = sum(x["bytes"] for x in recomputed)
    digest = sha256_hex(compact_json_bytes(recomputed))
    if candidate.get("totalBytes") != total or candidate.get("packageDigest") != digest:
        return False, None, None
    return True, total, digest


def round12(x: float) -> float:
    return round(x, 12)


def valid_binary_prediction(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and (x == 0 or x == 1)


def latency_value(latencies: Any, name: str) -> tuple[bool, float | int | None]:
    if not isinstance(latencies, dict) or name not in latencies:
        return False, None
    value = latencies[name]
    if not is_finite_number(value) or float(value) < 0:
        return False, None
    return True, value


def select(req: dict[str, Any], stored_response: dict[str, Any]) -> dict[str, Any]:
    stored_candidates = stored_response["candidates"]
    submitted_candidates = req["candidates"]
    policy = req["policy"]
    ok_policy, policy_obj = validate_policy(policy)
    codes_global: set[str] = set()
    if not ok_policy:
        codes_global.add("INVALID_POLICY")

    stored_names = [c["name"] for c in stored_candidates]
    submitted_names = [c.get("name") if isinstance(c, dict) else None for c in submitted_candidates]
    order = policy.get("candidateOrder") if isinstance(policy, dict) else []
    if not validate_unique_nonempty_strings(order) or set(order) != set(stored_names) or set(order) != set(submitted_names):
        codes_global.add("INVALID_POLICY")

    stored_by_name = {c["name"]: c for c in stored_candidates}
    submitted_by_name = {c.get("name"): c for c in submitted_candidates if isinstance(c, dict)}
    rows = req["rows"]
    if not isinstance(rows, list):
        codes_global.add("INVALID_INPUT")

    results = []
    latencies = req.get("latencies")

    # Results must follow candidateOrder, with UTF-8 name as fallback.
    if stored_names:
        result_names = [x for x in stored_names if x in set(order)]
    else:
        result_names = [x for x in submitted_names if isinstance(x, str)]
    result_names = list(dict.fromkeys(result_names))
    result_names.sort(key=lambda n: (order.index(n) if n in order else len(order), utf8_key(n)))

    for name in result_names:
        c = stored_by_name.get(name)
        if c is None:
            c = {
                "name": name,
                "status": "invalid",
                "inventory": [],
                "totalBytes": None,
                "packageDigest": None,
                "reasonCodes": [],
            }
        sc = submitted_by_name.get(name)
        rcodes: set[str] = set(c.get("reasonCodes", []))
        aggregate = None
        slices: dict[str, Any] = {}
        total_bytes = None
        latency = None

        # The submitted candidate array must be byte-for-byte equivalent at the
        # JSON value level to the stored freeze response's candidate array.
        # Python's JSON object equality is appropriate here because object key
        # order is not semantic JSON data.
        if sc != c:
            rcodes.add("INVALID_LINEAGE")

        manifest_ok, recomputed_total, _ = recompute_manifest(c)
        if not manifest_ok:
            rcodes.add("INVALID_MANIFEST")
        else:
            total_bytes = recomputed_total

        # Frozen means usable lineage. Unsupported/invalid freeze candidates are
        # never eligible for admission.
        if c.get("status") != "frozen":
            rcodes.add("NOT_FROZEN")

        valid_predictions = True
        row_values: list[tuple[str, Any, Any]] = []
        if not isinstance(rows, list):
            valid_predictions = False
        else:
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("predictions"), dict):
                    valid_predictions = False
                    break
                if "label" not in row or "slice" not in row or not isinstance(row["slice"], str):
                    valid_predictions = False
                    break
                if not valid_binary_prediction(row["label"]):
                    valid_predictions = False
                    break
                if name not in row["predictions"] or not valid_binary_prediction(row["predictions"][name]):
                    valid_predictions = False
                    break
                row_values.append((row["slice"], row["label"], row["predictions"][name]))

        if not valid_predictions:
            rcodes.add("INVALID_PREDICTIONS")
        else:
            if len(row_values) == 0:
                # No rows means aggregate cannot be computed; treat it as an
                # unmet aggregate floor rather than inventing a score.
                aggregate = None
            else:
                correct = sum(1 for _, label, pred in row_values if pred == label)
                aggregate = round12(correct / len(row_values))

            required = policy_obj["requiredSlices"] if ok_policy else {}
            for slice_name in required:
                vals = [(label, pred) for s, label, pred in row_values if s == slice_name]
                if not vals:
                    slices[slice_name] = None
                    rcodes.add(f"MISSING_SLICE:{slice_name}")
                else:
                    acc = round12(sum(1 for label, pred in vals if pred == label) / len(vals))
                    slices[slice_name] = acc
                    if acc < float(required[slice_name]):
                        rcodes.add(f"SLICE_FLOOR:{slice_name}")

        if ok_policy and aggregate is not None and aggregate < float(policy_obj["aggregateFloor"]):
            rcodes.add("AGGREGATE_FLOOR")
        elif ok_policy and aggregate is None:
            rcodes.add("AGGREGATE_FLOOR")

        latency_ok, latency_value_result = latency_value(latencies, name)
        if latency_ok:
            latency = latency_value_result
            if ok_policy and float(latency) > float(policy_obj["maxLatencyMs"]):
                rcodes.add("LATENCY_LIMIT")
        else:
            latency = None
            rcodes.add("LATENCY_LIMIT")

        if ok_policy and total_bytes is not None and total_bytes > policy_obj["maxBytes"]:
            rcodes.add("SIZE_LIMIT")
        elif ok_policy and total_bytes is None:
            rcodes.add("SIZE_LIMIT")

        # Keep required slice output deterministic in UTF-8 key order.
        slices = {k: slices[k] for k in sorted(slices, key=utf8_key)}
        results.append({
            "name": name,
            "aggregate": aggregate,
            "slices": slices,
            "totalBytes": total_bytes,
            "latencyMs": latency,
            "admitted": len(rcodes) == 0,
            "reasonCodes": code_sort(rcodes),
        })

    # If candidate sets/policy are globally malformed, no candidate can be admitted.
    if codes_global:
        for r in results:
            r["admitted"] = False
            r["reasonCodes"] = code_sort(set(r["reasonCodes"]) | codes_global)

    admitted = [r for r in results if r["admitted"]]
    winner = None
    if admitted:
        order_pos = {name: i for i, name in enumerate(order)}
        winner = min(
            admitted,
            key=lambda r: (
                r["totalBytes"],
                float(r["latencyMs"]),
                order_pos.get(r["name"], len(order)),
                utf8_key(r["name"]),
            ),
        )

    return {
        "freezeId": req["freezeId"],
        "selected": winner["name"] if winner else None,
        "results": results,
        "packageManifest": stored_by_name.get(winner["name"]) if winner else None,
    }


@app.post("/quantize")
async def quantize(request: Request):
    try:
        raw = await request.body()
        try:
            body = parse_json_string(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return invalid_input()
    except Exception:
        return invalid_input()

    if not isinstance(body, dict):
        return invalid_input()

    phase = body.get("phase")
    if phase == "freeze":
        if not freeze_request_valid(body):
            return invalid_input()

        freeze_id = body["freezeId"]
        with DB_LOCK:
            existing = get_freeze(freeze_id)
            if existing is not None:
                old_request, old_response = existing
                if old_request == body:
                    return JSONResponse(old_response)
                return conflict()

            response = build_freeze(body)
            request_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            response_json = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            DB.execute(
                "INSERT INTO freezes(freeze_id, request_json, response_json) VALUES (?, ?, ?)",
                (freeze_id, request_json, response_json),
            )
            DB.commit()
            return JSONResponse(response)

    if phase == "select":
        if not validate_select_request_shape(body):
            return invalid_input()
        existing = get_freeze(body["freezeId"])
        if existing is None:
            # A select for an unknown ID is a valid select-shaped request. The
            # submitted candidates are still evaluated, but none can be admitted.
            stored_response = {"freezeId": body["freezeId"], "candidates": []}
        else:
            _, stored_response = existing

        return JSONResponse(select(body, stored_response))

    return invalid_input()


@app.get("/health")
def health():
    return {"status": "ok"}
