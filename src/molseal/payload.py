"""The authenticated contents of a structure row, before padding and encryption."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

PAYLOAD_VERSION = 1


class PayloadError(Exception):
    pass


@dataclass(frozen=True)
class Payload:
    smiles: str
    keys: dict[str, str] = field(default_factory=dict)
    deleted: bool = False


def encode(p: Payload) -> bytes:
    doc = {"v": PAYLOAD_VERSION, "smiles": p.smiles, "keys": dict(p.keys), "deleted": bool(p.deleted)}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def decode(b: bytes) -> Payload:
    try:
        doc = json.loads(b)
    except ValueError as exc:
        raise PayloadError("payload is not JSON") from exc
    if not isinstance(doc, dict) or doc.get("v") != PAYLOAD_VERSION:
        raise PayloadError("unknown payload version")
    try:
        return Payload(
            smiles=str(doc["smiles"]),
            keys={str(k): str(v) for k, v in doc["keys"].items()},
            deleted=bool(doc["deleted"]),
        )
    except (KeyError, AttributeError, TypeError) as exc:
        raise PayloadError("payload is missing a field") from exc
