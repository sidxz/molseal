# molseal Library (Milestone 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the generic `molseal` package: encrypted structure rows in any SQL database, an in-process RDKit index for substructure and similarity, exact-match by keyed hash, encrypted snapshots, a CLI, and the absence check with its negative control.

**Architecture:** Eight small modules under `src/molseal`. `crypto`, `keys` and `payload` are pure byte-level helpers. `store` is SQLAlchemy Core over four tables and knows no chemistry. `engine` wraps RDKit's SubstructLibrary plus a packed Morgan matrix and knows no SQL. `snapshot` serializes the engine to a chunk-encrypted file. `vault` is the per-tenant facade that is the only module importing all of them. `cli` drives the vault.

**Tech Stack:** Python 3.13, uv, `rdkit==2025.3.3`, `cryptography`, `numpy>=2.0`, `sqlalchemy>=2.0`, `psycopg[binary]>=3.2`, pytest. SQLite for unit tests, PostgreSQL 16 (docker compose, port 5439) for integration tests.

**Spec:** `docs/superpowers/specs/2026-10-13-molseal-poc-design.md` (sections 1 to 7, 9, 10, 11, 13). The benchmark (spec section 8) is a separate plan written after this one lands.

## Global Constraints

- Python `>=3.13`; package manager `uv`; src layout (`src/molseal`).
- Pinned: `rdkit==2025.3.3`, `numpy>=2.0` (needs `np.bitwise_count`), `sqlalchemy>=2.0`, `cryptography>=45`, `psycopg[binary]>=3.2`. No other runtime dependencies. CLI is `argparse`.
- **Generic-library rule (spec 1.1):** nothing under `src/molseal` references ChemCellar or any consumer, nor consumer concepts (workspace, registration number, project). Tenants are opaque `UUID`s. Exact keys are caller-defined `(kind, value)` strings; the library never computes InChIKeys (the CLI may, acting as a caller).
- Match flags fixed: `useChirality=False`, `recursionPossible=True`, `useQueryQueryMatches=False`. Morgan radius 2, 2048 bits, default invariants.
- Padding: power-of-two size classes, minimum 256 bytes, true length in the last 4 bytes. Nonce: 12 random bytes from `os.urandom` on every encryption. Snapshot chunk size 64 MiB.
- AAD formats (fixed width): row = `tenant.bytes + id.bytes + >II(key_version, row_version)`; key wrap = `tenant.bytes + kind.encode() + b"\x00" + >I(version)`; snapshot chunk = `prefix + >II(index, count)`.
- License BSD-3-Clause. Conventional Commits. Every commit uses explicit pathspecs (`git add -- <paths>`), ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`, and never carries a `Claude-Session` trailer or claude.ai URL.
- Tests never touch the network. Postgres tests skip unless `MOLSEAL_TEST_DATABASE_URL` is set.
- Run everything through `uv run` from the repo root `~/workspace/molseal-poc`.

## Review Focus

Inputs the spec implies but did not spell out, each pinned by a test in the owning task:

1. **Unparseable SMILES on `put`** must raise before any database write; no half row, no cursor bump. (Task 10)
2. **A payload whose serialized length sits exactly on a size-class boundary** (length + 4 == 256) must round-trip; off-by-one here corrupts every such row. (Task 2)
3. **Editing an id whose old structure matches a query the new one does not** must return nothing for that id; and the same id must never appear twice in results. (Task 8)
4. **Empty states:** search on an empty index, a query matching nothing, `put` with no exact keys, `find_exact` for an unknown value all return empty results rather than raising. (Tasks 7, 8, 10)
5. **A snapshot made for another tenant or key version, or with a tampered header,** must refuse to load even though the file is well-formed. (Task 9)

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `LICENSE`, `README.md`, `src/molseal/__init__.py`, `tests/__init__.py`, `tests/test_scaffold.py`, `docker-compose.yml`

**Interfaces:**
- Produces: importable package `molseal` with `__version__`; `uv run pytest` works; compose file for a dev Postgres on port 5439.

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "molseal"
version = "0.0.1"
description = "Encrypted chemical structure store with in-process RDKit search"
readme = "README.md"
requires-python = ">=3.13"
license = "BSD-3-Clause"
dependencies = [
  "rdkit==2025.3.3",
  "cryptography>=45",
  "numpy>=2.0",
  "sqlalchemy>=2.0",
  "psycopg[binary]>=3.2",
]

[project.scripts]
molseal = "molseal.cli:main"

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/molseal"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Write the package init, license, README stub and compose file**

`src/molseal/__init__.py`:
```python
"""molseal: encrypted structure rows in any SQL database, chemistry search in-process."""

__version__ = "0.0.1"
```

`LICENSE`: the standard BSD-3-Clause text with `Copyright (c) 2026, Siddhant Rath`.

`README.md` (expanded in Task 13):
```markdown
# molseal

Encrypted chemical structure store with in-process RDKit search. Proof of concept.

Design: `docs/superpowers/specs/2026-10-13-molseal-poc-design.md`.
```

`docker-compose.yml` (dev Postgres for integration tests; port 5439 so it never collides with another local Postgres):
```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: molseal
      POSTGRES_PASSWORD: molseal
      POSTGRES_DB: molseal
    ports:
      - "5439:5432"
    volumes:
      - molseal-pgdata:/var/lib/postgresql/data
volumes:
  molseal-pgdata:
```

`tests/__init__.py`: empty file.

- [ ] **Step 3: Write the scaffold test**

`tests/test_scaffold.py`:
```python
import molseal


def test_package_imports_and_has_version():
    assert molseal.__version__ == "0.0.1"


def test_rdkit_and_numpy_are_the_pinned_generation():
    import numpy as np
    import rdkit

    assert rdkit.__version__.startswith("2025.03")
    assert hasattr(np, "bitwise_count")
```

- [ ] **Step 4: Install and run**

Run: `cd ~/workspace/molseal-poc && uv sync && uv run pytest -q`
Expected: `2 passed`. If `uv sync` cannot resolve `rdkit==2025.3.3` for Python 3.13 on this Mac, stop and report; do not loosen the pin.

- [ ] **Step 5: Commit**

```bash
git add -- pyproject.toml uv.lock LICENSE README.md docker-compose.yml src/molseal/__init__.py tests/__init__.py tests/test_scaffold.py
git commit -m "chore: scaffold molseal package

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- pyproject.toml uv.lock LICENSE README.md docker-compose.yml src/molseal/__init__.py tests/__init__.py tests/test_scaffold.py
```

---

### Task 2: crypto module

**Files:**
- Create: `src/molseal/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Produces:
  - `class SealError(Exception)`
  - `NONCE_LEN = 12`, `MIN_SIZE_CLASS = 256`, `CHUNK_SIZE = 64 * 1024 * 1024`
  - `size_class(n: int, min_class: int = 256) -> int`
  - `pad(data: bytes, min_class: int = 256) -> bytes`, `unpad(padded: bytes) -> bytes`
  - `seal(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]` returning `(nonce, ciphertext)`
  - `open_sealed(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes` raising `SealError`
  - `row_aad(tenant_id: UUID, id: UUID, key_version: int, row_version: int) -> bytes`
  - `key_aad(tenant_id: UUID, kind: str, version: int) -> bytes`
  - `chunk_aad(prefix: bytes, index: int, count: int) -> bytes`
  - `seal_chunks(key, data, aad_prefix, chunk_size=CHUNK_SIZE) -> list[tuple[bytes, bytes]]`
  - `open_chunks(key, chunks, aad_prefix) -> bytes`

- [ ] **Step 1: Write the failing tests**

`tests/test_crypto.py`:
```python
import os
from uuid import uuid4

import pytest

from molseal import crypto

KEY = bytes(range(32))


@pytest.mark.parametrize("n,expected", [(0, 256), (1, 256), (252, 256), (253, 512), (508, 512), (509, 1024)])
def test_pad_lands_on_the_smallest_power_of_two_class(n, expected):
    data = os.urandom(n)
    padded = crypto.pad(data)
    assert len(padded) == expected
    assert crypto.unpad(padded) == data


def test_unpad_rejects_garbage():
    with pytest.raises(crypto.SealError):
        crypto.unpad(b"\x00\x00\x00\xff")


def test_seal_open_round_trip_with_aad():
    nonce, ct = crypto.seal(KEY, b"hello", b"label")
    assert len(nonce) == crypto.NONCE_LEN
    assert crypto.open_sealed(KEY, nonce, ct, b"label") == b"hello"


def test_wrong_aad_wrong_key_and_tamper_all_fail():
    nonce, ct = crypto.seal(KEY, b"hello", b"label")
    with pytest.raises(crypto.SealError):
        crypto.open_sealed(KEY, nonce, ct, b"other")
    with pytest.raises(crypto.SealError):
        crypto.open_sealed(bytes(32), nonce, ct, b"label")
    bad = bytes([ct[0] ^ 1]) + ct[1:]
    with pytest.raises(crypto.SealError):
        crypto.open_sealed(KEY, nonce, bad, b"label")


def test_every_seal_uses_a_fresh_nonce():
    a = crypto.seal(KEY, b"same", b"same")
    b = crypto.seal(KEY, b"same", b"same")
    assert a[0] != b[0]
    assert a[1] != b[1]


def test_row_and_key_aad_are_fixed_width_and_distinct():
    t, i = uuid4(), uuid4()
    assert len(crypto.row_aad(t, i, 1, 1)) == 16 + 16 + 8
    assert crypto.row_aad(t, i, 1, 1) != crypto.row_aad(t, i, 1, 2)
    assert crypto.row_aad(t, i, 1, 1) != crypto.row_aad(t, i, 2, 1)
    assert crypto.key_aad(t, "data", 1) != crypto.key_aad(t, "hmac", 1)


def test_chunks_round_trip_and_detect_reorder_drop_tamper():
    data = os.urandom(1000)
    chunks = crypto.seal_chunks(KEY, data, b"pre", chunk_size=400)
    assert len(chunks) == 3
    assert crypto.open_chunks(KEY, chunks, b"pre") == data
    with pytest.raises(crypto.SealError):
        crypto.open_chunks(KEY, [chunks[1], chunks[0], chunks[2]], b"pre")
    with pytest.raises(crypto.SealError):
        crypto.open_chunks(KEY, chunks[:2], b"pre")
    n, c = chunks[2]
    with pytest.raises(crypto.SealError):
        crypto.open_chunks(KEY, chunks[:2] + [(n, bytes([c[0] ^ 1]) + c[1:])], b"pre")
    with pytest.raises(crypto.SealError):
        crypto.open_chunks(KEY, chunks, b"other")


def test_empty_data_is_one_chunk():
    chunks = crypto.seal_chunks(KEY, b"", b"pre")
    assert len(chunks) == 1
    assert crypto.open_chunks(KEY, chunks, b"pre") == b""
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_crypto.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'molseal.crypto'` (or `AttributeError`).

- [ ] **Step 3: Write the implementation**

`src/molseal/crypto.py`:
```python
"""Byte-level primitives: AES-256-GCM with a label (AAD), size-class padding, chunked sealing.

Nothing here knows about chemistry, databases or files.
"""

from __future__ import annotations

import os
import struct
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12
MIN_SIZE_CLASS = 256
CHUNK_SIZE = 64 * 1024 * 1024


class SealError(Exception):
    """Ciphertext did not open: wrong key, wrong label, or tampered bytes."""


def size_class(n: int, min_class: int = MIN_SIZE_CLASS) -> int:
    """Smallest power-of-two class >= n, never below min_class."""
    c = min_class
    while c < n:
        c *= 2
    return c


def pad(data: bytes, min_class: int = MIN_SIZE_CLASS) -> bytes:
    """data + zero fill + 4-byte big-endian true length, sized to a power-of-two class."""
    total = size_class(len(data) + 4, min_class)
    return data + b"\x00" * (total - len(data) - 4) + struct.pack(">I", len(data))


def unpad(padded: bytes) -> bytes:
    if len(padded) < 4:
        raise SealError("padded data too short")
    (n,) = struct.unpack(">I", padded[-4:])
    if n > len(padded) - 4:
        raise SealError("bad padding length")
    return padded[:n]


def seal(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    """Encrypt with a fresh random nonce. Returns (nonce, ciphertext)."""
    nonce = os.urandom(NONCE_LEN)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def open_sealed(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise SealError("ciphertext did not open") from exc


def row_aad(tenant_id: UUID, id: UUID, key_version: int, row_version: int) -> bytes:
    return tenant_id.bytes + id.bytes + struct.pack(">II", key_version, row_version)


def key_aad(tenant_id: UUID, kind: str, version: int) -> bytes:
    return tenant_id.bytes + kind.encode("ascii") + b"\x00" + struct.pack(">I", version)


def chunk_aad(prefix: bytes, index: int, count: int) -> bytes:
    return prefix + struct.pack(">II", index, count)


def seal_chunks(
    key: bytes, data: bytes, aad_prefix: bytes, chunk_size: int = CHUNK_SIZE
) -> list[tuple[bytes, bytes]]:
    """Split data into chunks and seal each with a label naming its index and the chunk count."""
    count = max(1, -(-len(data) // chunk_size))
    return [
        seal(key, data[i * chunk_size : (i + 1) * chunk_size], chunk_aad(aad_prefix, i, count))
        for i in range(count)
    ]


def open_chunks(key: bytes, chunks: list[tuple[bytes, bytes]], aad_prefix: bytes) -> bytes:
    count = len(chunks)
    return b"".join(
        open_sealed(key, nonce, ct, chunk_aad(aad_prefix, i, count))
        for i, (nonce, ct) in enumerate(chunks)
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_crypto.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -- src/molseal/crypto.py tests/test_crypto.py
git commit -m "feat(crypto): AES-GCM with labels, size-class padding, chunked sealing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/crypto.py tests/test_crypto.py
```

