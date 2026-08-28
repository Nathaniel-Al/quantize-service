# main.py
import logging
from typing import Any, Dict, List, Optional
from flask import Flask, jsonify, request

app = Flask(__name__)
logger = logging.getLogger("main")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s")


# ==========================================
# Validation Helpers
# ==========================================

def validate_freeze_payload(payload: Dict[str, Any]) -> Optional[str]:
    """Validates freeze phase payload."""
    candidates = payload.get("candidates")
    if candidates is None or not isinstance(candidates, list) or len(candidates) == 0:
        logger.warning("Freeze validation failed: 'candidates' field missing, not a list, or empty.")
        return "'candidates' field missing, not a list, or empty."
    return None


def validate_policy_payload(policy: Dict[str, Any]) -> Optional[str]:
    """Validates selection policy parameters."""
    max_bytes = policy.get("maxBytes", policy.get("maxTotalBytes"))
    if max_bytes is not None and max_bytes < 0:
        logger.warning(f"Policy validation failed: Invalid maxBytes/maxTotalBytes '{max_bytes}'.")
        return f"Invalid maxBytes/maxTotalBytes '{max_bytes}'."
    return None


# ==========================================
# Accuracy & Selection Logic
# ==========================================

def evaluate_accuracy(candidate_name: str, rows: List[Dict[str, Any]], policy: Dict[str, Any]) -> bool:
    """Evaluates aggregate and per-slice accuracy thresholds for a candidate."""
    if not rows:
        return True

    slice_counts: Dict[str, List[int]] = {}  # slice_name -> [correct_count, total_count]
    total_correct = 0
    total_rows = len(rows)

    for row in rows:
        label = row.get("label")
        slice_name = row.get("slice")
        pred = row.get("predictions", {}).get(candidate_name)

        is_correct = pred == label
        if is_correct:
            total_correct += 1

        if slice_name:
            if slice_name not in slice_counts:
                slice_counts[slice_name] = [0, 0]
            slice_counts[slice_name][1] += 1
            if is_correct:
                slice_counts[slice_name][0] += 1

    # 1. Check Aggregate Accuracy Floor
    aggregate_acc = total_correct / total_rows
    if aggregate_acc < policy.get("aggregateFloor", 0.0):
        return False

    # 2. Check Per-Slice Thresholds
    required_slices = policy.get("requiredSlices", {})
    for slice_name, min_acc in required_slices.items():
        if slice_name in slice_counts:
            correct, total = slice_counts[slice_name]
            acc = correct / total if total > 0 else 0.0
            if acc < min_acc:
                return False

    return True


def select_candidate(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluates candidates in candidateOrder and picks the first passing candidate."""
    policy = payload.get("policy", {})
    latencies = payload.get("latencies", {})
    rows = payload.get("rows", [])
    candidates = payload.get("candidates", [])

    candidate_map = {c["name"]: c for c in candidates}
    candidate_order = policy.get("candidateOrder", [])

    max_bytes = policy.get("maxBytes", policy.get("maxTotalBytes", float("inf")))
    max_latency = policy.get("maxLatencyMs", float("inf"))

    for name in candidate_order:
        cand = candidate_map.get(name)
        if not cand:
            continue

        # Rule 1: Candidate must be frozen (loadable & valid)
        if cand.get("status") != "frozen":
            continue

        # Rule 2: Byte budget constraint
        total_bytes = cand.get("totalBytes")
        if total_bytes is None or total_bytes > max_bytes:
            continue

        # Rule 3: Latency constraint
        if latencies.get(name, float("inf")) > max_latency:
            continue

        # Rule 4: Accuracy checks (aggregate + per-slice)
        if not evaluate_accuracy(name, rows, policy):
            continue

        return cand

    return None


# ==========================================
# Endpoint Handler
# ==========================================

@app.route("/quantize", methods=["POST"])
def quantize():
    payload = request.get_json(force=True) or {}
    phase = payload.get("phase")

    logger.info(f"POST /quantize raw body: {request.get_data(as_text=True)}")

    if phase == "freeze":
        err = validate_freeze_payload(payload)
        if err:
            logger.warning("Freeze phase validation failed inside /quantize endpoint.")
            return jsonify({"status": "error", "message": err}), 400
        
        return jsonify({"status": "frozen", "freezeId": payload.get("freezeId")}), 200

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
