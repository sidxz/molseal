"""One contract for every key provider, run against in-process fakes of the four services.

The fakes are realistic where it matters: AES-GCM with the label as associated data where the real
service has an encryption context (AWS, GCP), a real RSA-OAEP key with a versioned key id for Azure,
base64 over the two Infisical URLs. No vendor SDK is installed or imported."""

import base64
import builtins
import os
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from molseal import keys
from molseal.providers.aws import AwsKmsKeyProvider
from molseal.providers.azure import ALGORITHM, AzureKeyVaultKeyProvider
from molseal.providers.gcp import GcpKmsKeyProvider
from molseal.providers.infisical import InfisicalKmsKeyProvider


class ServiceError(Exception):
    """What a vendor SDK raises; must surface as KeyProviderError with this as __cause__."""


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _open(key: bytes, blob: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], aad)
    except Exception as exc:
        raise ServiceError("decrypt failed") from exc


def _ctx(context: dict[str, str]) -> bytes:     # KMS authenticates the encryption context
    return "&".join(f"{k}={v}" for k, v in sorted(context.items())).encode()


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeAwsKms:
    def __init__(self, key_id="alias/molseal"):
        self.key_id, self.key, self.down = key_id, os.urandom(32), False

    def _check(self, key_id):
        if self.down:
            raise ServiceError("service unavailable")
        if key_id != self.key_id:
            raise ServiceError("NotFoundException")

    def encrypt(self, KeyId, Plaintext, EncryptionContext):
        self._check(KeyId)
        return {"CiphertextBlob": _seal(self.key, Plaintext, _ctx(EncryptionContext))}

    def decrypt(self, CiphertextBlob, KeyId, EncryptionContext):
        self._check(KeyId)
        return {"Plaintext": _open(self.key, CiphertextBlob, _ctx(EncryptionContext))}


class FakeGcpKms:
    def __init__(self, key_name="projects/p/locations/l/keyRings/r/cryptoKeys/k"):
        self.key_name, self.key, self.down = key_name, os.urandom(32), False

    def _check(self, name):
        if self.down:
            raise ServiceError("service unavailable")
        if name != self.key_name:
            raise ServiceError("NotFound")

    def encrypt(self, request):
        self._check(request["name"])
        return _Obj(ciphertext=_seal(self.key, request["plaintext"], request["additional_authenticated_data"]))

    def decrypt(self, request):
        self._check(request["name"])
        return _Obj(plaintext=_open(self.key, request["ciphertext"], request["additional_authenticated_data"]))


_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


class FakeAzureVault:
    """One RSA key with one version. `client(key_id)` hands out a client bound to that id; wrapKey
    answers with the versioned id, unwrapKey fails for any id but the versioned one, as Key Vault does."""

    UNVERSIONED = "https://v.vault.azure.net/keys/molseal"
    VERSIONED = UNVERSIONED + "/0123456789abcdef0123456789abcdef"

    def __init__(self):
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.down = False
        self.asked: list[str] = []

    def _check(self, algorithm, key_id, allowed):
        if self.down:
            raise ServiceError("service unavailable")
        if algorithm != "RSA-OAEP-256":
            raise ServiceError(f"unsupported algorithm {algorithm}")
        if key_id not in allowed:
            raise ServiceError(f"key {key_id} not found")

    def client(self, key_id):
        self.asked.append(key_id)
        vault = self

        class Client:
            def wrap_key(self, algorithm, key):
                vault._check(algorithm, key_id, (vault.UNVERSIONED, vault.VERSIONED))
                return _Obj(key_id=vault.VERSIONED, algorithm=algorithm,
                            encrypted_key=vault.private.public_key().encrypt(key, _OAEP))

            def unwrap_key(self, algorithm, encrypted_key):
                vault._check(algorithm, key_id, (vault.VERSIONED,))
                try:
                    return _Obj(key_id=vault.VERSIONED, algorithm=algorithm,
                                key=vault.private.decrypt(encrypted_key, _OAEP))
                except Exception as exc:
                    raise ServiceError("unwrap failed") from exc

        return Client()


class FakeInfisical:
    def __init__(self, base_url="https://eu.infisical.com", key_id="4f1c2d3e-0000-4000-8000-000000000001"):
        self.base_url, self.key_id, self.key, self.down = base_url, key_id, os.urandom(32), False
        self.urls: list[str] = []
        self.reply: dict | None = None            # when set, returned verbatim

    def post(self, url, body):
        self.urls.append(url)
        if self.down:
            raise ServiceError("503")
        if self.reply is not None:
            return self.reply
        prefix = f"{self.base_url}/api/v1/kms/keys/{self.key_id}/"
        if url == prefix + "encrypt":
            return {"ciphertext": base64.b64encode(_seal(self.key, base64.b64decode(body["plaintext"]), b"")).decode()}
        if url == prefix + "decrypt":
            return {"plaintext": base64.b64encode(_open(self.key, base64.b64decode(body["ciphertext"]), b"")).decode()}
        raise ServiceError(f"404 {url}")


NAMES = ["raw", "file", "aws", "gcp", "azure", "infisical"]


def _make(name, tmp_path):
    if name == "raw":
        return keys.RawKeyProvider(os.urandom(32)), None
    if name == "file":
        p = tmp_path / "kek.key"
        keys.generate_key_file(p)
        return keys.FileKeyProvider(p), None
    if name == "aws":
        f = FakeAwsKms()
        return AwsKmsKeyProvider(f.key_id, client=f), f
    if name == "gcp":
        f = FakeGcpKms()
        return GcpKmsKeyProvider(f.key_name, client=f), f
    if name == "azure":
        f = FakeAzureVault()
        return AzureKeyVaultKeyProvider(f.UNVERSIONED, client_factory=f.client), f
    if name == "infisical":
        f = FakeInfisical()
        return InfisicalKmsKeyProvider(f.base_url, f.key_id, token="t", post=f.post), f
    raise AssertionError(name)


