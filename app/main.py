import hashlib
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

STORED_CANDIDATES: Dict[str, Dict[str, Any]] = {}


def is_finite_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def validate_freeze_structure(body: Dict[str, Any]) -> bool:
    if not isinstance(body, dict):
        logger.warning("Freeze validation failed: Body is not a JSON object.")
        return False

    allowed = body.get("allowedUnsupportedReasons")
    if allowed is not None:
        if (
            not isinstance(allowed, list)
            or not all(isinstance(a, str) and a for a in allowed)
            or len(set(allowed)) != len(allowed)
        ):
            logger.warning("Freeze validation failed: Invalid allowedUnsupportedReasons format.")
            return False

    candidates = body.get("candidates")
    if not isinstance(candidates, list):
        logger.warning("Freeze validation failed: 'candidates' field missing or not a list.")
        return False

    top_cal = body.get("calibrationDigest")
    top_tok = body.get("tokenizerDigest")

    for idx, c in enumerate(candidates):
        if not isinstance(c, dict):
            logger.warning(f"Freeze validation failed: Candidate at index {idx} is not an object.")
            return False
        if not isinstance(c.get("name"), str) or not c["name"]:
            logger.warning(f"Freeze validation failed: Candidate at index {idx} missing valid 'name'.")
            return False

        if not c.get("unsupportedReason"):
            if not isinstance(c.get("loadable"), bool):
                logger.warning(f"Freeze validation failed: Candidate '{c.get('name')}' missing 'loadable' bool.")
                return False

            cand_cal = c.get("calibrationDigest", top_cal)
            cand_tok = c.get("tokenizerDigest", top_tok)

            if not isinstance(cand_cal, str):
                logger.warning(f"Freeze validation failed: Candidate '{c.get('name')}' missing valid 'calibrationDigest'.")
                return False
            if not isinstance(cand_tok, str):
                logger.warning(f"Freeze validation failed: Candidate '{c.get('name')}' missing valid 'tokenizerDigest'.")
                return False

    return True


def validate_select_structure(body: Dict[str, Any]) -> bool:
    if not isinstance(body, dict):
        logger.warning("Select validation failed: Body is not a JSON object.")
        return False

    candidates = body.get("candidates")
    if candidates is not None and not isinstance(candidates, list):
        logger.warning("Select validation failed: 'candidates' field is provided but is not a list.")
        return False

    if "selectionPolicy" in body and not isinstance(body["selectionPolicy"], dict):
        logger.warning("Select validation failed: 'selectionPolicy' is not a dict.")
        return False

    return True


def validate_quantize_structure(body: Dict[str, Any]) -> bool:
    if not isinstance(body, dict):
        logger.warning("Quantize validation failed: Body is not a dict.")
        return False

    if "candidates" in body:
        candidates = body["candidates"]
        if not isinstance(candidates, list):
            logger.warning("Quantize validation failed: 'candidates' field is not a list.")
            return False
        for idx, c in enumerate(candidates):
            if not isinstance(c, dict):
                logger.warning(f"Quantize validation failed: Candidate at index {idx} is not an object.")
                return False
            if "files" in c and not isinstance(c["files"], list):
                logger.warning(f"Quantize validation failed: 'files' in candidate '{c.get('name')}' is not a list.")
                return False
    else:
        target = body.get("candidate", body)
        if not isinstance(target, dict):
            logger.warning("Quantize validation failed: Target payload is not an object.")
            return False
        if "files" in target and not isinstance(target["files"], list):
            logger.warning("Quantize validation failed: 'files' field is present but not a list.")
            return False

    return True


def validate_policy(body: Dict[str, Any], candidate_names: List[str]) -> bool:
    policy = body.get("selectionPolicy", {})
    if not isinstance(policy, dict):
        logger.warning("Policy validation failed: 'selectionPolicy' is not a dictionary.")
        return False

    max_bytes = policy.get("maxTotalBytes")
    if max_bytes is not None and (not is_finite_number(max_bytes) or max_bytes < 0):
        logger.warning(f"Policy validation failed: Invalid maxTotalBytes '{max_bytes}'.")
        return False

    max_latency = policy.get("maxLatencyMs")
    if max_latency is not None and (not is_finite_number(max_latency) or max_latency < 0):
        logger.warning(f"Policy validation failed: Invalid maxLatencyMs '{max_latency}'.")
        return False

    return True


