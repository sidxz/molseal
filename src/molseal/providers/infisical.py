"""Infisical KMS: wrap and unwrap through the encrypt and decrypt endpoints of a KMS key.

    POST {base_url}/api/v1/kms/keys/{key_id}/encrypt   {"plaintext": <base64>}  -> {"ciphertext": <base64>}
    POST {base_url}/api/v1/kms/keys/{key_id}/decrypt   {"ciphertext": <base64>} -> {"plaintext": <base64>}

(API reference: operations encryptWithKmsKey and decryptWithKmsKey.) No SDK and no extra: the
standard library does the two calls. The service takes no associated data; `molseal.keys` binds the
label inside the plaintext. `token` is a bearer token for a machine identity that may use the key;
obtaining it is the caller's job."""

from __future__ import annotations

import base64
import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from ..keys import KeyProviderError


@dataclass(frozen=True)
class InfisicalKmsKeyProvider:
    base_url: str               # https://eu.infisical.com, https://us.infisical.com, or a self-hosted origin
    key_id: str                 # KMS key UUID (an encrypt/decrypt key)
    token: str                  # bearer token
    post: Callable[[str, dict], dict] | None = None   # (url, json body) -> json response; tests inject

    def _post(self, url: str, body: dict) -> dict:
        if self.post is not None:
            return self.post(url, body)
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def _url(self, op: str) -> str:
        return f"{self.base_url.rstrip('/')}/api/v1/kms/keys/{self.key_id}/{op}"

    def wrap(self, label: bytes, plaintext: bytes) -> bytes:
        try:
            r = self._post(self._url("encrypt"), {"plaintext": base64.b64encode(plaintext).decode("ascii")})
            return base64.b64decode(r["ciphertext"], validate=True)
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"Infisical KMS encrypt failed: {exc}") from exc

    def unwrap(self, label: bytes, blob: bytes) -> bytes:
        try:
            r = self._post(self._url("decrypt"), {"ciphertext": base64.b64encode(blob).decode("ascii")})
            return base64.b64decode(r["plaintext"], validate=True)
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"Infisical KMS decrypt failed: {exc}") from exc
