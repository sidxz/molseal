"""HashiCorp Vault Transit: wrap and unwrap through the encrypt and decrypt endpoints of a Transit key.

    POST {base_url}/v1/{mount}/encrypt/{key_name}   {"plaintext": <base64>}        -> {"data": {"ciphertext": "vault:v1:..."}}
    POST {base_url}/v1/{mount}/decrypt/{key_name}   {"ciphertext": "vault:v1:..."}  -> {"data": {"plaintext": <base64>}}

No SDK and no extra: the standard library does the two calls, with the token in `X-Vault-Token` and,
when one is configured, the namespace in `X-Vault-Namespace` (Vault Enterprise and HCP Vault). The
ciphertext string names the key version it was made with, so rotating the Transit key needs nothing
from the library. The engine takes no associated data here; `molseal.keys` binds the label inside the
plaintext. `token` needs `update` on both paths; obtaining and renewing it is the caller's job."""

from __future__ import annotations

import base64
import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..keys import KeyProviderError

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class VaultTransitKeyProvider:
    base_url: str                    # https://vault.example.com:8200
    key_name: str                    # the Transit key's name
    token: str = field(repr=False)   # sent as X-Vault-Token
    mount: str = "transit"           # where the Transit engine is mounted
    namespace: str | None = None     # sent as X-Vault-Namespace; Enterprise and HCP Vault only
    post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None   # (url, json body) -> json response; tests inject

    def __post_init__(self) -> None:
        u = urlsplit(self.base_url)
        if u.scheme != "https" and u.hostname not in LOOPBACK_HOSTS:
            raise KeyProviderError("Vault base_url must use https (the token unwraps every tenant key); "
                                   "http is allowed only for a loopback host")

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.post is not None:
            return self.post(url, body)
        headers = {"Content-Type": "application/json", "X-Vault-Token": self.token}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc: dict[str, Any] = json.load(resp)
        return doc

    def _url(self, op: str) -> str:
        return f"{self.base_url.rstrip('/')}/v1/{self.mount.strip('/')}/{op}/{self.key_name}"

    def wrap(self, label: bytes, plaintext: bytes) -> bytes:
        try:
            r = self._post(self._url("encrypt"), {"plaintext": base64.b64encode(plaintext).decode("ascii")})
            ciphertext = r["data"]["ciphertext"]
            if not isinstance(ciphertext, str) or not ciphertext.startswith("vault:v"):
                raise TypeError(f"unexpected ciphertext {ciphertext!r}")
            return ciphertext.encode("ascii")           # stored as Transit returned it, version prefix included
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"Vault Transit encrypt failed: {exc}") from exc

    def unwrap(self, label: bytes, blob: bytes) -> bytes:
        try:
            r = self._post(self._url("decrypt"), {"ciphertext": blob.decode("ascii")})
            return base64.b64decode(r["data"]["plaintext"], validate=True)
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"Vault Transit decrypt failed: {exc}") from exc
