import logging
from typing import Any, Dict, List, Optional
from flask import Flask, jsonify, request

app = Flask(__name__)
logger = logging.getLogger("main")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s")

# In-memory store for frozen sessions: freezeId -> dict of candidate states
FREEZE_STORE: Dict[str, Dict[str, Any]] = {}


# ==========================================
# Phase 1: Freeze Validation & Processing
# ==========================================

def validate_candidate(cand: Dict[str, Any], top_cal: str, top_tok: str, allowed_reasons: List[str]) -> Dict[str, Any]:
    """Validates an individual candidate and assigns its freeze status."""
    name = cand.get("name")
    loadable = cand.get("loadable", False)
    cal_digest = cand.get("calibrationDigest")
    tok_digest = cand.get("tokenizerDigest")
    unsupported_reason = cand.get("unsupportedReason")

    # Loadable candidate requirement: digests must match top-level request
    if loadable:
        if cal_digest == top_cal and tok_digest == top_tok:
            return {**cand, "status": "frozen"}
        return {**cand, "status": "invalid", "reason": "DIGEST_MISMATCH"}

    # Non-loadable candidate requirement: unsupportedReason must be explicitly allowed
    if unsupported_reason and unsupported_reason in allowed_reasons:
        return {**cand, "status": "frozen"}

    return {**cand, "status": "invalid", "reason": "UNALLOWED_REASON_OR_BAD_INPUT"}


def process_freeze(payload: Dict[str, Any]):
    """Processes freeze phase and returns (response_dict, status_code)."""
    freeze_id = payload.get("freezeId")
    candidates = payload.get("candidates")

    # Strict Validation: Reject missing, non-list, or empty candidate list
    if not isinstance(candidates, list) or len(candidates) == 0:
        logger.warning("Freeze validation failed: 'candidates' field missing, not a list, or empty.")
        logger.warning("Freeze phase validation failed inside /quantize endpoint.")
        return {"status": "error", "message": "'candidates' field missing, not a list, or empty."}, 400

    top_cal = payload.get("calibrationDigest", "")
    top_tok = payload.get("tokenizerDigest", "")
    allowed_reasons = payload.get("allowedUnsupportedReasons", [])

    processed_candidates = []
    for cand in candidates:
        validated = validate_candidate(cand, top_cal, top_tok, allowed_reasons)
        processed_candidates.append(validated)

    # Persist frozen state in memory for subsequent 'select' phase calls
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

def validate_policy_payload(policy: Dict[str, Any]) -> Optional[str]:
    """Validates policy structure and bounds."""
    max_bytes = policy.get("maxBytes", policy.get("maxTotalBytes"))
    if max_bytes is not None and max_bytes < 0:
        logger.warning(f"Policy validation failed: Invalid maxBytes/maxTotalBytes '{max_bytes}'.")
        return f"Invalid maxBytes/maxTotalBytes '{max_bytes}'."
    return None


def evaluate_accuracy(candidate_name: str, rows: List[Dict[str, Any]], policy: Dict[str, Any]) -> bool:
    """Evaluates aggregate and per-slice accuracy thresholds for a candidate."""
    if not rows:
        return True

    slice_counts: Dict[str, List[int]] = {}
    total_correct = 0
    total_rows = len(rows)

    for row in rows:
        label = row.get("label")
        slice_name = row.get("slice")
        pred = row.get("predictions", {}).get(candidate_name)

        is_correct = (pred == label)
        if is_correct:
            total_correct += 1

        if slice_name:
            if slice_name not in slice_counts:
                slice_counts[slice_name] = [0, 0]
            slice_counts[slice_name][1] += 1
            if is_correct:
                slice_counts[slice_name][0] += 1

    # Aggregate accuracy floor check
    aggregate_acc = total_correct / total_rows
    if aggregate_acc < policy.get("aggregateFloor", 0.0):
        return False

    # Per-slice accuracy checks
    required_slices = policy.get("requiredSlices", {})
    for slice_name, min_acc in required_slices.items():
        if slice_name in slice_counts:
            correct, total = slice_counts[slice_name]
            acc = correct / total if total > 0 else 0.0
            if acc < min_acc:
                return False

    return True


def select_candidate(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluates valid candidates in candidateOrder and selects the optimal one."""
    policy = payload.get("policy", {})
    latencies = payload.get("latencies", {})
    rows = payload.get("rows", [])
    freeze_id = payload.get("freezeId")

    # Fetch stored candidates from freeze phase, or fallback to request body
    candidates = FREEZE_STORE.get(freeze_id, {}).get("candidates", payload.get("candidates", []))
    candidate_map = {c["name"]: c for c in candidates}
    candidate_order = policy.get("candidateOrder", [])

    max_bytes = policy.get("maxBytes", policy.get("maxTotalBytes", float("inf")))
    max_latency = policy.get("maxLatencyMs", float("inf"))

    for name in candidate_order:
        cand = candidate_map.get(name)
        if not cand:
            continue

        # Rule 1: Candidate status must be 'frozen' (loadable & valid)
        if cand.get("status") != "frozen":
            continue

        # Rule 2: Byte budget constraint
        total_bytes = cand.get("totalBytes")
        if total_bytes is not None and total_bytes > max_bytes:
            continue

        # Rule 3: Latency constraint
        if latencies.get(name, float("inf")) > max_latency:
            continue

        # Rule 4: Accuracy constraints
        if not evaluate_accuracy(name, rows, policy):
            continue

        return cand

    return None


# ==========================================
# Primary API Route
# ==========================================

@app.route("/quantize", methods=["POST"])
def quantize():
    payload = request.get_json(force=True) or {}
    logger.info(f"POST /quantize raw body: {request.get_data(as_text=True)}")

    phase = payload.get("phase")

    if phase == "freeze":
        res, status_code = process_freeze(payload)
        return jsonify(res), status_code

    elif phase == "select":
        policy = payload.get("policy", {})
        err = validate_policy_payload(policy)
        if err:
            return jsonify({"status": "error", "message": err}), 400

        selected = select_candidate(payload)
        if not selected:
            return jsonify({"status": "no_candidate_selected", "selected": None}), 200

        return jsonify({"status": "selected", "selected": selected["name"], "candidate": selected}), 200

    return jsonify({"status": "error", "message": f"Unknown phase '{phase}'"}), 400


if __name__ == "__main__":
    app.run(port=5000)
