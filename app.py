"""
app.py

Flask entrypoint. Single endpoint: POST /quantize, dispatching on
body["phase"] to the freeze or select implementation.
"""

import json

from flask import Flask, request, Response

import storage
import freeze
import select_phase as select_mod

app = Flask(__name__)
storage.init_db()


def json_response(obj, status=200):
    # Compact-ish but readable; exact key order follows dict insertion
    # order as built by freeze.py / select.py.
    body = json.dumps(obj, ensure_ascii=False)
    return Response(body, status=status, mimetype="application/json")


@app.route("/quantize", methods=["POST"])
def quantize():
    try:
        body = request.get_json(force=True, silent=True)
    except Exception:
        body = None

    if not isinstance(body, dict):
        return json_response({"error": "INVALID_INPUT"}, 400)

    phase = body.get("phase")

    if phase == "freeze":
        reject_reason = freeze.validate_top_level(body)
        if reject_reason is not None:
            return json_response({"error": "INVALID_INPUT"}, 400)

        freeze_id = body["freezeId"]
        response_obj = freeze.build_freeze_response(body)

        outcome, stored_response = storage.put_freeze_if_absent(freeze_id, body, response_obj)

        if outcome in ("created", "replay"):
            return json_response(stored_response, 200)
        else:  # conflict
            return json_response({"error": "FREEZE_ID_CONFLICT"}, 409)

    elif phase == "select":
        reject_reason = select_mod.validate_top_level(body)
        if reject_reason is not None:
            return json_response({"error": "INVALID_INPUT"}, 400)

        response_obj = select_mod.build_select_response(body, storage.get_freeze)
        return json_response(response_obj, 200)

    else:
        return json_response({"error": "INVALID_INPUT"}, 400)


@app.route("/", methods=["GET"])
def health():
    return json_response({"status": "ok"}, 200)


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