---

### Task 3: keys module

**Files:**
- Create: `src/molseal/keys.py`
- Test: `tests/test_keys.py`

**Interfaces:**
- Consumes: `crypto.seal`, `crypto.open_sealed`, `crypto.key_aad`
- Produces:
  - `KEY_LEN = 32`
  - `class KeyProvider(Protocol)` with `kek() -> bytes`
  - `class KeyFileError(Exception)`
  - `generate_key_file(path: Path) -> None`
  - `class FileKeyProvider(path)` with `kek() -> bytes`
  - `@dataclass(frozen=True) TenantKeys(data_key: bytes, data_key_version: int, hmac_key: bytes, hmac_key_version: int)`
  - `new_tenant_secret() -> bytes`
  - `wrap_key(kek, tenant_id, kind, version, secret) -> tuple[bytes, bytes]`
  - `unwrap_key(kek, tenant_id, kind, version, nonce, wrapped) -> bytes`
  - `exact_key_hmac(hmac_key: bytes, kind: str, value: str) -> bytes` (32 bytes)

- [ ] **Step 1: Write the failing tests**

`tests/test_keys.py`:
```python
import os
import stat
from uuid import uuid4

import pytest

from molseal import crypto, keys


def test_generate_key_file_is_600_and_32_bytes(tmp_path):
    p = tmp_path / "kek.key"
    keys.generate_key_file(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert len(p.read_bytes()) == keys.KEY_LEN
    assert keys.FileKeyProvider(p).kek() == p.read_bytes()


def test_generate_refuses_to_overwrite(tmp_path):
    p = tmp_path / "kek.key"
    keys.generate_key_file(p)
    with pytest.raises(FileExistsError):
        keys.generate_key_file(p)


def test_group_or_world_readable_key_file_is_refused(tmp_path):
    p = tmp_path / "kek.key"
    keys.generate_key_file(p)
    os.chmod(p, 0o644)
    with pytest.raises(keys.KeyFileError):
        keys.FileKeyProvider(p).kek()


def test_wrong_length_key_file_is_refused(tmp_path):
    p = tmp_path / "kek.key"
    p.write_bytes(b"short")
    os.chmod(p, 0o600)
    with pytest.raises(keys.KeyFileError):
        keys.FileKeyProvider(p).kek()


def test_wrap_unwrap_round_trip_and_binding():
    kek = os.urandom(32)
    t = uuid4()
    secret = keys.new_tenant_secret()
    nonce, wrapped = keys.wrap_key(kek, t, "data", 1, secret)
    assert keys.unwrap_key(kek, t, "data", 1, nonce, wrapped) == secret
    with pytest.raises(crypto.SealError):
        keys.unwrap_key(kek, uuid4(), "data", 1, nonce, wrapped)
    with pytest.raises(crypto.SealError):
        keys.unwrap_key(kek, t, "hmac", 1, nonce, wrapped)
    with pytest.raises(crypto.SealError):
        keys.unwrap_key(kek, t, "data", 2, nonce, wrapped)


def test_exact_key_hmac_is_deterministic_and_keyed():
    k1, k2 = os.urandom(32), os.urandom(32)
    a = keys.exact_key_hmac(k1, "inchikey", "ABC")
    assert len(a) == 32
    assert a == keys.exact_key_hmac(k1, "inchikey", "ABC")
    assert a != keys.exact_key_hmac(k1, "smiles", "ABC")
    assert a != keys.exact_key_hmac(k2, "inchikey", "ABC")
    assert keys.exact_key_hmac(k1, "ab", "c") != keys.exact_key_hmac(k1, "a", "bc")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_keys.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/molseal/keys.py`:
```python
"""Key hierarchy: a key-encryption key (KEK) from a provider wraps two secrets per tenant."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from .crypto import key_aad, open_sealed, seal

KEY_LEN = 32


class KeyProvider(Protocol):
    """Where the KEK comes from. Implementations: a 0600 file (here), a KMS (consumer's job)."""

    def kek(self) -> bytes: ...


class KeyFileError(Exception):
    pass


def generate_key_file(path: Path) -> None:
    """Write 32 random bytes to a new file with mode 0600. Refuses to overwrite."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(os.urandom(KEY_LEN))


@dataclass(frozen=True)
class FileKeyProvider:
    path: Path

    def kek(self) -> bytes:
        p = Path(self.path)
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            raise KeyFileError(f"{p} is readable by group or others (mode {mode:o}); chmod 600 it")
        data = p.read_bytes()
        if len(data) != KEY_LEN:
            raise KeyFileError(f"{p} must hold exactly {KEY_LEN} bytes, has {len(data)}")
        return data


@dataclass(frozen=True)
class TenantKeys:
    data_key: bytes
    data_key_version: int
    hmac_key: bytes
    hmac_key_version: int


def new_tenant_secret() -> bytes:
    return os.urandom(KEY_LEN)


def wrap_key(kek: bytes, tenant_id: UUID, kind: str, version: int, secret: bytes) -> tuple[bytes, bytes]:
    """Returns (nonce, wrapped). The label pins the secret to one tenant, kind and version."""
    return seal(kek, secret, key_aad(tenant_id, kind, version))


def unwrap_key(kek: bytes, tenant_id: UUID, kind: str, version: int, nonce: bytes, wrapped: bytes) -> bytes:
    return open_sealed(kek, nonce, wrapped, key_aad(tenant_id, kind, version))


def exact_key_hmac(hmac_key: bytes, kind: str, value: str) -> bytes:
    """Keyed hash of a caller-defined exact key. Same (key, kind, value) gives the same bytes."""
    msg = kind.encode("utf-8") + b"\x00" + value.encode("utf-8")
    return hmac.new(hmac_key, msg, hashlib.sha256).digest()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_keys.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -- src/molseal/keys.py tests/test_keys.py
git commit -m "feat(keys): file key provider, tenant key wrapping, exact-key HMAC

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/keys.py tests/test_keys.py
```

---

### Task 4: payload module

**Files:**
- Create: `src/molseal/payload.py`
- Test: `tests/test_payload.py`

**Interfaces:**
- Produces:
  - `PAYLOAD_VERSION = 1`
  - `class PayloadError(Exception)`
  - `@dataclass(frozen=True) Payload(smiles: str, keys: dict[str, str] = {}, deleted: bool = False)`
  - `encode(p: Payload) -> bytes` (canonical JSON: sorted keys, no whitespace, ASCII)
  - `decode(b: bytes) -> Payload`

- [ ] **Step 1: Write the failing tests**

`tests/test_payload.py`:
```python
import pytest

from molseal import payload


def test_encode_is_canonical_and_round_trips():
    p = payload.Payload(smiles="CCO", keys={"inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N", "a": "b"})
    b = payload.encode(p)
    assert b == b'{"deleted":false,"keys":{"a":"b","inchikey":"LFQSCWFLJHTTHZ-UHFFFAOYSA-N"},"smiles":"CCO","v":1}'
    assert payload.decode(b) == p


def test_tombstone_round_trips():
    p = payload.Payload(smiles="", keys={}, deleted=True)
    assert payload.decode(payload.encode(p)) == p


@pytest.mark.parametrize("bad", [b"not json", b'{"v":2,"smiles":"C","keys":{},"deleted":false}', b'{"v":1}', b"[]"])
def test_decode_rejects_garbage_and_unknown_versions(bad):
    with pytest.raises(payload.PayloadError):
        payload.decode(bad)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_payload.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/molseal/payload.py`:
```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_payload.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -- src/molseal/payload.py tests/test_payload.py
git commit -m "feat(payload): canonical JSON payload with tombstone flag

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/payload.py tests/test_payload.py
```

---

### Task 5: store module (SQLAlchemy Core, SQLite tests)

**Files:**
- Create: `src/molseal/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `metadata`, tables `tenants`, `tenant_keys`, `structures`, `structure_keys`
  - `@dataclass(frozen=True) TenantRow(tenant_id: UUID, seq_cursor: int, data_key_version: int, hmac_key_version: int)`
  - `@dataclass(frozen=True) WrappedKey(kind: str, version: int, nonce: bytes, wrapped: bytes)`
  - `@dataclass(frozen=True) StructureRecord(tenant_id: UUID, id: UUID, row_version: int, data_key_version: int, nonce: bytes, ciphertext: bytes, deleted: bool, seq: int = 0)`
  - `class StructureStore(Protocol)` with methods: `ensure_tenant(tenant_id) -> TenantRow`, `seq_cursor(tenant_id) -> int`, `get_tenant_keys(tenant_id) -> list[WrappedKey]`, `put_tenant_key(tenant_id, key: WrappedKey) -> None`, `put_structure(rec: StructureRecord, key_hmacs: dict[str, bytes]) -> int`, `get_structure(tenant_id, id) -> StructureRecord | None`, `changes_since(tenant_id, seq: int, limit: int = 10000) -> list[StructureRecord]`, `max_seq(tenant_id) -> int`, `live_count(tenant_id) -> int`, `find_by_key(tenant_id, kind: str, hmac: bytes) -> list[UUID]`
  - `class SqlStore(engine: sqlalchemy.Engine)` implementing it, plus `create_tables()`

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:
```python
from uuid import uuid4

import pytest
import sqlalchemy as sa

from molseal.store import SqlStore, StructureRecord, WrappedKey


@pytest.fixture
def store(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'molseal.db'}")
    s = SqlStore(engine)
    s.create_tables()
    return s


def rec(tenant, id_, rv=1, deleted=False, ct=b"ct"):
    return StructureRecord(tenant_id=tenant, id=id_, row_version=rv, data_key_version=1,
                           nonce=b"n" * 12, ciphertext=ct, deleted=deleted)


def test_ensure_tenant_is_idempotent(store):
    t = uuid4()
    first = store.ensure_tenant(t)
    second = store.ensure_tenant(t)
    assert first == second
    assert first.seq_cursor == 0 and first.data_key_version == 1 and first.hmac_key_version == 1


def test_tenant_keys_round_trip(store):
    t = uuid4()
    store.ensure_tenant(t)
    store.put_tenant_key(t, WrappedKey("data", 1, b"n" * 12, b"w1"))
    store.put_tenant_key(t, WrappedKey("hmac", 1, b"m" * 12, b"w2"))
    got = {(k.kind, k.version): k.wrapped for k in store.get_tenant_keys(t)}
    assert got == {("data", 1): b"w1", ("hmac", 1): b"w2"}
    assert store.get_tenant_keys(uuid4()) == []


def test_put_assigns_increasing_seq_and_changes_since_orders_by_it(store):
    t = uuid4()
    store.ensure_tenant(t)
    a, b = uuid4(), uuid4()
    s1 = store.put_structure(rec(t, a), {"inchikey": b"h" * 32})
    s2 = store.put_structure(rec(t, b), {})
    assert (s1, s2) == (1, 2)
    assert store.seq_cursor(t) == 2
    assert [r.id for r in store.changes_since(t, 0)] == [a, b]
    assert [r.id for r in store.changes_since(t, 1)] == [b]
    assert store.changes_since(t, 2) == []
    assert store.max_seq(t) == 2


def test_update_replaces_row_and_key_rows_and_bumps_seq(store):
    t = uuid4()
    store.ensure_tenant(t)
    a = uuid4()
    store.put_structure(rec(t, a, rv=1, ct=b"v1"), {"inchikey": b"h" * 32, "other": b"o" * 32})
    s = store.put_structure(rec(t, a, rv=2, ct=b"v2"), {"inchikey": b"k" * 32})
    assert s == 2
    got = store.get_structure(t, a)
    assert got.row_version == 2 and got.ciphertext == b"v2" and got.seq == 2
    assert store.find_by_key(t, "inchikey", b"h" * 32) == []
    assert store.find_by_key(t, "inchikey", b"k" * 32) == [a]
    assert store.find_by_key(t, "other", b"o" * 32) == []
    assert len(store.changes_since(t, 0)) == 1  # one row, latest state


def test_delete_is_a_write_that_bumps_seq_and_hides_keys(store):
    t = uuid4()
    store.ensure_tenant(t)
    a = uuid4()
    store.put_structure(rec(t, a), {"inchikey": b"h" * 32})
    s = store.put_structure(rec(t, a, rv=2, deleted=True), {})
    assert s == 2
    assert store.live_count(t) == 0
    assert store.find_by_key(t, "inchikey", b"h" * 32) == []
    assert store.changes_since(t, 1)[0].deleted is True