def recompute_manifest(base_candidate: Dict[str, Any], files: List[Dict[str, Any]]) -> Tuple[bool, Optional[int], Optional[str]]:
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
    except Exception as e:
        logger.error(f"Error recomputing manifest: {str(e)}")
        return False, None, None


def compute_freeze_response(body: Dict[str, Any]) -> Dict[str, Any]:
    allowed_reasons = set(body.get("allowedUnsupportedReasons", []))
    candidates = body.get("candidates", [])
    top_cal = body.get("calibrationDigest")
    top_tok = body.get("tokenizerDigest")

    results = []
    for c in candidates:
        name = c["name"]
        unsupported = c.get("unsupportedReason")

        cand_cal = c.get("calibrationDigest", top_cal)
        cand_tok = c.get("tokenizerDigest", top_tok)

        if unsupported:
            if unsupported in allowed_reasons:
                status = "ALLOWED_UNSUPPORTED"
            else:
                status = "REJECTED_UNSUPPORTED"
        elif not c.get("loadable", False):
            status = "UNLOADABLE"
        elif top_cal and cand_cal != top_cal:
            status = "UNLOADABLE"
        elif top_tok and cand_tok != top_tok:
            status = "UNLOADABLE"
        else:
            status = "FROZEN"
            STORED_CANDIDATES[name] = c

        results.append({"name": name, "status": status})

    response: Dict[str, Any] = {"candidates": results}

    # Preserve all top-level context fields in response payload
    for field in ["freezeId", "phase", "calibrationDigest", "tokenizerDigest", "allowedUnsupportedReasons"]:
        if field in body:
            response[field] = body[field]

    return response


def handle_select(body: Dict[str, Any]) -> Dict[str, Any]:
    submitted_candidates = body.get("candidates") or list(STORED_CANDIDATES.values())
    policy = body.get("selectionPolicy", {})

    max_bytes = policy.get("maxTotalBytes", float("inf"))
    max_latency = policy.get("maxLatencyMs", float("inf"))

    admitted_results = []
    order_index = {c["name"]: idx for idx, c in enumerate(submitted_candidates) if isinstance(c, dict) and "name" in c}

    for c in submitted_candidates:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not name:
            continue
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

    response: Dict[str, Any] = {
        "selected": selected_candidate["name"] if selected_candidate else None,
        "admittedCount": len(admitted_sorted),
    }

    for field in ["selectId", "phase", "selectionPolicy"]:
        if field in body:
            response[field] = body[field]

    return response


def handle_quantize(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
    is_batch = "candidates" in body
    items = body["candidates"] if is_batch else [body.get("candidate", body)]
    processed = []

    for item in items:
        cand = dict(item)
        files = cand.get("files", body.get("files", []))

        ok, total_bytes, package_digest = recompute_manifest(cand, files)
        if not ok:
            logger.warning(f"Failed manifest recomputation for item: {cand}")
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

    candidate_names = [c["name"] for c in body.get("candidates", []) if isinstance(c, dict) and "name" in c]
    if not validate_policy(body, candidate_names):
        return jsonify({"error": "INVALID_INPUT"}), 400

    response_data = handle_select(body)
    return jsonify(response_data), 200


@app.route("/quantize", methods=["POST"])
def quantize_endpoint():
    raw_data = request.get_data(as_text=True)
    logger.info(f"POST /quantize raw body: {raw_data}")
    body = request.get_json(silent=True) or {}

    phase = body.get("phase")

    if phase == "freeze":
        if not validate_freeze_structure(body):
            logger.warning("Freeze phase validation failed inside /quantize endpoint.")
            return jsonify({"error": "INVALID_INPUT"}), 400
        return jsonify(compute_freeze_response(body)), 200

    elif phase == "select":
        if not validate_select_structure(body):
            logger.warning("Select phase validation failed inside /quantize endpoint.")
            return jsonify({"error": "INVALID_INPUT"}), 400
        candidate_names = [c["name"] for c in body.get("candidates", []) if isinstance(c, dict) and "name" in c]
        if not validate_policy(body, candidate_names):
            return jsonify({"error": "INVALID_INPUT"}), 400
        return jsonify(handle_select(body)), 200

    if not validate_quantize_structure(body):
        logger.warning(f"POST /quantize validation rejected payload: {body}")
        return jsonify({"error": "INVALID_INPUT"}), 400

    response_data, ok = handle_quantize(body)
    if not ok:
        logger.warning(f"POST /quantize processing failed for payload: {body}")
        return jsonify({"error": "INVALID_INPUT"}), 400

    return jsonify(response_data), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
