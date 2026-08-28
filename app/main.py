import hashlib
from typing import Any, Dict, List, Optional, Tuple
from flask import Flask, jsonify, request

app = Flask(__name__)

# Temporary in-memory state for candidates uploaded during Phase 1
STORED_CANDIDATES: Dict[str, Dict[str, Any]] = {}


def is_finite_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def validate_freeze_structure(body: Dict[str, Any]) -> bool:
    """Validates the schema for Phase 1 (Freeze)."""
    if not isinstance(body, dict):
        return False

    # Issue 1 Fix: allowedUnsupportedReasons should be optional
    allowed = body.get("allowedUnsupportedReasons")
    if allowed is not None:
        if (
            not isinstance(allowed, list)
            or not all(isinstance(a, str) and a for a in allowed)
            or len(set(allowed)) != len(allowed)
        ):
            return False

    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return False

    for c in candidates:
        if not isinstance(c, dict):
            return False
        if not isinstance(c.get("name"), str) or not c["name"]:
            return False

        # Issue 3 Fix: Skip loadable/digest checks if unsupportedReason is populated
        if not c.get("unsupportedReason"):
            if not isinstance(c.get("loadable"), bool):
                return False
            if not isinstance(c.get("calibrationDigest"), str):
                return False
            if not isinstance(c.get("tokenizerDigest"), str):
                return False

    return True


def validate_select_structure(body: Dict[str, Any]) -> bool:
    """Validates the schema for Phase 2 (Select)."""
    if not isinstance(body, dict):
        return False

    # Issue 2 Fix: candidates list is optional during Select phase
    candidates = body.get("candidates")
    if candidates is not None and not isinstance(candidates, list):
        return False

    if "selectionPolicy" in body and not isinstance(body["selectionPolicy"], dict):
        return False

    return True


def validate_policy(body: Dict[str, Any], candidate_names: List[str]) -> bool:
    """Validates policy rules against candidates."""
    policy = body.get("selectionPolicy", {})
    if not isinstance(policy, dict):
        return False

    # Issue 5 Fix: Candidate latencies should not invalidate the entire policy structure.
    # Policy structure checks (e.g., maxTotalBytes, maxLatencyMs) remain, while individual
    # latency evaluations are deferred to candidate-level selection checks.
    max_bytes = policy.get("maxTotalBytes")
    if max_bytes is not None and (not is_finite_number(max_bytes) or max_bytes < 0):
        return False

    max_latency = policy.get("maxLatencyMs")
    if max_latency is not None and (not is_finite_number(max_latency) or max_latency < 0):
        return False

    return True


def recompute_manifest(base_candidate: Dict[str, Any], files: List[Dict[str, Any]]) -> Tuple[bool, Optional[int], Optional[str]]:
    """Calculates package total bytes and digest without assuming declared metadata is correct."""
    total_bytes = 0
    hasher = hashlib.sha256()

    try:
        for f in sorted(files, key=lambda x: x.get("path", "")):
            content = f.get("content", b"")
            if isinstance(content, str):
                content = content.encode("utf-8")

            total_bytes += len(content)
            hasher.update(content)

        calculated_digest = hasher.hexdigest()

        # Issue 6 Fix: Never fail evaluation based on declared vs computed mismatches;
        # return computed values directly per design contract.
        return True, total_bytes, calculated_digest
    except Exception:
        return False, None, None


def compute_freeze_response(body: Dict[str, Any]) -> Dict[str, Any]:
    allowed_reasons = set(body.get("allowedUnsupportedReasons", []))
    candidates = body.get("candidates", [])

    results = []
    for c in candidates:
        name = c["name"]
        unsupported = c.get("unsupportedReason")

        if unsupported:
            if unsupported in allowed_reasons:
                status = "ALLOWED_UNSUPPORTED"
            else:
                status = "REJECTED_UNSUPPORTED"
        elif not c.get("loadable", False):
            status = "UNLOADABLE"
        else:
            status = "FROZEN"
            STORED_CANDIDATES[name] = c

        results.append({"name": name, "status": status})

    return {"candidates": results}


def handle_select(body: Dict[str, Any]) -> Dict[str, Any]:
    # Issue 2 Fix: Safely fall back to stored candidates if omitted in payload
    submitted_candidates = body.get("candidates") or list(STORED_CANDIDATES.values())
    policy = body.get("selectionPolicy", {})

    max_bytes = policy.get("maxTotalBytes", float("inf"))
    max_latency = policy.get("maxLatencyMs", float("inf"))

    admitted_results = []
    order_index = {c["name"]: idx for idx, c in enumerate(submitted_candidates)}

    for c in submitted_candidates:
        name = c["name"]
        total_bytes = c.get("totalBytes", 0)
        latency_ms = c.get("latencyMs")

        # Basic admissibility check
        if total_bytes > max_bytes:
            continue
        if latency_ms is not None and latency_ms > max_latency:
            continue

        admitted_results.append({
            "name": name,
            "totalBytes": total_bytes,
            "latencyMs": latency_ms,
            "candidate": c
        })

    # Issue 4 Fix: Safe sort key handling None for latencyMs
    admitted_sorted = sorted(
        admitted_results,
        key=lambda r: (
            r["totalBytes"],
            r["latencyMs"] if r["latencyMs"] is not None else float("inf"),
            order_index.get(r["name"], len(order_index)),
        ),
    )

    selected_candidate = admitted_sorted[0]["candidate"] if admitted_sorted else None

    return {
        "selected": selected_candidate["name"] if selected_candidate else None,
        "admittedCount": len(admitted_sorted),
    }


@app.route("/freeze", methods=["POST"])
def freeze_endpoint():
    body = request.get_json(silent=True) or {}
    if not validate_freeze_structure(body):
        return jsonify({"error": "INVALID_INPUT"}), 400

    response_data = compute_freeze_response(body)
    return jsonify(response_data), 200


@app.route("/select", methods=["POST"])
def select_endpoint():
    body = request.get_json(silent=True) or {}
    if not validate_select_structure(body):
        return jsonify({"error": "INVALID_INPUT"}), 400

    candidate_names = [c["name"] for c in body.get("candidates", []) if "name" in c]
    if not validate_policy(body, candidate_names):
        return jsonify({"error": "INVALID_INPUT"}), 400

    response_data = handle_select(body)
    return jsonify(response_data), 200


if __name__ == "__main__":
    app.run(port=8000)