def test_find_by_key_is_tenant_scoped_and_can_return_several(store):
    t1, t2 = uuid4(), uuid4()
    store.ensure_tenant(t1)
    store.ensure_tenant(t2)
    a, b, c = uuid4(), uuid4(), uuid4()
    store.put_structure(rec(t1, a), {"inchikey": b"h" * 32})
    store.put_structure(rec(t1, b), {"inchikey": b"h" * 32})
    store.put_structure(rec(t2, c), {"inchikey": b"h" * 32})
    assert sorted(store.find_by_key(t1, "inchikey", b"h" * 32)) == sorted([a, b])
    assert store.find_by_key(t2, "inchikey", b"h" * 32) == [c]
    assert store.live_count(t1) == 2


def test_get_structure_missing_is_none(store):
    t = uuid4()
    store.ensure_tenant(t)
    assert store.get_structure(t, uuid4()) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/molseal/store.py`:
```python
"""The four tables and a SQLAlchemy Core store. No chemistry lives here.

The change sequence comes from a per-tenant cursor row updated inside the writer's
transaction. The row lock is held to commit, so sequence order equals commit order on
PostgreSQL and SQLite alike. A plain database sequence does not give that guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Integer, LargeBinary, MetaData, String,
                        Table, Uuid, delete, func, insert, select, update)

metadata = MetaData()

tenants = Table(
    "tenants", metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("seq_cursor", BigInteger, nullable=False),
    Column("data_key_version", Integer, nullable=False),
    Column("hmac_key_version", Integer, nullable=False),
)

tenant_keys = Table(
    "tenant_keys", metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("kind", String(8), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("nonce", LargeBinary, nullable=False),
    Column("wrapped", LargeBinary, nullable=False),
)

structures = Table(
    "structures", metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("row_version", Integer, nullable=False),
    Column("data_key_version", Integer, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("deleted", Boolean, nullable=False),   # index only; the authenticated flag is in the payload
    Column("seq", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    sa.Index("ix_structures_tenant_seq", "tenant_id", "seq"),
)

structure_keys = Table(
    "structure_keys", metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("kind", String(64), primary_key=True),
    Column("hmac", LargeBinary(32), nullable=False),   # index only; the authenticated keys are in the payload
    sa.Index("ix_structure_keys_lookup", "tenant_id", "kind", "hmac"),
)


@dataclass(frozen=True)
class TenantRow:
    tenant_id: UUID
    seq_cursor: int
    data_key_version: int
    hmac_key_version: int


@dataclass(frozen=True)
class WrappedKey:
    kind: str
    version: int
    nonce: bytes
    wrapped: bytes


@dataclass(frozen=True)
class StructureRecord:
    tenant_id: UUID
    id: UUID
    row_version: int
    data_key_version: int
    nonce: bytes
    ciphertext: bytes
    deleted: bool
    seq: int = 0


class StructureStore(Protocol):
    def ensure_tenant(self, tenant_id: UUID) -> TenantRow: ...
    def seq_cursor(self, tenant_id: UUID) -> int: ...
    def get_tenant_keys(self, tenant_id: UUID) -> list[WrappedKey]: ...
    def put_tenant_key(self, tenant_id: UUID, key: WrappedKey) -> None: ...
    def put_structure(self, rec: StructureRecord, key_hmacs: dict[str, bytes]) -> int: ...
    def get_structure(self, tenant_id: UUID, id: UUID) -> StructureRecord | None: ...
    def changes_since(self, tenant_id: UUID, seq: int, limit: int = 10000) -> list[StructureRecord]: ...
    def max_seq(self, tenant_id: UUID) -> int: ...
    def live_count(self, tenant_id: UUID) -> int: ...
    def find_by_key(self, tenant_id: UUID, kind: str, hmac: bytes) -> list[UUID]: ...


def _record(row) -> StructureRecord:
    return StructureRecord(
        tenant_id=row.tenant_id, id=row.id, row_version=row.row_version,
        data_key_version=row.data_key_version, nonce=row.nonce, ciphertext=row.ciphertext,
        deleted=bool(row.deleted), seq=row.seq,
    )


class SqlStore:
    def __init__(self, engine: sa.Engine) -> None:
        self.engine = engine

    def create_tables(self) -> None:
        metadata.create_all(self.engine)

    def ensure_tenant(self, tenant_id: UUID) -> TenantRow:
        with self.engine.begin() as conn:
            row = conn.execute(select(tenants).where(tenants.c.tenant_id == tenant_id)).first()
            if row is None:
                conn.execute(insert(tenants).values(tenant_id=tenant_id, seq_cursor=0,
                                                    data_key_version=1, hmac_key_version=1))
                return TenantRow(tenant_id, 0, 1, 1)
            return TenantRow(row.tenant_id, row.seq_cursor, row.data_key_version, row.hmac_key_version)

    def seq_cursor(self, tenant_id: UUID) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(tenants.c.seq_cursor).where(tenants.c.tenant_id == tenant_id)).scalar_one()

    def get_tenant_keys(self, tenant_id: UUID) -> list[WrappedKey]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(tenant_keys).where(tenant_keys.c.tenant_id == tenant_id)
                                .order_by(tenant_keys.c.kind, tenant_keys.c.version)).all()
        return [WrappedKey(r.kind, r.version, r.nonce, r.wrapped) for r in rows]

    def put_tenant_key(self, tenant_id: UUID, key: WrappedKey) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(tenant_keys).values(tenant_id=tenant_id, kind=key.kind, version=key.version,
                                                    nonce=key.nonce, wrapped=key.wrapped))

    def put_structure(self, rec: StructureRecord, key_hmacs: dict[str, bytes]) -> int:
        """One transaction: bump the tenant cursor (row lock to commit), upsert the row, replace its keys."""
        with self.engine.begin() as conn:
            seq = conn.execute(
                update(tenants).where(tenants.c.tenant_id == rec.tenant_id)
                .values(seq_cursor=tenants.c.seq_cursor + 1).returning(tenants.c.seq_cursor)
            ).scalar_one()
            values = dict(row_version=rec.row_version, data_key_version=rec.data_key_version,
                          nonce=rec.nonce, ciphertext=rec.ciphertext, deleted=rec.deleted, seq=seq,
                          updated_at=datetime.now(timezone.utc))
            res = conn.execute(update(structures)
                               .where(structures.c.tenant_id == rec.tenant_id, structures.c.id == rec.id)
                               .values(**values))
            if res.rowcount == 0:
                conn.execute(insert(structures).values(tenant_id=rec.tenant_id, id=rec.id, **values))
            conn.execute(delete(structure_keys)
                         .where(structure_keys.c.tenant_id == rec.tenant_id, structure_keys.c.id == rec.id))
            if key_hmacs:
                conn.execute(insert(structure_keys), [
                    dict(tenant_id=rec.tenant_id, id=rec.id, kind=kind, hmac=h) for kind, h in key_hmacs.items()
                ])
            return int(seq)

    def get_structure(self, tenant_id: UUID, id: UUID) -> StructureRecord | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(structures)
                               .where(structures.c.tenant_id == tenant_id, structures.c.id == id)).first()
        return None if row is None else _record(row)

    def changes_since(self, tenant_id: UUID, seq: int, limit: int = 10000) -> list[StructureRecord]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(structures)
                                .where(structures.c.tenant_id == tenant_id, structures.c.seq > seq)
                                .order_by(structures.c.seq).limit(limit)).all()
        return [_record(r) for r in rows]

    def max_seq(self, tenant_id: UUID) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.coalesce(func.max(structures.c.seq), 0))
                                    .where(structures.c.tenant_id == tenant_id)).scalar_one())

    def live_count(self, tenant_id: UUID) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count())
                                    .where(structures.c.tenant_id == tenant_id, structures.c.deleted == sa.false())
                                    ).scalar_one())

    def find_by_key(self, tenant_id: UUID, kind: str, hmac: bytes) -> list[UUID]:
        j = structure_keys.join(structures, sa.and_(structures.c.tenant_id == structure_keys.c.tenant_id,
                                                    structures.c.id == structure_keys.c.id))
        with self.engine.connect() as conn:
            rows = conn.execute(select(structure_keys.c.id).select_from(j)
                                .where(structure_keys.c.tenant_id == tenant_id, structure_keys.c.kind == kind,
                                       structure_keys.c.hmac == hmac, structures.c.deleted == sa.false())).all()
        return [r.id for r in rows]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_store.py -q`
Expected: all pass. If SQLite rejects `UPDATE ... RETURNING`, print `uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"`; it must be 3.35 or newer. Report rather than work around.

- [ ] **Step 5: Commit**

```bash
git add -- src/molseal/store.py tests/test_store.py
git commit -m "feat(store): four tables and SQLAlchemy Core store with cursor-row sequencing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/store.py tests/test_store.py
```

---

### Task 6: PostgreSQL integration test for commit-ordered sequencing

**Files:**
- Create: `tests/conftest.py`, `tests/test_store_pg.py`

**Interfaces:**
- Consumes: `SqlStore`, `tenants`, `structures` tables.
- Produces: fixture `pg_store` (skips when `MOLSEAL_TEST_DATABASE_URL` is unset).

- [ ] **Step 1: Start the dev database once**

Run: `docker compose up -d db` then `export MOLSEAL_TEST_DATABASE_URL=postgresql+psycopg://molseal:molseal@localhost:5439/molseal` (fish: `set -x MOLSEAL_TEST_DATABASE_URL postgresql+psycopg://molseal:molseal@localhost:5439/molseal`).

- [ ] **Step 2: Write the fixture and the failing test**

`tests/conftest.py`:
```python
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa

from molseal.store import SqlStore, metadata


@pytest.fixture(scope="session")
def pg_engine():
    url = os.environ.get("MOLSEAL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MOLSEAL_TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_store(pg_engine):
    return SqlStore(pg_engine)
```

`tests/test_store_pg.py`:
```python
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import insert, update

from molseal.store import StructureRecord, structures, tenants


def _rec(t, i, rv=1):
    return StructureRecord(tenant_id=t, id=i, row_version=rv, data_key_version=1,
                           nonce=b"n" * 12, ciphertext=b"ct", deleted=False)


def test_second_writer_waits_for_first_to_commit_so_seq_order_is_commit_order(pg_store, pg_engine):
    t = uuid4()
    pg_store.ensure_tenant(t)
    a, b = uuid4(), uuid4()
    seq_b = []

    # Writer A: take the cursor and hold the transaction open.
    conn_a = pg_engine.connect()
    tx_a = conn_a.begin()
    seq_a = conn_a.execute(update(tenants).where(tenants.c.tenant_id == t)
                           .values(seq_cursor=tenants.c.seq_cursor + 1)
                           .returning(tenants.c.seq_cursor)).scalar_one()
    conn_a.execute(insert(structures).values(tenant_id=t, id=a, row_version=1, data_key_version=1,
                                             nonce=b"n" * 12, ciphertext=b"ct", deleted=False, seq=seq_a,
                                             updated_at=datetime.now(timezone.utc)))

    # Writer B: a normal put, which must block on A's row lock.
    th = threading.Thread(target=lambda: seq_b.append(pg_store.put_structure(_rec(t, b), {})))
    th.start()
    time.sleep(0.5)
    assert th.is_alive(), "writer B should be blocked until A commits"
    assert pg_store.changes_since(t, 0) == [], "a reader sees nothing while A is uncommitted"

    tx_a.commit()
    conn_a.close()
    th.join(timeout=5)
    assert not th.is_alive()
    assert seq_b == [seq_a + 1]
    assert [r.id for r in pg_store.changes_since(t, 0)] == [a, b]
    assert pg_store.seq_cursor(t) == seq_a + 1


def test_round_trip_on_postgres(pg_store):
    t = uuid4()
    pg_store.ensure_tenant(t)
    i = uuid4()
    s = pg_store.put_structure(_rec(t, i), {"inchikey": b"h" * 32})
    got = pg_store.get_structure(t, i)
    assert got.seq == s and got.ciphertext == b"ct"
    assert pg_store.find_by_key(t, "inchikey", b"h" * 32) == [i]
```

- [ ] **Step 3: Run with and without the URL**

Run: `uv run pytest tests/test_store_pg.py -q` with the env var set. Expected: `2 passed`.
Run: `env -u MOLSEAL_TEST_DATABASE_URL uv run pytest tests/test_store_pg.py -q`. Expected: `2 skipped`.

- [ ] **Step 4: Commit**

```bash
git add -- tests/conftest.py tests/test_store_pg.py
git commit -m "test(store): PostgreSQL proves the cursor row orders sequences by commit

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- tests/conftest.py tests/test_store_pg.py
```

---

### Task 7: engine module, part 1: ingest and substructure

**Files:**
- Create: `src/molseal/engine.py`, `tests/mols.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Produces (engine):
  - `FP_BITS = 2048`, `FP_BYTES = 256`, `MORGAN_RADIUS = 2`, `QueryKind = Literal["smiles", "smarts"]`
  - `class StructureError(ValueError)`
  - `canonicalize(smiles: str) -> str` (raises `StructureError`)
  - `parse_many(smiles: Sequence[str]) -> list[Chem.Mol | None]`
  - `morgan_matrix(mols: Sequence[Chem.Mol], num_threads: int = -1) -> np.ndarray` shape `(n, 256)` uint8
  - `class StructureIndex` with: `last_seq: int`, `__len__()` (live count), `positions` property, `has(id, row_version) -> bool`, `add_batch(items: Sequence[tuple[UUID, str, int]], num_threads=-1) -> int`, `substructure(query, kind="smiles", max_results=None, num_threads=-1) -> list[UUID]`, `count_substructure(query, kind="smiles", num_threads=-1) -> int`
  - Task 8 adds `mark_dead`, `similar`, `to_parts`, `from_parts`.
- Produces (tests/mols.py): `molecules() -> list[str]` deterministic list of a few hundred unique canonical SMILES, and `STEREO = ["C[C@H](N)C(=O)O", "C[C@@H](N)C(=O)O", "CC(N)C(=O)O"]` included in it.

- [ ] **Step 1: Write the offline molecule generator**

`tests/mols.py`:
```python
"""A deterministic, offline set of a few hundred valid molecules for oracle tests."""

from rdkit import Chem, RDLogger

RDLogger.DisableLog("rdApp.*")

TEMPLATES = [
    "{R}c1ccccc1", "{R}C1CCCCC1", "{R}c1ccncc1", "{R}c1ccc(cc1){S}", "{R}C(=O)N{S}", "{R}OC(=O){S}",
    "{R}NC(=O)c1ccccc1", "{R}S(=O)(=O)N", "{R}C#N", "{R}C1CC1", "{R}c1ccc2[nH]ccc2c1", "{R}C1CCNCC1",
    "{R}c1cnc[nH]1", "{R}N1CCOCC1", "{R}c1ccc2ccccc2c1", "{R}C(=O)O", "{R}C(=O){S}", "{R}N{S}",
]
R_GROUPS = [
    "C", "CC", "CCC", "CC(C)C", "CCO", "CCN", "CCCl", "CF", "C(F)(F)F", "CO", "CN(C)C", "Cc1ccccc1",
    "CCC(=O)O", "CCS", "c1ccccc1", "C1CCCC1", "CC=C", "CC#C", "CCBr", "C(=O)C",
]
STEREO = ["C[C@H](N)C(=O)O", "C[C@@H](N)C(=O)O", "CC(N)C(=O)O"]


def molecules() -> list[str]:
    seen: dict[str, None] = {}
    for t in TEMPLATES:
        for r in R_GROUPS:
            for s in ("C", "CCO", "c1ccccc1"):
                mol = Chem.MolFromSmiles(t.format(R=r, S=s))
                if mol is not None:
                    seen[Chem.MolToSmiles(mol)] = None
    for s in STEREO:
        seen[Chem.MolToSmiles(Chem.MolFromSmiles(s))] = None
    return list(seen)
```

Run: `uv run python -c "import tests.mols as m; print(len(m.molecules()))"`. Expected: a few hundred (any number above 200 is fine; note it in the commit message).

- [ ] **Step 2: Write the failing tests**

`tests/test_engine.py`:
```python
from uuid import uuid4

import pytest
from rdkit import Chem

from molseal import engine
from tests.mols import molecules

QUERIES = [
    ("c1ccccc1", "smiles"), ("CCO", "smiles"), ("C(=O)N", "smiles"), ("C#N", "smiles"), ("C1CC1", "smiles"),
    ("[R]", "smarts"), ("[r5]", "smarts"), ("[x3]", "smarts"), ("[Cl,Br]", "smarts"), ("[#7;!$(N-C=O)]", "smarts"),
    ("c1ccc2[nH]ccc2c1", "smiles"), ("C[C@H](N)C(=O)O", "smiles"), ("[CX4][OX2H]", "smarts"), ("FC(F)F", "smiles"),
]


@pytest.fixture(scope="module")
def dataset():
    smiles = molecules()
    ids = [uuid4() for _ in smiles]
    mols = [Chem.MolFromSmiles(s) for s in smiles]
    return ids, smiles, mols


@pytest.fixture(scope="module")
def index(dataset):
    ids, smiles, _ = dataset
    idx = engine.StructureIndex()
    added = idx.add_batch([(i, s, 1) for i, s in zip(ids, smiles)])
    assert added == len(ids)
    return idx


def brute_force(dataset, query, kind):
    ids, _, mols = dataset
    q = Chem.MolFromSmarts(query) if kind == "smarts" else Chem.MolFromSmiles(query)
    return {i for i, m in zip(ids, mols) if m.HasSubstructMatch(q, useChirality=False)}


def test_canonicalize_round_trips_and_rejects_garbage():
    assert engine.canonicalize("OCC") == "CCO"
    assert engine.canonicalize("C1=CC=CC=C1") == "c1ccccc1"
    with pytest.raises(engine.StructureError):
        engine.canonicalize("not a molecule")


def test_morgan_matrix_shape_and_known_bit_count():
    m = engine.morgan_matrix([Chem.MolFromSmiles("CCO")])
    assert m.shape == (1, engine.FP_BYTES) and m.dtype.name == "uint8"
    import numpy as np
    assert 1 <= int(np.bitwise_count(m).sum()) <= 20


@pytest.mark.parametrize("query,kind", QUERIES)
def test_substructure_matches_brute_force_with_chirality_off(index, dataset, query, kind):
    expected = brute_force(dataset, query, kind)
    got = index.substructure(query, kind=kind)
    assert set(got) == expected
    assert len(got) == len(set(got))
    assert index.count_substructure(query, kind=kind) == len(expected)


def test_chiral_query_matches_both_enantiomers_and_the_flat_one(index, dataset):
    expected = brute_force(dataset, "C[C@H](N)C(=O)O", "smiles")
    assert len(expected) >= 3
    assert set(index.substructure("C[C@H](N)C(=O)O")) == expected


def test_max_results_caps_and_defaults_to_uncapped(index, dataset):
    total = index.count_substructure("c1ccccc1")
    assert total > 10
    assert len(index.substructure("c1ccccc1")) == total
    assert len(index.substructure("c1ccccc1", max_results=10)) == 10


def test_empty_index_and_no_match_return_empty():
    idx = engine.StructureIndex()
    assert idx.substructure("CCO") == []
    assert idx.count_substructure("CCO") == 0
    idx.add_batch([(uuid4(), "CCO", 1)])
    assert idx.substructure("c1ccccc1") == []


def test_invalid_query_raises():
    idx = engine.StructureIndex()
    idx.add_batch([(uuid4(), "CCO", 1)])
    with pytest.raises(engine.StructureError):
        idx.substructure("C(", kind="smiles")
    with pytest.raises(engine.StructureError):
        idx.substructure("[", kind="smarts")


def test_add_batch_skips_unparseable_and_already_seen():
    idx = engine.StructureIndex()
    a = uuid4()
    assert idx.add_batch([(a, "CCO", 1), (uuid4(), "garbage(", 1)]) == 1
    assert idx.add_batch([(a, "CCO", 1)]) == 0
    assert idx.has(a, 1) and not idx.has(a, 2)
    assert len(idx) == 1 and idx.positions == 1
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_engine.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Write the implementation**

`src/molseal/engine.py`:
```python
"""In-memory structure index: RDKit SubstructLibrary for substructure, a packed Morgan
matrix for similarity, and a live mask so edits and deletes work on an append-only library.

