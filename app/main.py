"""
Stateful two-phase candidate-admission API.

Endpoint: POST /quantize

Phase 1 ("freeze"): validates and hashes candidate file sets, persists the
result keyed by freezeId.

Phase 2 ("select"): re-validates the previously frozen candidates against a
grading policy + label/prediction rows, and picks a winner.

State is kept in-memory (a single process). That's fine for the grading
contract described (freezeId reuse within the same running service), but it
means state does not survive a restart / is not shared across workers.
"""

import hashlib
import json
import math
import threading
from flask import Flask, jsonify, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory store: freezeId -> {"input": <original freeze request dict>,
#                                "response": <computed freeze response dict>}
# ---------------------------------------------------------------------------
_STORE = {}
_STORE_LOCK = threading.Lock()

MAX_SAFE_INTEGER = 2 ** 53 - 1

FREEZE_CODES = {
    "UNALLOWED_UNSUPPORTED_REASON",
    "NOT_LOADABLE",
    "CALIBRATION_MISMATCH",
    "TOKENIZER_MISMATCH",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def is_finite_number(x):
    """True for real, finite, non-bool int/float."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def is_binary(x):
    """True for exactly 0 or 1 (not bool, not float)."""
    return isinstance(x, int) and not isinstance(x, bool) and x in (0, 1)


def utf8_sort_key(s):
    return s.encode("utf-8")


def compute_package_digest(inventory):
    """inventory: list of {"name":..., "bytes":..., "sha256":...} already
    sorted by UTF-8 filename. Produces compact JSON with exact key order
    name,bytes,sha256, then sha256-hashes the UTF-8 bytes of that JSON."""
    arr = [
        {"name": item["name"], "bytes": item["bytes"], "sha256": item["sha256"]}
        for item in inventory
    ]
    compact = json.dumps(arr, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def compute_inventory(files):
    """files: dict[filename] -> string content. Returns inventory sorted by
    UTF-8 filename bytes, with exact UTF-8 byte length + lowercase sha256."""
    items = []
    for name, content in files.items():
        raw = content.encode("utf-8")
        items.append(
            {
                "name": name,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    items.sort(key=lambda item: utf8_sort_key(item["name"]))
    return items


# ---------------------------------------------------------------------------
# FREEZE phase
# ---------------------------------------------------------------------------

def validate_freeze_structure(body):
    if not isinstance(body.get("freezeId"), str):
        return False
    if not (1 <= len(body["freezeId"]) <= 128):
        return False
    if not isinstance(body.get("calibrationDigest"), str) or not body["calibrationDigest"]:
        return False
    if not isinstance(body.get("tokenizerDigest"), str) or not body["tokenizerDigest"]:
        return False

    allowed = body.get("allowedUnsupportedReasons")
    if not isinstance(allowed, list):
        return False
    if not all(isinstance(a, str) and a for a in allowed):
        return False
    if len(set(allowed)) != len(allowed):
        return False

    candidates = body.get("candidates")
    if not isinstance(candidates, list) or len(candidates) == 0:
        return False

    names = []
    for c in candidates:
        if not isinstance(c, dict):
            return False
        name = c.get("name")
        if not isinstance(name, str) or not name:
            return False
        names.append(name)

        files = c.get("files")
        if not isinstance(files, dict) or len(files) == 0:
            return False
        for fname, fcontent in files.items():
            if not isinstance(fname, str) or not fname:
                return False
            if not isinstance(fcontent, str):
                return False

        if not isinstance(c.get("loadable"), bool):
            return False
        if not isinstance(c.get("calibrationDigest"), str):
            return False
        if not isinstance(c.get("tokenizerDigest"), str):
            return False

        if "unsupportedReason" in c and c["unsupportedReason"] is not None:
            if not isinstance(c["unsupportedReason"], str) or not c["unsupportedReason"]:
                return False

    if len(set(names)) != len(names):
        return False

    return True


def compute_freeze_candidate(c, body, allowed_set):
    reason_codes = []
    unsupported_reason = c.get("unsupportedReason") or None

    if unsupported_reason:
        if unsupported_reason in allowed_set:
            status = "unsupported"
        else:
            status = "invalid"
            reason_codes.append("UNALLOWED_UNSUPPORTED_REASON")
    else:
        codes = []
        if not c.get("loadable"):
            codes.append("NOT_LOADABLE")
        if c.get("calibrationDigest") != body["calibrationDigest"]:
            codes.append("CALIBRATION_MISMATCH")
        if c.get("tokenizerDigest") != body["tokenizerDigest"]:
            codes.append("TOKENIZER_MISMATCH")
        if codes:
            status = "invalid"
            reason_codes = codes
        else:
            status = "frozen"

    reason_codes = sorted(reason_codes, key=utf8_sort_key)

    if status == "invalid":
        inventory = []
        total_bytes = None
        package_digest = None
    else:
        try:
            inventory = compute_inventory(c["files"])
            total_bytes = sum(item["bytes"] for item in inventory)
            package_digest = compute_package_digest(inventory)
        except Exception:
            # Defensive fallback: malformed files -> treat as invalid.
            status = "invalid"
            inventory = []
            total_bytes = None
            package_digest = None

    return {
        "name": c["name"],
        "status": status,
        "inventory": inventory,
        "totalBytes": total_bytes,
        "packageDigest": package_digest,
        "reasonCodes": reason_codes,
    }


def compute_freeze_response(body):
    allowed_set = set(body.get("allowedUnsupportedReasons", []))
    results = [compute_freeze_candidate(c, body, allowed_set) for c in body["candidates"]]
    results.sort(key=lambda r: utf8_sort_key(r["name"]))
    return {"freezeId": body["freezeId"], "candidates": results}


def handle_freeze(body):
    freeze_id = body["freezeId"]
    with _STORE_LOCK:
        existing = _STORE.get(freeze_id)
        if existing is not None:
            if existing["input"] == body:
                return 200, existing["response"]
            return 409, {"error": "FREEZE_ID_CONFLICT"}

        response = compute_freeze_response(body)
        _STORE[freeze_id] = {"input": body, "response": response}
        return 200, response


# ---------------------------------------------------------------------------
# SELECT phase
# ---------------------------------------------------------------------------

def validate_policy(policy, name_set, latencies):
    if not isinstance(policy, dict):
        return False

    max_bytes = policy.get("maxBytes")
    if not (isinstance(max_bytes, int) and not isinstance(max_bytes, bool)):
        return False
    if not (0 <= max_bytes <= MAX_SAFE_INTEGER):
        return False

    aggregate_floor = policy.get("aggregateFloor")
    if not is_finite_number(aggregate_floor) or not (0 <= aggregate_floor <= 1):
        return False

    required_slices = policy.get("requiredSlices")
    if not isinstance(required_slices, dict):
        return False
    for v in required_slices.values():
        if not is_finite_number(v) or not (0 <= v <= 1):
            return False

    max_latency = policy.get("maxLatencyMs")
    if not is_finite_number(max_latency) or max_latency < 0:
        return False

    candidate_order = policy.get("candidateOrder")
    if not isinstance(candidate_order, list) or len(candidate_order) == 0:
        return False
    if not all(isinstance(x, str) and x for x in candidate_order):
        return False
    if len(set(candidate_order)) != len(candidate_order):
        return False
    if not name_set or set(candidate_order) != name_set:
        return False

    if not isinstance(latencies, dict):
        return False
    for n in name_set:
        v = latencies.get(n)
        if not is_finite_number(v) or v < 0:
            return False

    return True


def recompute_manifest(base_candidate):
    """Recomputes totalBytes/packageDigest from the candidate's inventory,
    never trusting the submitted/declared totals. Returns
    (manifest_valid, total_bytes_or_None, package_digest_or_None)."""
    inventory = base_candidate.get("inventory")
    if not isinstance(inventory, list) or len(inventory) == 0:
        return False, None, None
    try:
        names = []
        total = 0
        for item in inventory:
            if not isinstance(item, dict):
                return False, None, None
            n = item.get("name")
            b = item.get("bytes")
            s = item.get("sha256")
            if not isinstance(n, str) or not n:
                return False, None, None
            if not (isinstance(b, int) and not isinstance(b, bool) and b >= 0):
                return False, None, None
            if not isinstance(s, str) or not s:
                return False, None, None
            names.append(n)
            total += b

        if len(set(names)) != len(names):
            return False, None, None
        if names != sorted(names, key=utf8_sort_key):
            return False, None, None

        digest = compute_package_digest(inventory)

        if total != base_candidate.get("totalBytes"):
            return False, None, None
        if digest != base_candidate.get("packageDigest"):
            return False, None, None

        return True, total, digest
    except Exception:
        return False, None, None


def compute_predictions(rows, name, required_slice_names):
    """Returns (valid, aggregate, slices_dict). slices_dict always contains
    an entry for every slice actually observed for this candidate (used to
    detect MISSING_SLICE); aggregate/slices are None when invalid."""
    if not isinstance(rows, list) or len(rows) == 0:
        return False, None, None

    total = 0
    correct = 0
    slice_total = {}
    slice_correct = {}

    for row in rows:
        if not isinstance(row, dict):
            return False, None, None
        label = row.get("label")
        if not is_binary(label):
            return False, None, None
        preds = row.get("predictions")
        if not isinstance(preds, dict) or name not in preds:
            return False, None, None
        p = preds[name]
        if not is_binary(p):
            return False, None, None

        total += 1
        hit = 1 if p == label else 0
        correct += hit

        slice_name = row.get("slice")
        if isinstance(slice_name, str) and slice_name:
            slice_total[slice_name] = slice_total.get(slice_name, 0) + 1
            slice_correct[slice_name] = slice_correct.get(slice_name, 0) + hit

    if total == 0:
        return False, None, None

    aggregate = round(correct / total, 12)
    slices = {s: round(slice_correct[s] / slice_total[s], 12) for s in slice_total}
    return True, aggregate, slices


def validate_select_structure(body):
    if not isinstance(body.get("freezeId"), str) or not body["freezeId"]:
        return False
    if not isinstance(body.get("candidates"), list):
        return False
    if not isinstance(body.get("rows"), list):
        return False
    if not isinstance(body.get("policy"), dict):
        return False
    return True


def handle_select(body):
    freeze_id = body["freezeId"]
    submitted_candidates = body["candidates"]
    rows = body["rows"]
    policy = body["policy"]
    latencies = body.get("latencies")
    if not isinstance(latencies, dict):
        latencies = {}

    with _STORE_LOCK:
        frozen_entry = _STORE.get(freeze_id)

    not_frozen = frozen_entry is None
    stored_candidates = frozen_entry["response"]["candidates"] if frozen_entry else []
    stored_by_name = {c["name"]: c for c in stored_candidates}

    lineage_matches_whole = (not not_frozen) and (submitted_candidates == stored_candidates)

    submitted_by_name = {}
    valid_submitted_shape = True
    submitted_names = []
    for c in submitted_candidates:
        if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"]:
            submitted_names.append(c["name"])
            submitted_by_name[c["name"]] = c
        else:
            valid_submitted_shape = False

    unique_submitted_names = (
        valid_submitted_shape
        and len(submitted_names) == len(set(submitted_names))
        and len(submitted_names) > 0
    )

    name_set = set(submitted_names) if unique_submitted_names else set()
    if not name_set and isinstance(policy.get("candidateOrder"), list):
        name_set = {n for n in policy["candidateOrder"] if isinstance(n, str) and n}
    if not name_set and not not_frozen:
        name_set = set(stored_by_name.keys())

    policy_valid = validate_policy(policy, name_set, latencies)

    results = []
    for name in name_set:
        reason_codes = set()

        if not_frozen:
            reason_codes.add("NOT_FROZEN")

        stored_c = stored_by_name.get(name)
        if not not_frozen and stored_c is None:
            reason_codes.add("INVALID_LINEAGE")
        elif not not_frozen and not lineage_matches_whole:
            # whole submitted array didn't match the stored record exactly
            submitted_c = submitted_by_name.get(name)
            if submitted_c != stored_c:
                reason_codes.add("INVALID_LINEAGE")

        base_c = stored_c if stored_c is not None else submitted_by_name.get(name, {})

        if not not_frozen and base_c.get("status") != "frozen":
            reason_codes.add("NOT_FROZEN")

        manifest_valid, recomputed_bytes, _ = recompute_manifest(base_c)
        if not manifest_valid:
            reason_codes.add("INVALID_MANIFEST")

        if not policy_valid:
            reason_codes.add("INVALID_POLICY")

        required_slices = policy.get("requiredSlices") if isinstance(policy.get("requiredSlices"), dict) else {}
        preds_valid, aggregate, slice_accs = compute_predictions(rows, name, required_slices)
        if not preds_valid:
            reason_codes.add("INVALID_PREDICTIONS")

        if preds_valid and policy_valid:
            if aggregate < policy["aggregateFloor"]:
                reason_codes.add("AGGREGATE_FLOOR")
            for sname, sfloor in required_slices.items():
                if sname not in slice_accs:
                    reason_codes.add("MISSING_SLICE:%s" % sname)
                elif slice_accs[sname] < sfloor:
                    reason_codes.add("SLICE_FLOOR:%s" % sname)

        total_bytes_out = recomputed_bytes if manifest_valid else None
        if policy_valid and manifest_valid:
            if total_bytes_out > policy["maxBytes"]:
                reason_codes.add("SIZE_LIMIT")

        latency_val = latencies.get(name)
        latency_ok = is_finite_number(latency_val) and latency_val >= 0
        latency_out = latency_val if (policy_valid and latency_ok) else None
        if policy_valid and latency_ok:
            if latency_val > policy["maxLatencyMs"]:
                reason_codes.add("LATENCY_LIMIT")

        codes_sorted = sorted(reason_codes, key=utf8_sort_key)
        admitted = len(codes_sorted) == 0

        results.append(
            {
                "name": name,
                "aggregate": aggregate if preds_valid else None,
                "slices": slice_accs if preds_valid else None,
                "totalBytes": total_bytes_out,
                "latencyMs": latency_out,
                "admitted": admitted,
                "reasonCodes": codes_sorted,
            }
        )

    if policy_valid:
        order_list = policy["candidateOrder"]
    else:
        order_list = sorted(name_set, key=utf8_sort_key)
    order_index = {n: i for i, n in enumerate(order_list)}
    results.sort(key=lambda r: order_index.get(r["name"], len(order_index)))

    admitted_results = [r for r in results if r["admitted"]]
    selected = None
    if admitted_results:
        admitted_sorted = sorted(
            admitted_results,
            key=lambda r: (r["totalBytes"], r["latencyMs"], order_index.get(r["name"], len(order_index))),
        )
        selected = admitted_sorted[0]["name"]

    package_manifest = stored_by_name.get(selected) if selected else None

    return 200, {
        "freezeId": freeze_id,
        "selected": selected,
        "results": results,
        "packageManifest": package_manifest,
    }


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

@app.route("/quantize", methods=["POST"])
def quantize():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "INVALID_INPUT"}), 400

    phase = body.get("phase")

    if phase == "freeze":
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or len(candidates) == 0:
            return jsonify({"error": "INVALID_INPUT"}), 400
        if not validate_freeze_structure(body):
            return jsonify({"error": "INVALID_INPUT"}), 400
        status_code, resp = handle_freeze(body)
        return jsonify(resp), status_code

    if phase == "select":
        if not validate_select_structure(body):
            return jsonify({"error": "INVALID_INPUT"}), 400
        status_code, resp = handle_select(body)
        return jsonify(resp), status_code

    return jsonify({"error": "INVALID_INPUT"}), 400


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    # Only run the Flask development server when executing the module directly.
    app.run(host="0.0.0.0", port=port)
