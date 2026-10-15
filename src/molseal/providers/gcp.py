"""Google Cloud KMS: wrap and unwrap under a symmetric key the process never holds.

The label travels as additional authenticated data. Cloud KMS ciphertext records the key version it
was made with, so rotation on the service side needs nothing from the library.
Extra: `pip install molseal[gcp]` (google-cloud-kms)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..keys import KeyProviderError


@dataclass(frozen=True)
class GcpKmsKeyProvider:
    key_name: str               # projects/P/locations/L/keyRings/R/cryptoKeys/K
    client: Any = None          # google.cloud.kms.KeyManagementServiceClient()

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            from google.cloud import kms
        except ImportError as exc:
            raise KeyProviderError("Google Cloud KMS provider needs google-cloud-kms: pip install molseal[gcp]") from exc
        return kms.KeyManagementServiceClient()

    def wrap(self, label: bytes, plaintext: bytes) -> bytes:
        try:
            r = self._client().encrypt(request={"name": self.key_name, "plaintext": plaintext,
                                                "additional_authenticated_data": label})
            return bytes(r.ciphertext)
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"Google Cloud KMS encrypt failed: {exc}") from exc

    def unwrap(self, label: bytes, blob: bytes) -> bytes:
        try:
            r = self._client().decrypt(request={"name": self.key_name, "ciphertext": blob,
                                                "additional_authenticated_data": label})
            return bytes(r.plaintext)
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"Google Cloud KMS decrypt failed: {exc}") from exc