@pytest.fixture(params=NAMES)
def provider(request, tmp_path):
    return _make(request.param, tmp_path)


def test_round_trip_and_fresh_randomness(provider):
    p, _ = provider
    t, secret = uuid4(), keys.new_tenant_secret()
    blob = keys.wrap_tenant_key(p, t, "data", 1, secret)
    assert keys.unwrap_tenant_key(p, t, "data", 1, blob) == secret
    assert keys.wrap_tenant_key(p, t, "data", 1, secret) != blob


def test_wrong_tenant_kind_or_version_fails(provider):
    p, _ = provider
    t, secret = uuid4(), keys.new_tenant_secret()
    blob = keys.wrap_tenant_key(p, t, "data", 1, secret)
    for args in ((uuid4(), "data", 1), (t, "hmac", 1), (t, "data", 2)):
        with pytest.raises(keys.KeyProviderError):
            keys.unwrap_tenant_key(p, *args, blob)


def test_any_flipped_byte_fails(provider):
    p, _ = provider
    t, secret = uuid4(), keys.new_tenant_secret()
    blob = keys.wrap_tenant_key(p, t, "data", 1, secret)
    for i in range(len(blob)):
        flipped = blob[:i] + bytes([blob[i] ^ 0x01]) + blob[i + 1:]
        with pytest.raises(keys.KeyProviderError):
            keys.unwrap_tenant_key(p, t, "data", 1, flipped)


def test_garbage_blob_fails(provider):
    p, _ = provider
    t = uuid4()
    for garbage in (b"", b"\x00", b"\xff\xff", os.urandom(5), os.urandom(40), os.urandom(300)):
        with pytest.raises(keys.KeyProviderError):
            keys.unwrap_tenant_key(p, t, "data", 1, garbage)


@pytest.mark.parametrize("name", ["aws", "gcp", "azure", "infisical"])
def test_service_failure_surfaces_as_provider_error_with_cause(name, tmp_path):
    p, fake = _make(name, tmp_path)
    t, secret = uuid4(), keys.new_tenant_secret()
    blob = keys.wrap_tenant_key(p, t, "data", 1, secret)
    fake.down = True
    with pytest.raises(keys.KeyProviderError) as info:
        keys.wrap_tenant_key(p, t, "data", 1, secret)
    assert isinstance(info.value.__cause__, ServiceError)
    with pytest.raises(keys.KeyProviderError) as info:
        keys.unwrap_tenant_key(p, t, "data", 1, blob)
    assert isinstance(info.value.__cause__, ServiceError)


def test_aws_and_gcp_bind_the_label_inside_the_service(tmp_path):
    """Calling the provider directly bypasses the library check; the service's own associated data
    must still reject a wrong label."""
    for name in ("aws", "gcp"):
        p, _ = _make(name, tmp_path)
        blob = p.wrap(b"label-1", b"x" * 59)
        assert p.unwrap(b"label-1", blob) == b"x" * 59
        with pytest.raises(keys.KeyProviderError):
            p.unwrap(b"label-2", blob)


def test_azure_unwrap_uses_the_versioned_key_id_from_the_blob(tmp_path):
    p, fake = _make("azure", tmp_path)
    t, secret = uuid4(), keys.new_tenant_secret()
    blob = keys.wrap_tenant_key(p, t, "data", 1, secret)
    assert fake.asked == [fake.UNVERSIONED]
    assert fake.VERSIONED.encode() in blob
    assert keys.unwrap_tenant_key(p, t, "data", 1, blob) == secret
    assert fake.asked == [fake.UNVERSIONED, fake.VERSIONED]


def test_infisical_posts_to_the_encrypt_and_decrypt_endpoints(tmp_path):
    p, fake = _make("infisical", tmp_path)
    t, secret = uuid4(), keys.new_tenant_secret()
    blob = keys.wrap_tenant_key(p, t, "data", 1, secret)
    assert keys.unwrap_tenant_key(p, t, "data", 1, blob) == secret
    assert fake.urls == [f"{fake.base_url}/api/v1/kms/keys/{fake.key_id}/encrypt",
                         f"{fake.base_url}/api/v1/kms/keys/{fake.key_id}/decrypt"]


def test_infisical_bad_response_is_a_provider_error(tmp_path):
    p, fake = _make("infisical", tmp_path)
    for reply in ({}, {"ciphertext": "not base64!"}, {"ciphertext": None}):
        fake.reply = reply
        with pytest.raises(keys.KeyProviderError) as info:
            p.wrap(b"l", b"p")
        assert info.value.__cause__ is not None


def test_missing_sdk_is_reported_with_the_extra_to_install(monkeypatch):
    real = builtins.__import__

    def no_sdk(name, *a, **k):
        if name.split(".")[0] in ("boto3", "google", "azure"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    cases = ((AwsKmsKeyProvider("alias/k"), "aws"), (GcpKmsKeyProvider("projects/p/k"), "gcp"),
             (AzureKeyVaultKeyProvider("https://v.vault.azure.net/keys/k"), "azure"))
    for p, extra in cases:
        with pytest.raises(keys.KeyProviderError, match=rf"molseal\[{extra}\]"):
            p.wrap(b"l", b"p")
        with pytest.raises(keys.KeyProviderError, match=rf"molseal\[{extra}\]"):
            p.unwrap(b"l", b"\x00\x05abcde" + b"x" * 20)
