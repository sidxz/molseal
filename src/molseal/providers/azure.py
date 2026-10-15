"""Azure Key Vault (and Managed HSM): wrap and unwrap with RSA-OAEP-256 under a non-exportable key.

The service takes no associated data; `molseal.keys` binds the label inside the plaintext. The wrap
result's key id is the versioned id, and it is stored in the blob so unwrap addresses that exact
version after the key has been rotated in Key Vault. The stored id must be a version of the
configured key (same host, same key name); anything else is rejected before any client is built.
The identity needs `get`, `wrapKey` and `unwrapKey` (or a versioned key id when `get` is not granted).
Extra: `pip install molseal[azure]` (azure-keyvault-keys, azure-identity)."""

from __future__ import annotations

import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..keys import KeyProviderError

ALGORITHM = "RSA-OAEP-256"      # KeyWrapAlgorithm.rsa_oaep_256; the SDK accepts the string

_VERSION = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class AzureKeyVaultKeyProvider:
    key_id: str                                          # https://<vault>.vault.azure.net/keys/<name>[/<version>]
    credential: Any = None                               # default azure.identity.DefaultAzureCredential()
    client_factory: Callable[[str], Any] | None = None   # key id -> CryptographyClient-like; tests inject

    def _client(self, key_id: str) -> tuple[Any, Any]:
        if self.client_factory is not None:
            return self.client_factory(key_id), ALGORITHM
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm
        except ImportError as exc:
            raise KeyProviderError("Azure Key Vault provider needs azure-keyvault-keys and azure-identity: "
                                   "pip install molseal[azure]") from exc
        return CryptographyClient(key_id, self.credential or DefaultAzureCredential()), KeyWrapAlgorithm.rsa_oaep_256

    def _key_id_ok(self, kid: str) -> bool:
        base, k = urlsplit(self.key_id), urlsplit(kid)
        parts = base.path.rstrip("/").split("/")          # ['', 'keys', name] or ['', 'keys', name, version]
        name = parts[2] if len(parts) >= 3 and parts[1] == "keys" else ""
        kparts = k.path.rstrip("/").split("/")
        return bool(k.scheme == "https" and k.netloc and k.netloc.lower() == base.netloc.lower()
                    and not k.query and not k.fragment
                    and name and len(kparts) == 4 and kparts[1] == "keys" and kparts[2].lower() == name.lower()
                    and _VERSION.match(kparts[3]) is not None)

    def wrap(self, label: bytes, plaintext: bytes) -> bytes:
        try:
            client, alg = self._client(self.key_id)
            r = client.wrap_key(alg, plaintext)
            kid = str(r.key_id)
            if not self._key_id_ok(kid):
                raise KeyProviderError(f"Key Vault returned key id {kid!r}, which is not a versioned id of the "
                                       "configured key; grant the identity keys/get or configure a versioned key id")
            kb = kid.encode("utf-8")
            return struct.pack(">H", len(kb)) + kb + bytes(r.encrypted_key)
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"Azure Key Vault wrapKey failed: {exc}") from exc

    def unwrap(self, label: bytes, blob: bytes) -> bytes:
        try:
            if len(blob) < 2:
                raise KeyProviderError("Azure wrapped key blob is too short")
            (n,) = struct.unpack(">H", blob[:2])
            if len(blob) < 2 + n:
                raise KeyProviderError("Azure wrapped key blob is truncated")
            kid, encrypted = blob[2:2 + n].decode("utf-8"), blob[2 + n:]
            if not self._key_id_ok(kid):
                raise KeyProviderError("Azure wrapped key blob names a key id outside the configured key")
            client, alg = self._client(kid)
            return bytes(client.unwrap_key(alg, encrypted).key)
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"Azure Key Vault unwrapKey failed: {exc}") from exc