No SQL lives here. Positions are integers; the vault maps ids to its own tables.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal
from uuid import UUID

import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.rdSubstructLibrary import AddPatterns, CachedTrustedSmilesMolHolder, SubstructLibrary

RDLogger.DisableLog("rdApp.*")

FP_BITS = 2048
FP_BYTES = FP_BITS // 8
MORGAN_RADIUS = 2
QueryKind = Literal["smiles", "smarts"]

_MORGAN = rdFingerprintGenerator.GetMorganGenerator(radius=MORGAN_RADIUS, fpSize=FP_BITS)


class StructureError(ValueError):
    """A SMILES or query could not be parsed."""


def canonicalize(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise StructureError(f"cannot parse SMILES: {smiles!r}")
    return Chem.MolToSmiles(mol)


def parse_many(smiles: Sequence[str]) -> list[Chem.Mol | None]:
    # ponytail: serial parse; swap for a multithreaded supplier or a process pool when ingest time matters
    return [Chem.MolFromSmiles(s) for s in smiles]


def _pack(fp) -> np.ndarray:
    bits = np.frombuffer(fp.ToBitString().encode("ascii"), dtype=np.uint8) - ord("0")
    return np.packbits(bits)


def morgan_matrix(mols: Sequence[Chem.Mol], num_threads: int = -1) -> np.ndarray:
    """Morgan radius-2, 2048-bit fingerprints packed to (n, 256) uint8."""
    if not mols:
        return np.zeros((0, FP_BYTES), dtype=np.uint8)
    fps = _MORGAN.GetFingerprints(list(mols), numThreads=num_threads)
    return np.stack([_pack(fp) for fp in fps])


def _parse_query(query: str, kind: QueryKind) -> Chem.Mol:
    q = Chem.MolFromSmarts(query) if kind == "smarts" else Chem.MolFromSmiles(query)
    if q is None:
        raise StructureError(f"cannot parse {kind} query: {query!r}")
    return q


class StructureIndex:
    def __init__(self) -> None:
        self._lib = SubstructLibrary(CachedTrustedSmilesMolHolder())
        self._patterns = False          # True once AddPatterns has run (initial build)
        self._ids: list[UUID] = []      # position -> id
        self._pos_of: dict[UUID, int] = {}   # id -> latest position (live or dead)
        self._row_versions: dict[UUID, int] = {}
        self._n = 0
        self._cap = 0
        self._live = np.zeros(0, dtype=bool)
        self._fps = np.zeros((0, FP_BYTES), dtype=np.uint8)
        self._pop = np.zeros(0, dtype=np.uint16)
        self._order_dirty = False       # True once any position has died
        self.last_seq = 0

    # ----- bookkeeping -----

    def __len__(self) -> int:
        return int(self._live[: self._n].sum())

    @property
    def positions(self) -> int:
        return self._n

    def has(self, id: UUID, row_version: int) -> bool:
        return self._row_versions.get(id, 0) >= row_version

    def _ensure_capacity(self, extra: int) -> None:
        need = self._n + extra
        if need <= self._cap:
            return
        cap = max(need, self._cap * 2, 1024)
        live = np.zeros(cap, dtype=bool); live[: self._n] = self._live[: self._n]
        fps = np.zeros((cap, FP_BYTES), dtype=np.uint8); fps[: self._n] = self._fps[: self._n]
        pop = np.zeros(cap, dtype=np.uint16); pop[: self._n] = self._pop[: self._n]
        self._live, self._fps, self._pop, self._cap = live, fps, pop, cap

    def _apply_order(self) -> None:
        """RDKit's library cannot delete; a search order listing only live positions is the sanctioned
        workaround. AddMol does not extend an existing order, so this runs after every append."""
        if self._order_dirty:
            self._lib.SetSearchOrder([int(p) for p in np.flatnonzero(self._live[: self._n])])

    # ----- ingest -----

    def add_batch(self, items: Sequence[tuple[UUID, str, int]], num_threads: int = -1) -> int:
        """Add (id, canonical_smiles, row_version) items. Skips rows already seen at this version
        or newer, and rows RDKit cannot parse. An id seen before gets its old position marked dead."""
        fresh = [it for it in items if not self.has(it[0], it[2])]
        if not fresh:
            return 0
        mols = parse_many([s for _, s, _ in fresh])
        keep = [(it, m) for it, m in zip(fresh, mols) if m is not None]
        if not keep:
            return 0
        fps = morgan_matrix([m for _, m in keep], num_threads)
        self._ensure_capacity(len(keep))
        for (id_, _, rv), m in keep:
            old = self._pos_of.get(id_)
            if old is not None:
                self._live[old] = False
                self._order_dirty = True
            self._lib.AddMol(m)
            pos = self._n
            self._ids.append(id_)
            self._pos_of[id_] = pos
            self._row_versions[id_] = rv
            self._live[pos] = True
            self._n += 1
        start = self._n - len(keep)
        self._fps[start : self._n] = fps
        self._pop[start : self._n] = np.bitwise_count(fps).sum(axis=1).astype(np.uint16)
        self._apply_order()
        return len(keep)

    def _ensure_patterns(self, num_threads: int) -> None:
        if not self._patterns and self._n > 0:
            AddPatterns(self._lib, numThreads=num_threads)
            self._patterns = True

    # ----- substructure -----

    def substructure(self, query: str, kind: QueryKind = "smiles", max_results: int | None = None,
                     num_threads: int = -1) -> list[UUID]:
        if len(self) == 0:
            return []
        q = _parse_query(query, kind)
        self._ensure_patterns(num_threads)
        hits = self._lib.GetMatches(q, recursionPossible=True, useChirality=False, useQueryQueryMatches=False,
                                    numThreads=num_threads, maxResults=-1 if max_results is None else int(max_results))
        out: dict[UUID, None] = {}
        for pos in hits:
            id_ = self._ids[pos]
            if self._live[pos] and self._pos_of[id_] == pos:
                out[id_] = None
        return list(out)

    def count_substructure(self, query: str, kind: QueryKind = "smiles", num_threads: int = -1) -> int:
        if len(self) == 0:
            return 0
        q = _parse_query(query, kind)
        self._ensure_patterns(num_threads)
        return int(self._lib.CountMatches(q, recursionPossible=True, useChirality=False,
                                          useQueryQueryMatches=False, numThreads=num_threads))
```

If Boost.Python rejects the keyword names on `GetMatches` / `CountMatches`, pass them positionally in this order: `query, recursionPossible, useChirality, useQueryQueryMatches, numThreads[, maxResults]`. Do not change the values.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_engine.py -q`
Expected: all pass. If a brute-force comparison fails only for the `[R]`/`[r5]`/`[x3]` ring queries, that is the trusted holder skipping ring perception; switch the holder to `CachedSmilesMolHolder` in `__init__` and note the change in the commit body (the spec allows it; it costs parse time per match candidate).

- [ ] **Step 6: Commit**

```bash
git add -- src/molseal/engine.py tests/mols.py tests/test_engine.py
git commit -m "feat(engine): SubstructLibrary index with Morgan matrix and brute-force oracle tests

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/engine.py tests/mols.py tests/test_engine.py
```

---

### Task 8: engine module, part 2: similarity, edits and deletes, serialization

**Files:**
- Modify: `src/molseal/engine.py` (add methods to `StructureIndex`)
- Test: `tests/test_engine.py` (append)

**Interfaces:**
- Produces on `StructureIndex`:
  - `mark_dead(id: UUID, row_version: int) -> bool` (False if already at this version or newer)
  - `similar(query_smiles: str, threshold: float = 0.7, top_k: int | None = None) -> list[tuple[UUID, float]]` sorted by score descending
  - `to_parts() -> dict[str, bytes]` with keys `meta, lib, ids, live, rv, fps`
  - `StructureIndex.from_parts(parts: dict[str, bytes]) -> StructureIndex` (classmethod)

- [ ] **Step 1: Append the failing tests**

Append to `tests/test_engine.py`:
```python
import numpy as np
from rdkit import DataStructs
from rdkit.Chem import rdFingerprintGenerator


def brute_similar(dataset, query, threshold):
    ids, _, mols = dataset
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    qfp = gen.GetFingerprint(Chem.MolFromSmiles(query))
    sims = DataStructs.BulkTanimotoSimilarity(qfp, [gen.GetFingerprint(m) for m in mols])
    return {i: s for i, s in zip(ids, sims) if s >= threshold}


@pytest.mark.parametrize("query", ["CCOc1ccccc1", "CC(C)Cc1ccncc1", "NC(=O)c1ccccc1", "CCCl"])
def test_similar_matches_bulk_tanimoto(index, dataset, query):
    expected = brute_similar(dataset, query, 0.4)
    got = index.similar(query, threshold=0.4)
    assert {i for i, _ in got} == set(expected)
    for i, s in got:
        assert abs(s - expected[i]) < 1e-9
    scores = [s for _, s in got]
    assert scores == sorted(scores, reverse=True)


def test_similar_top_k_and_self_hit(index, dataset):
    ids, smiles, _ = dataset
    got = index.similar(smiles[5], threshold=0.0, top_k=3)
    assert len(got) == 3
    assert got[0][0] == ids[5] and abs(got[0][1] - 1.0) < 1e-9


def test_similar_on_empty_index_and_bad_query():
    idx = engine.StructureIndex()
    assert idx.similar("CCO") == []
    idx.add_batch([(uuid4(), "CCO", 1)])
    with pytest.raises(engine.StructureError):
        idx.similar("C(")


def test_edit_hides_the_old_structure_and_never_duplicates_the_id():
    idx = engine.StructureIndex()
    x, y = uuid4(), uuid4()
    idx.add_batch([(x, "CCO", 1), (y, "CCN", 1)])
    assert set(idx.substructure("CCO")) == {x}
    idx.add_batch([(x, "c1ccccc1", 2)])
    assert idx.substructure("CCO") == []            # old structure of x is gone
    assert idx.substructure("c1ccccc1") == [x]      # new one is there, once
    assert len(idx) == 2 and idx.positions == 3
    assert {i for i, _ in idx.similar("c1ccccc1", threshold=0.99)} == {x}
    assert {i for i, _ in idx.similar("CCO", threshold=0.99)} == set()


def test_delete_removes_from_both_searches_and_caps_stay_honest():
    idx = engine.StructureIndex()
    ids = [uuid4() for _ in range(5)]
    idx.add_batch([(i, "CCO", 1) for i in ids])
    assert idx.mark_dead(ids[0], 2) and idx.mark_dead(ids[1], 2)
    assert not idx.mark_dead(ids[1], 2)             # already at this version
    assert len(idx) == 3
    assert set(idx.substructure("CCO", max_results=5)) == set(ids[2:])
    assert idx.count_substructure("CCO") == 3
    assert {i for i, _ in idx.similar("CCO", threshold=0.99)} == set(ids[2:])


def test_append_after_a_delete_is_visible():
    idx = engine.StructureIndex()
    a, b = uuid4(), uuid4()
    idx.add_batch([(a, "CCO", 1)])
    idx.mark_dead(a, 2)
    idx.add_batch([(b, "CCO", 1)])
    assert idx.substructure("CCO") == [b]


def test_parts_round_trip_preserves_results_and_dead_mask(index, dataset):
    idx = engine.StructureIndex()
    a, b, c = uuid4(), uuid4(), uuid4()
    idx.add_batch([(a, "CCO", 1), (b, "c1ccccc1", 1), (c, "CCN", 1)])
    idx.mark_dead(c, 2)
    idx.add_batch([(a, "CCCl", 2)])
    idx.last_seq = 42
    parts = idx.to_parts()
    back = engine.StructureIndex.from_parts(parts)
    assert back.last_seq == 42 and len(back) == 2 and back.positions == 4
    assert back.substructure("CCCl") == [a] and back.substructure("CCO") == [] and back.substructure("CCN") == []
    assert back.has(a, 2) and back.has(c, 2) and not back.has(c, 3)
    assert {i for i, _ in back.similar("c1ccccc1", threshold=0.99)} == {b}
    assert back.add_batch([(uuid4(), "CCO", 1)]) == 1
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `uv run pytest tests/test_engine.py -q -k "similar or edit or delete or append_after or parts"`
Expected: FAIL with `AttributeError` for the missing methods.

- [ ] **Step 3: Add the methods to `StructureIndex`**

Append inside the class in `src/molseal/engine.py`:
```python
    # ----- deletes -----

    def mark_dead(self, id: UUID, row_version: int) -> bool:
        """Record a tombstone at row_version. Returns False if this version was already applied."""
        if self.has(id, row_version):
            return False
        self._row_versions[id] = row_version
        pos = self._pos_of.get(id)
        if pos is not None and self._live[pos]:
            self._live[pos] = False
            self._order_dirty = True
            self._apply_order()
        return True

    # ----- similarity -----

    def similar(self, query_smiles: str, threshold: float = 0.7, top_k: int | None = None) -> list[tuple[UUID, float]]:
        if len(self) == 0:
            return []
        q = _parse_query(query_smiles, "smiles")
        qfp = morgan_matrix([q])[0]
        qpop = int(np.bitwise_count(qfp).sum())
        q64 = qfp.view(np.uint64)
        n = self._n
        sims = np.empty(n, dtype=np.float64)
        step = 250_000
        for s in range(0, n, step):
            e = min(n, s + step)
            rows = self._fps[s:e].view(np.uint64)
            inter = np.bitwise_count(rows & q64).sum(axis=1).astype(np.float64)
            denom = self._pop[s:e].astype(np.float64) + qpop - inter
            sims[s:e] = np.divide(inter, denom, out=np.zeros_like(inter), where=denom > 0)
        sims[~self._live[:n]] = -1.0
        idx = np.flatnonzero(sims >= threshold)
        if top_k is not None and len(idx) > top_k:
            idx = idx[np.argpartition(-sims[idx], top_k - 1)[:top_k]]
        idx = idx[np.argsort(-sims[idx], kind="stable")]
        return [(self._ids[i], float(sims[i])) for i in idx]

    # ----- serialization (the snapshot module encrypts these parts) -----

    def to_parts(self) -> dict[str, bytes]:
        n = self._n
        meta = {"n": n, "last_seq": self.last_seq, "patterns": self._patterns}
        return {
            "meta": json.dumps(meta, sort_keys=True).encode("ascii"),
            "lib": bytes(self._lib.Serialize()),
            "ids": b"".join(i.bytes for i in self._ids),
            "live": self._live[:n].tobytes(),
            "rv": np.array([self._row_versions[i] for i in self._ids], dtype=np.uint32).tobytes(),
            "fps": self._fps[:n].tobytes(),
        }

    @classmethod
    def from_parts(cls, parts: dict[str, bytes]) -> "StructureIndex":
        meta = json.loads(parts["meta"])
        n = int(meta["n"])
        self = cls()
        self._lib = SubstructLibrary(parts["lib"])
        self._patterns = bool(meta["patterns"])
        self.last_seq = int(meta["last_seq"])
        self._ids = [UUID(bytes=parts["ids"][i * 16 : (i + 1) * 16]) for i in range(n)]
        live = np.frombuffer(parts["live"], dtype=bool)[:n]
        rvs = np.frombuffer(parts["rv"], dtype=np.uint32)[:n]
        fps = np.frombuffer(parts["fps"], dtype=np.uint8).reshape(n, FP_BYTES)
        self._ensure_capacity(n)
        self._live[:n] = live
        self._fps[:n] = fps
        self._pop[:n] = np.bitwise_count(fps).sum(axis=1).astype(np.uint16)
        self._n = n
        for pos, (id_, rv) in enumerate(zip(self._ids, rvs)):
            self._pos_of[id_] = pos                    # later positions win
            self._row_versions[id_] = max(int(rv), self._row_versions.get(id_, 0))
        self._order_dirty = not bool(self._live[:n].all())
        self._apply_order()
        return self
```

If `SubstructLibrary(parts["lib"])` does not accept bytes in this RDKit build, use `lib = SubstructLibrary(); lib.InitFromStream(...)` only if a bytes-capable stream works; the reviewer verified the bytes constructor on 2025.03.3, so report rather than improvise if it fails.

If `CountMatches` turns out to ignore the search order (count includes dead positions in `test_delete_removes_from_both_searches_and_caps_stay_honest`), implement `count_substructure` as `len(self.substructure(query, kind, None, num_threads))` and note it in the commit body.

- [ ] **Step 4: Run the whole engine suite**

Run: `uv run pytest tests/test_engine.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -- src/molseal/engine.py tests/test_engine.py
git commit -m "feat(engine): Tanimoto over packed Morgan matrix, dead-position mask, parts round trip

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/engine.py tests/test_engine.py
```

---

### Task 9: snapshot module

**Files:**
- Create: `src/molseal/snapshot.py`
- Test: `tests/test_snapshot.py`

**Interfaces:**
- Consumes: `crypto.seal_chunks`, `crypto.open_chunks`, `crypto.SealError`, `crypto.CHUNK_SIZE`, `StructureIndex.to_parts/from_parts`
- Produces:
  - `FORMAT_VERSION = 1`, `MAGIC = b"MOLSEAL1"`, `PART_ORDER = ("meta", "lib", "ids", "live", "rv", "fps")`
  - `class SnapshotError(Exception)`
  - `aad_prefix(tenant_id: UUID, key_version: int, last_seq: int) -> bytes`
  - `save(index, path: Path, data_key: bytes, tenant_id: UUID, key_version: int, chunk_size: int = CHUNK_SIZE) -> None`
  - `load(path: Path, data_key: bytes, tenant_id: UUID, key_version: int) -> StructureIndex`

File layout: `MAGIC | >I header_len | header JSON | repeated: >Q ct_len | 12-byte nonce | ciphertext`. The header's tenant, key version, last_seq and chunk count are all inside every chunk's AAD, so editing the header breaks decryption.

- [ ] **Step 1: Write the failing tests**

`tests/test_snapshot.py`:
```python
import os
import struct
from uuid import uuid4

import pytest

from molseal import engine, snapshot

KEY = os.urandom(32)


def make_index():
    idx = engine.StructureIndex()
    a, b, c = uuid4(), uuid4(), uuid4()
    idx.add_batch([(a, "CCO", 1), (b, "c1ccccc1", 1), (c, "CCN", 1)])
    idx.mark_dead(c, 2)
    idx.last_seq = 7
    return idx, a, b


def test_round_trip_with_several_chunks(tmp_path):
    idx, a, b = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1, chunk_size=500)
    assert p.read_bytes()[:8] == snapshot.MAGIC
    back = snapshot.load(p, KEY, t, 1)
    assert back.last_seq == 7 and len(back) == 2 and back.positions == 3
    assert back.substructure("CCO") == [a] and back.substructure("CCN") == []
    assert {i for i, _ in back.similar("c1ccccc1", threshold=0.99)} == {b}


