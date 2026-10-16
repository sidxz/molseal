"""The authenticated contents of a structure row, before padding and encryption.

The wire form is canonical JSON (sorted keys, no whitespace, ASCII), so equal payloads give equal bytes.
Decoding validates every field: the bytes were authenticated, but a build that wrote them differently
must fail loudly rather than hand back a half-read payload."""

from __future__ import annotations

import json

from pydantic import Field, ValidationError
from pydantic.dataclasses import dataclass

from .errors import MolsealError

PAYLOAD_VERSION = 1


class PayloadError(MolsealError):
    """Decrypted bytes are not a payload this build reads: not JSON, another version, or a bad field."""


@dataclass(frozen=True)
class Payload:
    """What a row holds once decrypted. `keys` are the caller's exact lookup keys, kind -> value, as
    given to Vault.put. `deleted` marks a tombstone; the vault's read methods never return one, so a
    caller only meets it through `decode`."""

    smiles: str
    keys: dict[str, str] = Field(default_factory=dict)
    deleted: bool = False


def encode(p: Payload) -> bytes:
    doc = {"v": PAYLOAD_VERSION, "smiles": p.smiles, "keys": dict(p.keys), "deleted": p.deleted}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def decode(b: bytes) -> Payload:
    try:
        doc = json.loads(b)
    except ValueError as exc:
        raise PayloadError("payload is not JSON") from exc
    if not isinstance(doc, dict) or doc.get("v") != PAYLOAD_VERSION:
        raise PayloadError("unknown payload version")
    try:
        return Payload(smiles=doc["smiles"], keys=doc["keys"], deleted=doc["deleted"])
    except KeyError as exc:
        raise PayloadError(f"payload is missing field {exc}") from None
    except ValidationError as exc:   # its text repeats the input values, i.e. the decrypted contents
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        raise PayloadError(f"payload has malformed fields: {fields}") from None
