import logging
from typing import Any, Dict, List, Optional
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


def compute_slice_accuracies(rows: List[Dict[str, Any]], candidate_name: str) -> Dict[str, float]:
    """Computes prediction accuracy per slice for a given candidate."""
    slice_counts: Dict[str, int] = {}
    slice_correct: Dict[str, int] = {}

    for row in rows:
        label = row.get("label")
        slice_name = row.get("slice", "default")
        predictions = row.get("predictions", {})

        if candidate_name not in predictions:
            continue

        slice_counts[slice_name] = slice_counts.get(slice_name, 0) + 1
        if predictions[candidate_name] == label:
            slice_correct[slice_name] = slice_correct.get(slice_name, 0) + 1

    return {
        s: slice_correct[s] / slice_counts[s]
        for s in slice_counts
        if slice_counts[s] > 0
    }


def evaluate_select_candidate(
    candidate: Dict[str, Any],
    policy: Dict[str, Any],
    latencies: Dict[str, float],
    rows: List[Dict[str, Any]],
) -> bool:
    """Checks if a candidate satisfies all policy constraints."""
    name = candidate.get("name")
    status = candidate.get("status")

    # 1. Candidate must be frozen and valid
    if status != "frozen":
        return False

    # 2. Check latency constraint (cand_latency <= maxLatencyMs)
    max_latency = policy.get("maxLatencyMs")
    if max_latency is not None:
        cand_latency = latencies.get(name)
        if cand_latency is None or cand_latency > max_latency:
            return False

    # 3. Check memory / byte limit constraint (totalBytes <= maxBytes)
    max_bytes = policy.get("maxBytes")
    # Sanitize negative or zero maxBytes to unbounded (None)
    if max_bytes is not None and max_bytes <= 0:
        max_bytes = None

    if max_bytes is not None:
        total_bytes = candidate.get("totalBytes")
        if total_bytes is None or total_bytes > max_bytes:
            return False

    # 4. Check accuracy constraints
    slice_accs = compute_slice_accuracies(rows, name)

    # 4a. Check required accuracy floor per slice
    required_slices = policy.get("requiredSlices", {})
    for slice_name, floor in required_slices.items():
        if slice_accs.get(slice_name, 0.0) < floor:
            return False

    # 4b. Check overall aggregate accuracy floor
    aggregate_floor = policy.get("aggregateFloor")
    if aggregate_floor is not None:
        if not rows:
            return False
        total_correct = sum(
            1 for r in rows
            if r.get("predictions", {}).get(name) == r.get("label")
        )
        overall_acc = total_correct / len(rows)
        if overall_acc < aggregate_floor:
            return False

    return True


@app.route("/quantize", methods=["POST"])
def quantize():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            "selected": None,
            "status": "validation_error",
            "error": "Invalid or missing JSON body"
        }), 400

    phase = payload.get("phase")

    # -------------------------------------------------------------------------
    # PHASE: FREEZE
    # -------------------------------------------------------------------------
    if phase == "freeze":
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) == 0:
            app.logger.warning("Freeze validation failed: 'candidates' field missing, not a list, or empty.")
            return jsonify({
                "selected": None,
                "status": "validation_error",
                "error": "'candidates' must be a non-empty list during freeze phase"
            }), 400

        freeze_id = payload.get("freezeId")
        return jsonify({
            "freezeId": freeze_id,
            "status": "frozen",
            "count": len(candidates)
        }), 200

    # -------------------------------------------------------------------------
    # PHASE: SELECT
    # -------------------------------------------------------------------------
    elif phase == "select":
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            return jsonify({
                "selected": None,
                "status": "validation_error",
                "error": "'candidates' must be a list during select phase"
            }), 400

        policy = payload.get("policy", {})
        latencies = payload.get("latencies", {})
        rows = payload.get("rows", [])

        # Respect candidate evaluation order if explicitly specified
        candidate_order = policy.get("candidateOrder", [])
        cand_map = {c.get("name"): c for c in candidates if isinstance(c, dict) and c.get("name")}

        if candidate_order:
            ordered_candidates = [cand_map[name] for name in candidate_order if name in cand_map]
        else:
            ordered_candidates = [c for c in candidates if isinstance(c, dict)]

        # Evaluate candidates in order and select the first matching policy
        for cand in ordered_candidates:
            if evaluate_select_candidate(cand, policy, latencies, rows):
                return jsonify({
                    "selected": cand.get("name"),
                    "status": "selected"
                }), 200

        # Return status when no candidates meet all policy criteria
        return jsonify({
            "selected": None,
            "status": "no_candidate_selected"
        }), 200

    # -------------------------------------------------------------------------
    # UNKNOWN OR MISSING PHASE
    # -------------------------------------------------------------------------
    return jsonify({
        "selected": None,
        "status": "validation_error",
        "error": "Missing or unrecognized 'phase'"
    }), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
