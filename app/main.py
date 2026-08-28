import logging
from typing import Any, Dict, List, Optional
from flask import Flask, jsonify, request

app = Flask(__name__)
logger = logging.getLogger("main")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s")

FREEZE_STORE: Dict[str, Dict[str, Any]] = {}


# ==========================================
# Phase 1: Freeze Validation & Processing
# ==========================================

def validate_candidate(cand: Dict[str, Any], top_cal: str, top_tok: str, allowed_reasons: List[str]) -> Dict[str, Any]:
    """Validates an individual candidate and assigns its freeze status."""
    loadable = cand.get("loadable", False)
    cal_digest = cand.get("calibrationDigest")
    tok_digest = cand.get("tokenizerDigest")
    unsupported_reason = cand.get("unsupportedReason")

    if loadable:
        if cal_digest == top_cal and tok_digest == top_tok:
            return {**cand, "status": "frozen"}
        return {**cand, "status": "invalid", "reason": "DIGEST_MISMATCH"}

    if unsupported_reason and unsupported_reason in allowed_reasons:
        return {**cand, "status": "unsupported"}

    return {**cand, "status": "invalid", "reason": "UNALLOWED_REASON_OR_BAD_INPUT"}


def process_freeze(payload: Dict[str, Any]):
    """Processes freeze phase and returns (response_dict, status_code)."""
    freeze_id = payload.get("freezeId")
    candidates = payload.get("candidates")

    if not isinstance(candidates, list) or len(candidates) == 0:
        logger.warning("Freeze validation failed: 'candidates' field missing, not a list, or empty.")
        return {"status": "error", "message": "'candidates' field missing, not a list, or empty."}, 400

    top_cal = payload.get("calibrationDigest", "")
    top_tok = payload.get("tokenizerDigest", "")
    allowed_reasons = payload.get("allowedUnsupportedReasons", [])

    processed_candidates = []
    for cand in candidates:
        validated = validate_candidate(cand, top_cal, top_tok, allowed_reasons)
        processed_candidates.append(validated)

    if freeze_id:
        FREEZE_STORE[freeze_id] = {
            "freezeId": freeze_id,
            "candidates": processed_candidates,
            "allowedUnsupportedReasons": allowed_reasons,
        }

    return {
        "status": "frozen",
        "freezeId": freeze_id,
        "candidates": processed_candidates
    }, 200


# ==========================================
# Phase 2: Selection Logic
# ==========================================

def sanitize_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitizes policy bounds by resetting negative byte constraints to unbounded (None)."""
    sanitized = dict(policy) if isinstance(policy, dict) else {}

    for byte_key in ("maxBytes", "maxTotalBytes"):
        if byte_key in sanitized:
            val = sanitized[byte_key]
            if val is not None and (not isinstance(val, (int, float)) or val < 0):
                logger.info(f"Sanitizing negative or invalid '{byte_key}' ({val}) to unbounded (None).")
                sanitized[byte_key] = None

    return sanitized


def validate_policy_payload(policy: Dict[str, Any]) -> Optional[str]:
    """Validates policy structure."""
    if not isinstance(policy, dict):
        return "Policy must be a JSON object."
    return None


def evaluate_accuracy(candidate_name: str, rows: List[Dict[str, Any]], policy: Dict[str, Any]) -> bool:
    """Evaluates aggregate and per-slice accuracy thresholds for a candidate safely handling missing keys."""
    if not rows:
        return True

    slice_counts: Dict[str, List[int]] = {}
    total_correct = 0
    total_rows = len(rows)

    for row in rows:
        label = row.get("label")
        slice_name = row.get("slice")
        predictions = row.get("predictions", {})

        # Handle missing prediction entries explicitly
        if candidate_name not in predictions:
            is_correct = False
        else:
            is_correct = (predictions[candidate_name] == label)

        if is_correct:
            total_correct += 1

        if slice_name:
            if slice_name not in slice_counts:
                slice_counts[slice_name] = [0, 0]
            slice_counts[slice_name][1] += 1
            if is_correct:
                slice_counts[slice_name][0] += 1

    aggregate_acc = total_correct / total_rows
    if aggregate_acc < policy.get("aggregateFloor", 0.0):
        return False

    required_slices = policy.get("requiredSlices", {})
    for slice_name, min_acc in required_slices.items():
        if slice_name in slice_counts:
            correct, total = slice_counts[slice_name]
            acc = correct / total if total > 0 else 0.0
            if acc < min_acc:
                return False

    return True


def select_candidate(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluates valid candidates in candidateOrder and selects the optimal candidate."""
    policy = sanitize_policy(payload.get("policy", {}))
    latencies = payload.get("latencies", {})
    rows = payload.get("rows") or []
    freeze_id = payload.get("freezeId")

    payload_candidates = payload.get("candidates")
    stored_candidates = FREEZE_STORE.get(freeze_id, {}).get("candidates")

    # Merge or select candidate source prioritizing request payload when explicit status exists
    if payload_candidates is not None:
        candidates = payload_candidates
    elif stored_candidates is not None:
        candidates = stored_candidates
    else:
        candidates = []

    candidate_map = {c.get("name"): c for c in candidates if isinstance(c, dict) and "name" in c}
    candidate_order = policy.get("candidateOrder", [])

    max_bytes = policy.get("maxBytes")
    if max_bytes is None:
        max_bytes = policy.get("maxTotalBytes")
    if max_bytes is None:
        max_bytes = float("inf")

    max_latency = policy.get("maxLatencyMs")
    if max_latency is None:
        max_latency = float("inf")

    for name in candidate_order:
        cand = candidate_map.get(name)
        if not cand:
            continue

        # Status check: candidate must be explicitly marked frozen
        if cand.get("status") != "frozen":
            continue

        # Byte constraint check
        total_bytes = cand.get("totalBytes")
        if total_bytes is not None and total_bytes > max_bytes:
            continue

        # Latency constraint check
        cand_latency = latencies.get(name, float("inf"))
        if cand_latency > max_latency:
            continue

        # Accuracy constraint check
        if not evaluate_accuracy(name, rows, policy):
            continue

        return cand

    return None


# ==========================================
# Primary API Route
# ==========================================

@app.route("/quantize", methods=["POST"])
def quantize():
    payload = request.get_json(force=True, silent=True) or {}
    logger.info(f"POST /quantize raw body: {request.get_data(as_text=True)}")

    phase = payload.get("phase")

    if phase == "freeze":
        res, status_code = process_freeze(payload)
        return jsonify(res), status_code

    elif phase == "select":
        raw_policy = payload.get("policy", {})
        err = validate_policy_payload(raw_policy)
        if err:
            return jsonify({"status": "error", "message": err}), 400

        selected = select_candidate(payload)
        if not selected:
            return jsonify({"status": "no_candidate_selected", "selected": None}), 200

        return jsonify({"status": "selected", "selected": selected["name"], "candidate": selected}), 200

    return jsonify({"status": "error", "message": f"Unknown phase '{phase}'"}), 400


if __name__ == "__main__":
    app.run(port=5000)
