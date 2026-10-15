"""AWS KMS: wrap and unwrap under a symmetric encrypt/decrypt key the process never holds.

The label travels as the encryption context, so KMS enforces it and CloudTrail records it.
Extra: `pip install molseal[aws]` (boto3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..keys import KeyProviderError


@dataclass(frozen=True)
class AwsKmsKeyProvider:
    key_id: str                 # key id, ARN, or alias/NAME of a symmetric encrypt/decrypt key
    client: Any = None          # a boto3 KMS client; default boto3.client("kms")

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            import boto3
        except ImportError as exc:
            raise KeyProviderError("AWS KMS provider needs boto3: pip install molseal[aws]") from exc
        return boto3.client("kms")

    def wrap(self, label: bytes, plaintext: bytes) -> bytes:
        try:
            r = self._client().encrypt(KeyId=self.key_id, Plaintext=plaintext, EncryptionContext=_context(label))
            return bytes(r["CiphertextBlob"])
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"AWS KMS encrypt failed: {exc}") from exc

    def unwrap(self, label: bytes, blob: bytes) -> bytes:
        try:
            r = self._client().decrypt(CiphertextBlob=blob, KeyId=self.key_id, EncryptionContext=_context(label))
            return bytes(r["Plaintext"])
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError(f"AWS KMS decrypt failed: {exc}") from exc


def _context(label: bytes) -> dict[str, str]:
    return {"molseal_label": label.hex()}