def test_other_tenant_key_version_or_key_is_refused(tmp_path):
    idx, _, _ = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, uuid4(), 1)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 2)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, os.urandom(32), t, 1)


def test_header_edit_chunk_tamper_truncation_and_reorder_are_refused(tmp_path):
    idx, _, _ = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1, chunk_size=500)
    raw = p.read_bytes()
    (hlen,) = struct.unpack(">I", raw[8:12])
    header = raw[12 : 12 + hlen]
    body = raw[12 + hlen :]

    edited = raw[:12] + header.replace(b'"last_seq": 7', b'"last_seq": 8') + body
    assert edited != raw
    p.write_bytes(edited)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 1)

    flipped = bytearray(raw)
    flipped[-1] ^= 1
    p.write_bytes(bytes(flipped))
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 1)

    p.write_bytes(raw[:-50])
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 1)

    # swap the first two chunks
    off = 12 + hlen
    chunks = []
    while off < len(raw):
        (clen,) = struct.unpack(">Q", raw[off : off + 8])
        chunks.append(raw[off : off + 8 + 12 + clen])
        off += 8 + 12 + clen
    assert len(chunks) >= 2
    p.write_bytes(raw[: 12 + hlen] + chunks[1] + chunks[0] + b"".join(chunks[2:]))
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 1)


