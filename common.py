"""
common.py

Shared low-level helpers used by both the freeze and select phases:
strict type/shape checks (deliberately excluding bool where an int/float
is expected, since bool is a subclass of int in Python), UTF-8 byte
hashing, and the canonical-JSON package digest routine.
"""

import hashlib
import json
import math

MAX_SAFE_INTEGER = 2**53 - 1


def is_str(x) -> bool:
    return isinstance(x, str)


def is_nonempty_str(x, max_len=None) -> bool:
    if not isinstance(x, str) or len(x) == 0:
        return False
    if max_len is not None and len(x) > max_len:
        return False
    return True


def is_bool(x) -> bool:
    return isinstance(x, bool)


def is_number(x) -> bool:
    """True int/float, excluding bool."""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def is_finite_number(x) -> bool:
    return is_number(x) and math.isfinite(x)


def is_safe_nonneg_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= MAX_SAFE_INTEGER


def is_finite_in_unit_interval(x) -> bool:
    return is_finite_number(x) and 0 <= x <= 1


def is_finite_nonneg(x) -> bool:
    return is_finite_number(x) and x >= 0


def is_list(x) -> bool:
    return isinstance(x, list)


def is_dict(x) -> bool:
    return isinstance(x, dict)


def unique_nonempty_str_list(x) -> bool:
    if not is_list(x):
        return False
    if not all(is_nonempty_str(item) for item in x):
        return False
    return len(set(x)) == len(x)


def utf8_bytes(s: str) -> bytes:
    return s.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_inventory(files: dict):
    """
    files: dict[str, str] (filename -> UTF-8 content string), already
    validated as structurally sound by the caller.

    Returns (inventory_list, total_bytes, package_digest) where
    inventory_list entries are dicts with keys in the exact order
    name, bytes, sha256 (Python dicts preserve insertion order, and
    json.dumps respects it).

    Sorting "by UTF-8 filename": UTF-8's encoding preserves Unicode
    code-point ordering under byte-wise comparison, so a plain Python
    string sort (code-point order) is equivalent to sorting by the
    UTF-8 byte sequence.
    """
    items = []
    for name in sorted(files.keys()):
        content = files[name]
        raw = utf8_bytes(content)
        items.append(
            {
                "name": name,
                "bytes": len(raw),
                "sha256": sha256_hex(raw),
            }
        )

    total_bytes = sum(item["bytes"] for item in items)

    compact = json.dumps(items, separators=(",", ":"), ensure_ascii=False)
    package_digest = sha256_hex(utf8_bytes(compact))

    return items, total_bytes, package_digest


def sort_dedupe_codes(codes):
    """Sort and deduplicate reason codes by UTF-8 byte value."""
    return sorted(set(codes), key=lambda c: c.encode("utf-8"))
