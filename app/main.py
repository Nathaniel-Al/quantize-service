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

    candidates = body.get("candidates")
    if candidates is not None and not isinstance(candidates, list):
        return False

    if "selectionPolicy" in body and not isinstance(body["selectionPolicy"], dict):
        return False

    return True


def validate_quantize_structure(body: Dict[str, Any]) -> bool:
    """Validates the payload structure for Quantize requests."""
    if not isinstance(body, dict):
        return False

    # Check for candidates array OR single candidate/root-level candidate payload
    if "candidates" in body:
        candidates = body["candidates"]
        if not isinstance(candidates, list) or not candidates:
            return False
        for c in candidates:
            if not isinstance(c, dict):
                return False
            if not isinstance(c.get("name"), str) or not c["name"]:
                return False
            if "files" in c and not isinstance(c["files"], list):
                return False
    else:
        target = body.get("candidate", body)
        if not isinstance(target, dict):
            return False
        if not isinstance(target.get("name"), str) or not target["name"]:
            return False
        if "files" in target and not isinstance(target["files"], list):
            return False

    return True


def validate_policy(body: Dict[str, Any], candidate_names: List[str]) -> bool:
    """Validates policy rules against candidates."""
    policy = body.get("selectionPolicy", {})
    if not isinstance(policy, dict):
        return False

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


def handle_quantize(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Processes quantization requests, recalculates file manifests, and updates state."""
    is_batch = "candidates" in body
    items = body["candidates"] if is_batch else [body.get("candidate", body)]
    processed = []

    for item in items:
        cand = dict(item)
        files = cand.get("files", body.get("files", []))

        ok, total_bytes, package_digest = recompute_manifest(cand, files)
        if not ok:
            return None, False

        cand["totalBytes"] = total_bytes
        cand["packageDigest"] = package_digest

        name = cand.get("name")
        if name:
            if name in STORED_CANDIDATES:
                STORED_CANDIDATES[name].update(cand)
            else:
                STORED_CANDIDATES[name] = cand

        processed.append(cand)

    if is_batch:
        return {"candidates": processed}, True
    elif "candidate" in body:
        return {"candidate": processed[0]}, True
    else:
        return processed[0], True


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


@app.route("/quantize", methods=["POST"])
def quantize_endpoint():
    body = request.get_json(silent=True) or {}
    if not validate_quantize_structure(body):
        return jsonify({"error": "INVALID_INPUT"}), 400

    response_data, ok = handle_quantize(body)
    if not ok:
        return jsonify({"error": "INVALID_INPUT"}), 400

    return jsonify(response_data), 200


if __name__ == "__main__":
    app.run(port=10000)