def test_not_a_snapshot_is_refused(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"hello")
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, uuid4(), 1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_snapshot.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/molseal/snapshot.py`:
```python
"""Encrypted, chunked on-disk snapshot of a StructureIndex."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from uuid import UUID

from .crypto import CHUNK_SIZE, NONCE_LEN, SealError, open_chunks, seal_chunks
from .engine import StructureIndex

FORMAT_VERSION = 1
MAGIC = b"MOLSEAL1"
PART_ORDER = ("meta", "lib", "ids", "live", "rv", "fps")


class SnapshotError(Exception):
    pass


def aad_prefix(tenant_id: UUID, key_version: int, last_seq: int) -> bytes:
    return MAGIC + tenant_id.bytes + struct.pack(">IIQ", FORMAT_VERSION, key_version, last_seq)


def save(index: StructureIndex, path: Path, data_key: bytes, tenant_id: UUID, key_version: int,
         chunk_size: int = CHUNK_SIZE) -> None:
    parts = index.to_parts()
    blob = b"".join(struct.pack(">Q", len(parts[k])) + parts[k] for k in PART_ORDER)
    chunks = seal_chunks(data_key, blob, aad_prefix(tenant_id, key_version, index.last_seq), chunk_size)
    header = json.dumps({"format": FORMAT_VERSION, "tenant_id": str(tenant_id), "key_version": key_version,
                         "last_seq": index.last_seq, "chunk_count": len(chunks)}, sort_keys=True).encode("ascii")
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack(">I", len(header)))
        f.write(header)
        for nonce, ct in chunks:
            f.write(struct.pack(">Q", len(ct)))
            f.write(nonce)
            f.write(ct)
    os.replace(tmp, path)


def load(path: Path, data_key: bytes, tenant_id: UUID, key_version: int) -> StructureIndex:
    # ponytail: whole file in memory; stream chunk by chunk when snapshots pass a few GB
    data = Path(path).read_bytes()
    if data[:8] != MAGIC or len(data) < 12:
        raise SnapshotError("not a molseal snapshot")
    (hlen,) = struct.unpack(">I", data[8:12])
    try:
        header = json.loads(data[12 : 12 + hlen])
    except ValueError as exc:
        raise SnapshotError("bad snapshot header") from exc
    if header.get("format") != FORMAT_VERSION:
        raise SnapshotError("unknown snapshot format")
    if header.get("tenant_id") != str(tenant_id) or header.get("key_version") != key_version:
        raise SnapshotError("snapshot belongs to another tenant or key version")
    off = 12 + hlen
    chunks = []
    for _ in range(int(header["chunk_count"])):
        if off + 8 + NONCE_LEN > len(data):
            raise SnapshotError("snapshot is truncated")
        (clen,) = struct.unpack(">Q", data[off : off + 8])
        off += 8
        nonce, ct = data[off : off + NONCE_LEN], data[off + NONCE_LEN : off + NONCE_LEN + clen]
        off += NONCE_LEN + clen
        if len(ct) != clen:
            raise SnapshotError("snapshot is truncated")
        chunks.append((nonce, ct))
    if off != len(data):
        raise SnapshotError("snapshot has trailing bytes")
    try:
        blob = open_chunks(data_key, chunks, aad_prefix(tenant_id, key_version, int(header["last_seq"])))
    except SealError as exc:
        raise SnapshotError("snapshot did not open: wrong key, edited header, or tampered chunk") from exc
    parts: dict[str, bytes] = {}
    off = 0
    for k in PART_ORDER:
        (plen,) = struct.unpack(">Q", blob[off : off + 8])
        off += 8
        parts[k] = blob[off : off + plen]
        off += plen
    index = StructureIndex.from_parts(parts)
    index.last_seq = int(header["last_seq"])
    return index
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_snapshot.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -- src/molseal/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): chunk-encrypted index snapshot with labeled header

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/snapshot.py tests/test_snapshot.py
```

---

### Task 10: vault facade

**Files:**
- Create: `src/molseal/vault.py`
- Test: `tests/test_vault.py`

**Interfaces:**
- Consumes: everything from Tasks 2 to 9.
- Produces:
  - `class VaultError(Exception)`, `class NotFound(VaultError)`
  - `class Vault(store: StructureStore, key_provider: KeyProvider, tenant_id: UUID, snapshot_dir: Path | None = None, num_threads: int = -1, refresh_batch: int = 10000)`
  - methods: `put(id, smiles, exact_keys=None) -> int`, `delete(id) -> int`, `get(id) -> Payload | None`, `find_exact(kind, value) -> list[UUID]`, `substructure(query, kind="smiles", max_results=None) -> list[UUID]`, `count_substructure(query, kind="smiles") -> int`, `similar(query_smiles, threshold=0.7, top_k=None) -> list[tuple[UUID, float]]`, `refresh() -> int`, `rebuild() -> None`, `save_snapshot() -> Path`, `__len__()`
  - attributes: `tenant_id`, `keys: TenantKeys`, `index: StructureIndex`, `store`

- [ ] **Step 1: Write the failing tests**

`tests/test_vault.py`:
```python
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, insert, update

from molseal import crypto, engine, keys
from molseal.store import SqlStore, structure_keys, structures, tenants
from molseal.vault import NotFound, Vault


@pytest.fixture
def env(tmp_path):
    sa_engine = sa.create_engine(f"sqlite:///{tmp_path / 'v.db'}")
    store = SqlStore(sa_engine)
    store.create_tables()
    kp = tmp_path / "kek.key"
    keys.generate_key_file(kp)
    return store, keys.FileKeyProvider(kp), tmp_path / "snap"


@pytest.fixture
def vault(env):
    store, kp, snap = env
    return Vault(store, kp, uuid4(), snapshot_dir=snap)


def test_put_get_round_trip_stores_canonical_smiles(vault):
    i = uuid4()
    seq = vault.put(i, "OCC", {"inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"})
    assert seq == 1
    p = vault.get(i)
    assert p.smiles == "CCO" and p.keys == {"inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"} and not p.deleted
    assert vault.get(uuid4()) is None
    assert len(vault) == 1


def test_unparseable_smiles_writes_nothing(vault):
    with pytest.raises(engine.StructureError):
        vault.put(uuid4(), "garbage(")
    assert vault.store.seq_cursor(vault.tenant_id) == 0
    assert vault.store.live_count(vault.tenant_id) == 0


def test_read_your_writes_for_put_edit_and_delete(vault):
    x = uuid4()
    vault.put(x, "CCO")
    assert vault.substructure("CCO") == [x]
    vault.put(x, "c1ccccc1")
    assert vault.substructure("CCO") == [] and vault.substructure("c1ccccc1") == [x]
    assert vault.get(x).smiles == "c1ccccc1"
    vault.delete(x)
    assert vault.substructure("c1ccccc1") == [] and vault.get(x) is None and len(vault) == 0
    with pytest.raises(NotFound):
        vault.delete(uuid4())


def test_find_exact_verifies_against_the_payload(vault):
    a, b = uuid4(), uuid4()
    vault.put(a, "CCO", {"inchikey": "AAA"})
    vault.put(b, "CCN", {"inchikey": "BBB"})
    assert vault.find_exact("inchikey", "AAA") == [a]
    assert vault.find_exact("inchikey", "nope") == []
    assert vault.find_exact("other", "AAA") == []
    # A writer copies A's hmac row onto B. The index says B; the payload says no.
    h = keys.exact_key_hmac(vault.keys.hmac_key, "inchikey", "AAA")
    with vault.store.engine.begin() as conn:
        conn.execute(update(structure_keys).where(structure_keys.c.id == b).values(hmac=h))
    assert vault.find_exact("inchikey", "AAA") == [a]
    vault.delete(a)
    assert vault.find_exact("inchikey", "AAA") == []


def test_a_second_instance_sees_writes_through_the_cursor(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t)
    v2 = Vault(store, kp, t)
    assert v1.keys == v2.keys
    x = uuid4()
    v1.put(x, "CCO")
    assert v2.substructure("CCO") == [x]
    assert {i for i, _ in v2.similar("CCO", threshold=0.99)} == {x}
    v1.delete(x)
    assert v2.count_substructure("CCO") == 0


def test_rollback_in_the_store_triggers_a_rebuild(vault):
    ids = [uuid4() for _ in range(3)]
    for i in ids:
        vault.put(i, "CCO")
    assert len(vault) == 3
    with vault.store.engine.begin() as conn:
        conn.execute(delete(structures).where(structures.c.id == ids[2]))
        conn.execute(delete(structure_keys).where(structure_keys.c.id == ids[2]))
        conn.execute(update(tenants).where(tenants.c.tenant_id == vault.tenant_id).values(seq_cursor=2))
    assert set(vault.substructure("CCO")) == set(ids[:2])
    assert len(vault) == 2


def test_snapshot_save_load_and_catch_up(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t, snapshot_dir=snap)
    a, b = uuid4(), uuid4()
    v1.put(a, "CCO")
    path = v1.save_snapshot()
    assert path.exists()
    v1.put(b, "CCN")
    v3 = Vault(store, kp, t, snapshot_dir=snap)
    assert v3.index.positions >= 2
    assert set(v3.substructure("[CX4]", kind="smarts")) == {a, b}


def test_wrong_kek_cannot_open_an_existing_tenant(env, tmp_path):
    store, kp, snap = env
    t = uuid4()
    Vault(store, kp, t).put(uuid4(), "CCO")
    other = tmp_path / "other.key"
    keys.generate_key_file(other)
    with pytest.raises(crypto.SealError):
        Vault(store, keys.FileKeyProvider(other), t)


def test_no_snapshot_dir_means_save_fails_loudly(env):
    store, kp, _ = env
    v = Vault(store, kp, uuid4())
    with pytest.raises(Exception):
        v.save_snapshot()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_vault.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/molseal/vault.py`:
```python
"""The per-tenant facade. The only module that holds keys, store and engine together.

