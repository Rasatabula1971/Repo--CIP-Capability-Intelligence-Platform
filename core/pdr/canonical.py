"""
Canonical serialisation and hashing.

Everything the PDR workflow hashes — a requirements snapshot, a lock
payload, a need fingerprint — goes through canonical_json so the hash is
byte-stable across processes, platforms and dict insertion orders. Same
bar as scorecard.computed_hash (Step 9): stable under
PYTHONHASHSEED=random, verified by a subprocess test.

Rules:
  - object keys sorted, no insignificant whitespace
  - UTF-8, non-ASCII kept literal (ensure_ascii=False)
  - tuples become lists; sets become sorted lists
  - UUIDs become their string form
  - floats are refused: nothing in the workflow needs one, and a float
    that prints differently is how a hash stops being reproducible
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(
        _normalise(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def content_hash(obj: Any) -> str:
    """sha256 of the canonical JSON form of obj."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalise_text(text: str) -> str:
    """
    Normalise a document before hashing and storing it.

    Strips a UTF-8 BOM and folds CRLF / CR to LF, so the same PDR saved
    on Windows and on Linux produces one content_hash, and so character
    offsets into the stored text mean the same thing everywhere.
    """
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalise(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        raise TypeError(
            "floats are not allowed in canonical payloads; use an int or a string"
        )
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"canonical payload keys must be strings, got {k!r}")
            out[k] = _normalise(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_normalise(x) for x in obj]
    if isinstance(obj, (set, frozenset)):
        items = [_normalise(x) for x in obj]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    raise TypeError(f"cannot canonicalise {type(obj).__name__}")