Consistency model (single process): a put or delete updates the index in the same call, so a
search right after sees it. Every search first reads the tenant's cursor; if it moved, the vault
pulls the new rows first. If it moved backwards, or the live counts disagree, the index is rebuilt.
"""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import UUID

from . import snapshot
from .crypto import open_sealed, pad, row_aad, seal, unpad
from .engine import QueryKind, StructureIndex, canonicalize
from .keys import KeyProvider, TenantKeys, exact_key_hmac, new_tenant_secret, unwrap_key, wrap_key
from .payload import Payload, decode, encode
from .store import StructureRecord, StructureStore, WrappedKey


class VaultError(Exception):
    pass


class NotFound(VaultError):
    pass


class Vault:
    def __init__(self, store: StructureStore, key_provider: KeyProvider, tenant_id: UUID,
                 snapshot_dir: Path | None = None, num_threads: int = -1, refresh_batch: int = 10000) -> None:
        self.store = store
        self.tenant_id = tenant_id
        self.snapshot_dir = None if snapshot_dir is None else Path(snapshot_dir)
        self.num_threads = num_threads
        self.refresh_batch = refresh_batch
        self._lock = threading.Lock()   # ponytail: one lock for reads and writes; RW lock when readers contend
        self._kek = key_provider.kek()
        self.tenant = store.ensure_tenant(tenant_id)
        self.keys = self._load_or_create_keys()
        self.index = self._load_snapshot() or StructureIndex()
        self.refresh()

    # ----- keys -----

    def _load_or_create_keys(self) -> TenantKeys:
        dv, hv = self.tenant.data_key_version, self.tenant.hmac_key_version
        wrapped = {(k.kind, k.version): k for k in self.store.get_tenant_keys(self.tenant_id)}
        if not wrapped:
            data, hm = new_tenant_secret(), new_tenant_secret()
            n, w = wrap_key(self._kek, self.tenant_id, "data", dv, data)
            self.store.put_tenant_key(self.tenant_id, WrappedKey("data", dv, n, w))
            n, w = wrap_key(self._kek, self.tenant_id, "hmac", hv, hm)
            self.store.put_tenant_key(self.tenant_id, WrappedKey("hmac", hv, n, w))
            return TenantKeys(data, dv, hm, hv)
        try:
            d, h = wrapped[("data", dv)], wrapped[("hmac", hv)]
        except KeyError as exc:
            raise VaultError("tenant key rows are missing for the current key versions") from exc
        return TenantKeys(
            unwrap_key(self._kek, self.tenant_id, "data", dv, d.nonce, d.wrapped), dv,
            unwrap_key(self._kek, self.tenant_id, "hmac", hv, h.nonce, h.wrapped), hv,
        )

    # ----- snapshot -----

    def _snapshot_path(self) -> Path | None:
        return None if self.snapshot_dir is None else self.snapshot_dir / f"{self.tenant_id}.molseal"

    def _load_snapshot(self) -> StructureIndex | None:
        p = self._snapshot_path()
        if p is None or not p.exists():
            return None
        return snapshot.load(p, self.keys.data_key, self.tenant_id, self.keys.data_key_version)

    def save_snapshot(self) -> Path:
        p = self._snapshot_path()
        if p is None:
            raise VaultError("no snapshot_dir configured")
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            snapshot.save(self.index, p, self.keys.data_key, self.tenant_id, self.keys.data_key_version)
        return p

    # ----- row crypto -----

    def _open(self, rec: StructureRecord) -> Payload:
        aad = row_aad(self.tenant_id, rec.id, rec.data_key_version, rec.row_version)
        return decode(unpad(open_sealed(self.keys.data_key, rec.nonce, rec.ciphertext, aad)))

    def _seal(self, id: UUID, row_version: int, payload: Payload) -> tuple[bytes, bytes]:
        aad = row_aad(self.tenant_id, id, self.keys.data_key_version, row_version)
        return seal(self.keys.data_key, pad(encode(payload)), aad)

    # ----- writes -----

    def put(self, id: UUID, smiles: str, exact_keys: dict[str, str] | None = None) -> int:
        canonical = canonicalize(smiles)          # raises StructureError before any write
        ek = dict(exact_keys or {})
        with self._lock:
            existing = self.store.get_structure(self.tenant_id, id)
            rv = 1 if existing is None else existing.row_version + 1
            nonce, ct = self._seal(id, rv, Payload(canonical, ek, False))
            rec = StructureRecord(self.tenant_id, id, rv, self.keys.data_key_version, nonce, ct, False)
            seq = self.store.put_structure(rec, {k: exact_key_hmac(self.keys.hmac_key, k, v) for k, v in ek.items()})
            self.index.add_batch([(id, canonical, rv)], self.num_threads)
            self._advance(seq)
        return seq

    def delete(self, id: UUID) -> int:
        with self._lock:
            existing = self.store.get_structure(self.tenant_id, id)
            if existing is None:
                raise NotFound(str(id))
            rv = existing.row_version + 1
            nonce, ct = self._seal(id, rv, Payload("", {}, True))
            rec = StructureRecord(self.tenant_id, id, rv, self.keys.data_key_version, nonce, ct, True)
            seq = self.store.put_structure(rec, {})
            self.index.mark_dead(id, rv)
            self._advance(seq)
        return seq

    def _advance(self, seq: int) -> None:
        """After our own write: if nothing else landed in between, remember seq; otherwise catch up."""
        if seq == self.index.last_seq + 1:
            self.index.last_seq = seq
        else:
            self._refresh_locked()

    # ----- reads -----

    def get(self, id: UUID) -> Payload | None:
        rec = self.store.get_structure(self.tenant_id, id)
        if rec is None:
            return None
        p = self._open(rec)
        return None if p.deleted else p

    def find_exact(self, kind: str, value: str) -> list[UUID]:
        h = exact_key_hmac(self.keys.hmac_key, kind, value)
        out = []
        for id_ in self.store.find_by_key(self.tenant_id, kind, h):
            p = self.get(id_)
            if p is not None and p.keys.get(kind) == value:   # the index row is a hint; the payload decides
                out.append(id_)
        return out

    def substructure(self, query: str, kind: QueryKind = "smiles", max_results: int | None = None) -> list[UUID]:
        with self._lock:
            self._sync_locked()
            return self.index.substructure(query, kind, max_results, self.num_threads)

    def count_substructure(self, query: str, kind: QueryKind = "smiles") -> int:
        with self._lock:
            self._sync_locked()
            return self.index.count_substructure(query, kind, self.num_threads)

    def similar(self, query_smiles: str, threshold: float = 0.7, top_k: int | None = None) -> list[tuple[UUID, float]]:
        with self._lock:
            self._sync_locked()
            return self.index.similar(query_smiles, threshold, top_k)

    def __len__(self) -> int:
        return len(self.index)

    # ----- catching up -----

    def refresh(self) -> int:
        with self._lock:
            return self._refresh_locked()

    def rebuild(self) -> None:
        with self._lock:
            self._rebuild_locked()

    def _sync_locked(self) -> None:
        cursor = self.store.seq_cursor(self.tenant_id)
        if cursor < self.index.last_seq:
            self._rebuild_locked()
        elif cursor > self.index.last_seq:
            self._refresh_locked()

    def _catch_up(self, index: StructureIndex) -> int:
        applied = 0
        while True:
            recs = self.store.changes_since(self.tenant_id, index.last_seq, self.refresh_batch)
            if not recs:
                return applied
            items = []
            for rec in recs:
                p = self._open(rec)
                if p.deleted:
                    index.mark_dead(rec.id, rec.row_version)
                else:
                    items.append((rec.id, p.smiles, rec.row_version))
            index.add_batch(items, self.num_threads)
            index.last_seq = recs[-1].seq
            applied += len(recs)

    def _refresh_locked(self) -> int:
        applied = self._catch_up(self.index)
        if self.store.live_count(self.tenant_id) != len(self.index):
            self._rebuild_locked()
        return applied

    def _rebuild_locked(self) -> None:
        fresh = StructureIndex()          # built aside, then swapped in
        self._catch_up(fresh)
        self.index = fresh
```

- [ ] **Step 4: Run the vault tests and the whole suite**

Run: `uv run pytest tests/test_vault.py -q` then `uv run pytest -q`
Expected: all pass (Postgres tests skip or pass depending on the env var).

- [ ] **Step 5: Commit**

```bash
git add -- src/molseal/vault.py tests/test_vault.py
git commit -m "feat(vault): per-tenant facade with read-your-writes, verified exact match, rebuild on regression

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/vault.py tests/test_vault.py
```

---

### Task 11: CLI: keygen, load, search, count, similar

**Files:**
- Create: `src/molseal/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `main(argv: list[str] | None = None) -> int` and the console script `molseal`.
- Commands: `keygen PATH`; `load FILE` (one SMILES per line, first whitespace token; writes `FILE.ids.tsv` sidecar with `id<TAB>canonical_smiles<TAB>inchikey`); `search QUERY [--smarts] [--max N]`; `count QUERY [--smarts]`; `similar SMILES [--threshold T] [--top K]`.
- Common options, each with an env fallback: `--db` (`MOLSEAL_DATABASE_URL`), `--key` (`MOLSEAL_KEY_FILE`), `--tenant` (`MOLSEAL_TENANT`), `--snapshot-dir` (`MOLSEAL_SNAPSHOT_DIR`).
- The CLI is a *caller*: it decides that the example exact key is the InChIKey. The library never does.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:
```python
import uuid

import pytest

from molseal import cli
from tests.mols import molecules


@pytest.fixture
def ctx(tmp_path):
    key = tmp_path / "kek.key"
    assert cli.main(["keygen", str(key)]) == 0
    db = f"sqlite:///{tmp_path / 'cli.db'}"
    tenant = str(uuid.uuid4())
    src = tmp_path / "mols.smi"
    src.write_text("\n".join(molecules()[:30] + ["garbage(", "# comment"]) + "\n")
    common = ["--db", db, "--key", str(key), "--tenant", tenant, "--snapshot-dir", str(tmp_path / "snap")]
    return common, src


def test_keygen_refuses_overwrite(tmp_path):
    key = tmp_path / "k"
    assert cli.main(["keygen", str(key)]) == 0
    assert cli.main(["keygen", str(key)]) != 0


def test_load_search_count_similar(ctx, capsys):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    out = capsys.readouterr().out
    assert "loaded 30" in out and "skipped 1" in out
    sidecar = src.with_name(src.name + ".ids.tsv")
    lines = sidecar.read_text().splitlines()
    assert len(lines) == 30 and len(lines[0].split("\t")) == 3

    assert cli.main(["count", "c1ccccc1", *common]) == 0
    n = int(capsys.readouterr().out.strip())
    assert n > 0

    assert cli.main(["search", "c1ccccc1", "--max", "3", *common]) == 0
    ids = capsys.readouterr().out.split()
    assert len(ids) == 3 and all(uuid.UUID(i) for i in ids)

    assert cli.main(["search", "[r5]", "--smarts", *common]) == 0
    assert cli.main(["similar", lines[0].split("\t")[1], "--threshold", "0.99", *common]) == 0
    row = capsys.readouterr().out.strip().splitlines()[0].split("\t")
    assert row[0] == lines[0].split("\t")[0] and float(row[1]) > 0.99


def test_missing_connection_options_fail_cleanly(monkeypatch, capsys):
    for var in ("MOLSEAL_DATABASE_URL", "MOLSEAL_KEY_FILE", "MOLSEAL_TENANT"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["count", "CCO"]) == 2
    assert "--db" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

`src/molseal/cli.py`:
```python
"""Command line for the proof of concept.

The CLI acts as a caller of the library: it is the CLI that decides the example exact key is
the InChIKey. Nothing under molseal/ except this file computes one.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

import sqlalchemy as sa
from rdkit import Chem, RDLogger

from .keys import FileKeyProvider, generate_key_file
from .store import SqlStore
from .vault import Vault

RDLogger.DisableLog("rdApp.*")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=os.environ.get("MOLSEAL_DATABASE_URL", ""), help="SQLAlchemy URL [env MOLSEAL_DATABASE_URL]")
    p.add_argument("--key", default=os.environ.get("MOLSEAL_KEY_FILE", ""), help="KEK file [env MOLSEAL_KEY_FILE]")
    p.add_argument("--tenant", default=os.environ.get("MOLSEAL_TENANT", ""), help="tenant UUID [env MOLSEAL_TENANT]")
    p.add_argument("--snapshot-dir", default=os.environ.get("MOLSEAL_SNAPSHOT_DIR"), help="[env MOLSEAL_SNAPSHOT_DIR]")


def _require(args: argparse.Namespace) -> None:
    missing = [f"--{n}" for n in ("db", "key", "tenant") if not getattr(args, n)]
    if missing:
        raise SystemExit(f"required: {' '.join(missing)} (or their MOLSEAL_* environment variables)")


def _store(args: argparse.Namespace) -> tuple[SqlStore, uuid.UUID]:
    _require(args)
    store = SqlStore(sa.create_engine(args.db))
    store.create_tables()
    return store, uuid.UUID(args.tenant)


def _vault(args: argparse.Namespace) -> Vault:
    store, tenant = _store(args)
    snap = Path(args.snapshot_dir) if args.snapshot_dir else None
    return Vault(store, FileKeyProvider(Path(args.key)), tenant, snapshot_dir=snap)


def cmd_keygen(args: argparse.Namespace) -> int:
    try:
        generate_key_file(Path(args.path))
    except FileExistsError:
        print(f"refusing to overwrite {args.path}", file=sys.stderr)
        return 1
    print(f"wrote {args.path} (mode 600)")
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    vault = _vault(args)
    src = Path(args.file)
    sidecar = src.with_name(src.name + ".ids.tsv")
    loaded = skipped = 0
    with open(src) as fin, open(sidecar, "a") as fout:
        for line in fin:
            tok = line.split()
            if not tok or tok[0].startswith("#"):
                continue
            mol = Chem.MolFromSmiles(tok[0])
            if mol is None:
                skipped += 1
                continue
            canonical, inchikey = Chem.MolToSmiles(mol), Chem.MolToInchiKey(mol)
            id_ = uuid.uuid4()
            vault.put(id_, canonical, {"inchikey": inchikey})
            fout.write(f"{id_}\t{canonical}\t{inchikey}\n")
            loaded += 1
            if loaded % 1000 == 0:
                print(f"{loaded} loaded", file=sys.stderr)
    if args.snapshot_dir:
        vault.save_snapshot()
    print(f"loaded {loaded}, skipped {skipped}, sidecar {sidecar}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    vault = _vault(args)
    kind = "smarts" if args.smarts else "smiles"
    for id_ in vault.substructure(args.query, kind=kind, max_results=args.max):
        print(id_)
    return 0


def cmd_count(args: argparse.Namespace) -> int:
    vault = _vault(args)
    print(vault.count_substructure(args.query, kind="smarts" if args.smarts else "smiles"))
    return 0


def cmd_similar(args: argparse.Namespace) -> int:
    vault = _vault(args)
    for id_, score in vault.similar(args.smiles, threshold=args.threshold, top_k=args.top):
        print(f"{id_}\t{score:.4f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="molseal")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="write a new 32-byte key file with mode 600")
    k.add_argument("path")
    k.set_defaults(fn=cmd_keygen)

    l = sub.add_parser("load", help="load a SMILES file (one per line); writes FILE.ids.tsv")
    l.add_argument("file")
    _add_common(l)
    l.set_defaults(fn=cmd_load)

    s = sub.add_parser("search", help="substructure search; prints ids")
    s.add_argument("query")
    s.add_argument("--smarts", action="store_true")
    s.add_argument("--max", type=int, default=None)
    _add_common(s)
    s.set_defaults(fn=cmd_search)

    c = sub.add_parser("count", help="substructure count")
    c.add_argument("query")
    c.add_argument("--smarts", action="store_true")
    _add_common(c)
    c.set_defaults(fn=cmd_count)

    m = sub.add_parser("similar", help="Tanimoto similarity; prints id<TAB>score")
    m.add_argument("smiles")
    m.add_argument("--threshold", type=float, default=0.7)
    m.add_argument("--top", type=int, default=None)
    _add_common(m)
    m.set_defaults(fn=cmd_similar)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except SystemExit as exc:          # _require() raises with a message; argparse raises with an int
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_cli.py -q`
Expected: all pass. Also try the console script once: `uv run molseal --help`.

- [ ] **Step 5: Commit**

```bash
git add -- src/molseal/cli.py tests/test_cli.py
git commit -m "feat(cli): keygen, load, search, count, similar

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/cli.py tests/test_cli.py
```

---

### Task 12: absence check with negative control, and leak report

**Files:**
- Modify: `src/molseal/cli.py` (two new commands)
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Produces commands:
  - `absence-check SIDECAR [--dump PATH] [--run-pg-dump] [--log PATH]`: exit 0 when no loaded SMILES or exact key appears in the raw column bytes, the plain dump, or the log, **and** the planted control row was found; exit 1 otherwise. Prints a JSON summary.
  - `leak-report SIDECAR`: prints JSON with `n`, `pearson_r` (size class index vs heavy-atom count), `predict_accuracy` (majority bucket per size class), `baseline_accuracy` (majority bucket overall), and a size-class histogram.
- Needles shorter than 8 bytes are skipped and counted, because short byte strings occur in random ciphertext by chance.

- [ ] **Step 1: Append the failing tests**

Append to `tests/test_cli.py`:
```python
def test_absence_check_passes_and_finds_its_control(ctx, capsys):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    capsys.readouterr()
    sidecar = src.with_name(src.name + ".ids.tsv")
    assert cli.main(["absence-check", str(sidecar), *common]) == 0
    out = capsys.readouterr().out
    assert '"control_found": true' in out and '"db_hits": 0' in out


def test_absence_check_catches_a_plaintext_log(ctx, capsys, tmp_path):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    capsys.readouterr()
    sidecar = src.with_name(src.name + ".ids.tsv")
    leaked = sidecar.read_text().splitlines()[0].split("\t")[2]      # an InChIKey
    log = tmp_path / "postgres.log"
    log.write_text(f"LOG: statement: INSERT ... '{leaked}'\n")
    assert cli.main(["absence-check", str(sidecar), "--log", str(log), *common]) == 1
    assert '"log_hits": 1' in capsys.readouterr().out


def test_leak_report_prints_numbers(ctx, capsys):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    capsys.readouterr()
    sidecar = src.with_name(src.name + ".ids.tsv")
    assert cli.main(["leak-report", str(sidecar), *common]) == 0
    out = capsys.readouterr().out
    assert '"pearson_r"' in out and '"predict_accuracy"' in out and '"n": 30' in out
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `uv run pytest tests/test_cli.py -q -k "absence or leak"`
Expected: FAIL (argparse error: invalid choice).

- [ ] **Step 3: Add the two commands**

Add to `src/molseal/cli.py` (imports at top, functions before `build_parser`, and the two subparsers inside it):
```python
import json
import math
import shutil
import subprocess
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import delete, func, insert, select

from .store import structure_keys, structures, tenant_keys

MIN_NEEDLE = 8
CONTROL_ID = uuid.UUID("00000000-0000-0000-0000-0000000c0de0")


def _read_sidecar(path: Path) -> list[tuple[uuid.UUID, str, str]]:
    rows = []
    for line in Path(path).read_text().splitlines():
        id_, smiles, key = line.split("\t")
        rows.append((uuid.UUID(id_), smiles, key))
    return rows


def _column_bytes(store: SqlStore, tenant: uuid.UUID) -> bytes:
    with store.engine.connect() as conn:
        parts = [r.nonce + r.ciphertext for r in conn.execute(
            select(structures.c.nonce, structures.c.ciphertext).where(structures.c.tenant_id == tenant))]
        parts += [r.nonce + r.wrapped for r in conn.execute(
            select(tenant_keys.c.nonce, tenant_keys.c.wrapped).where(tenant_keys.c.tenant_id == tenant))]
        parts += [r.hmac for r in conn.execute(
            select(structure_keys.c.hmac).where(structure_keys.c.tenant_id == tenant))]
    return b"\x00".join(parts)


def _hits(blob: bytes, needles: list[bytes]) -> int:
    return sum(1 for n in needles if n in blob)


def cmd_absence_check(args: argparse.Namespace) -> int:
    store, tenant = _store(args)
    rows = _read_sidecar(Path(args.sidecar))
    raw = {s for _, s, _ in rows} | {k for _, _, k in rows}
    needles = [n.encode() for n in raw if len(n) >= MIN_NEEDLE]
    skipped_short = len(raw) - len(needles)

    control = b"MOLSEAL-CONTROL-PLAINTEXT-" + os.urandom(6).hex().encode()
    with store.engine.begin() as conn:      # raw insert: no cursor bump, seq 0 is never read by a vault
        conn.execute(insert(structures).values(tenant_id=tenant, id=CONTROL_ID, row_version=1, data_key_version=1,
                                               nonce=b"\x00" * 12, ciphertext=control, deleted=True, seq=0,
                                               updated_at=datetime.now(timezone.utc)))
    try:
        blob = _column_bytes(store, tenant)
        db_hits = _hits(blob, needles)
        control_found = control in blob
        dump_hits = None
        if args.run_pg_dump:
            if shutil.which("pg_dump") is None:
                print("pg_dump not found; skipping dump scan", file=sys.stderr)
            else:
                url = args.db.replace("postgresql+psycopg://", "postgresql://")
                res = subprocess.run(["pg_dump", "-Fp", "--compress=0", "--dbname", url], capture_output=True, check=True)
                dump_hits = _hits(res.stdout, needles)
                control_found = control_found and control in res.stdout
        elif args.dump:
            dump = Path(args.dump).read_bytes()
            dump_hits = _hits(dump, needles)
    finally:
        with store.engine.begin() as conn:
            conn.execute(delete(structures).where(structures.c.tenant_id == tenant, structures.c.id == CONTROL_ID))
    log_hits = _hits(Path(args.log).read_bytes(), needles) if args.log else None

    ok = db_hits == 0 and not dump_hits and not log_hits and control_found
    print(json.dumps({"needles": len(needles), "skipped_short": skipped_short, "db_hits": db_hits,
                      "dump_hits": dump_hits, "log_hits": log_hits, "control_found": control_found, "ok": ok}, indent=1))
    return 0 if ok else 1


def cmd_leak_report(args: argparse.Namespace) -> int:
    store, tenant = _store(args)
    rows = _read_sidecar(Path(args.sidecar))
    heavy = {id_: Chem.MolFromSmiles(s).GetNumHeavyAtoms() for id_, s, _ in rows}
    with store.engine.connect() as conn:
        sizes = {r.id: r.n for r in conn.execute(
            select(structures.c.id, func.length(structures.c.ciphertext).label("n"))
            .where(structures.c.tenant_id == tenant, structures.c.deleted == False))}  # noqa: E712
    ids = [i for i in heavy if i in sizes]
    cls = np.array([int(math.log2(sizes[i] - 16)) for i in ids])     # ciphertext = size class + 16-byte tag
    atoms = np.array([heavy[i] for i in ids])
    bucket = atoms // 10
    r = float(np.corrcoef(cls, atoms)[0, 1]) if len(ids) > 1 and cls.std() > 0 else 0.0
    predicted = np.empty_like(bucket)
    for c in np.unique(cls):
        mask = cls == c
        predicted[mask] = np.bincount(bucket[mask]).argmax()
    report = {
        "n": len(ids), "pearson_r": round(r, 4),
        "predict_accuracy": round(float((predicted == bucket).mean()), 4),
        "baseline_accuracy": round(float(np.bincount(bucket).max() / len(bucket)), 4),
        "size_classes": {str(2 ** int(c)): int((cls == c).sum()) for c in np.unique(cls)},
    }
    print(json.dumps(report, indent=1))
    return 0
```

Inside `build_parser()`, before `return p`:
```python
    a = sub.add_parser("absence-check", help="prove no loaded structure appears in the DB, a plain dump, or a log")
    a.add_argument("sidecar")
    a.add_argument("--dump", help="path to a `pg_dump -Fp --compress=0` output to scan")
    a.add_argument("--run-pg-dump", action="store_true", help="run pg_dump against --db and scan its output")
    a.add_argument("--log", help="PostgreSQL log captured with log_statement=all")
    _add_common(a)
    a.set_defaults(fn=cmd_absence_check)

    r = sub.add_parser("leak-report", help="how much ciphertext size class says about molecule size")
    r.add_argument("sidecar")
    _add_common(r)
    r.set_defaults(fn=cmd_leak_report)
```

- [ ] **Step 4: Run the CLI suite and the whole suite**

Run: `uv run pytest tests/test_cli.py -q` then `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Try it against Postgres once**

With `docker compose up -d db` running and a key file:
```bash
set -x MOLSEAL_DATABASE_URL postgresql+psycopg://molseal:molseal@localhost:5439/molseal
set -x MOLSEAL_KEY_FILE /tmp/molseal-dev.key
set -x MOLSEAL_TENANT (uuidgen | tr A-F a-f)
uv run molseal keygen $MOLSEAL_KEY_FILE
uv run python -c "import tests.mols as m; print('\n'.join(m.molecules()))" > /tmp/dev.smi
uv run molseal load /tmp/dev.smi
uv run molseal absence-check /tmp/dev.smi.ids.tsv --run-pg-dump
uv run molseal leak-report /tmp/dev.smi.ids.tsv
```
Expected: `"ok": true` with `"control_found": true`, and a leak report. Paste both JSON blocks into the commit body.

- [ ] **Step 6: Commit**

```bash
git add -- src/molseal/cli.py tests/test_cli.py
git commit -m "feat(cli): absence check with planted control row, leak report

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/cli.py tests/test_cli.py
```

---

### Task 13: README, full run, milestone tag

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Write the README**

Replace `README.md` with: a one-paragraph description that says plainly it is encrypted blob storage plus a plaintext in-memory index in a trusted process, not encrypted search; the threat model from spec section 2 (attacker, trusted parties, guaranteed, leaks accepted, not detectable); the key-file rule (outside the database host, volumes and backups); the AES-GCM nonce budget note (about 2^32 encryptions per data key; versions exist for rotation, rotation not implemented); a "Quick start" with the five commands from Task 12 step 5; a "What is deliberately not built" list copied from spec section 13; "License: BSD-3-Clause"; a "Data attribution" line reserved for ChEMBL (CC BY-SA 3.0) that the benchmark plan fills in.

- [ ] **Step 2: Full suite, both ways**

Run: `uv run pytest -q` with `MOLSEAL_TEST_DATABASE_URL` set, then `env -u MOLSEAL_TEST_DATABASE_URL uv run pytest -q`.
Expected: all pass; the Postgres tests skip in the second run.

- [ ] **Step 3: Commit and tag**

```bash
git add -- README.md
git commit -m "docs: README with threat model, quick start and non-goals

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- README.md
git tag -a milestone-1 -m "molseal milestone 1: library, tests, absence check"
```

---

## After this plan

Milestone 2 (spec section 8) gets its own plan: ChEMBL 37 loader with a process pool and a bulk `put_many`, the cartridge container and its schema, the query-set generator, the parity gates, the timing runner, and the in-VM bench container. Bulk and asynchronous ingestion for consumers (requested 2026-10-13) is designed there too, since the loader is the first bulk path.
