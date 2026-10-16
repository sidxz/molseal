"""One contract for every key provider, run against in-process fakes of the five services.

The fakes are realistic where it matters: AES-GCM with the label as associated data where the real
service has an encryption context (AWS, GCP), a real RSA-OAEP key with a versioned key id for Azure,
base64 over the two Infisical URLs, Vault's `vault:v1:` ciphertext strings over the two Transit paths.
No vendor SDK is installed or imported."""

import base64
import builtins
import io
import json
import os
import struct
import urllib.request
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from molseal import keys
from molseal.providers.aws import AwsKmsKeyProvider
from molseal.providers.azure import AzureKeyVaultKeyProvider
from molseal.providers.gcp import GcpKmsKeyProvider
from molseal.providers.infisical import InfisicalKmsKeyProvider
from molseal.providers.vault import VaultTransitKeyProvider


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
        self.echo_configured_id = False      # what the real client returns when keys/get is forbidden
        self.asked: list[str] = []

    def _check(self, algorithm, key_id, allowed):
        if self.down:
            raise ServiceError("service unavailable")
        if algorithm != "RSA-OAEP-256":
            raise ServiceError(f"unsupported algorithm {algorithm}")
        if key_id.lower() not in [a.lower() for a in allowed]:
            raise ServiceError(f"key {key_id} not found")

    def client(self, key_id):
        self.asked.append(key_id)
        vault = self

        class Client:
            def wrap_key(self, algorithm, key):
                vault._check(algorithm, key_id, (vault.UNVERSIONED, vault.VERSIONED))
                return _Obj(key_id=key_id if vault.echo_configured_id else vault.VERSIONED, algorithm=algorithm,
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


class FakeVaultTransit:
    """Transit's encrypt and decrypt under one mount. Ciphertext is the `vault:v1:<base64>` string the
    real engine returns; anything else on decrypt is a 400, as Vault answers."""

    def __init__(self, base_url="https://vault.example.com:8200", mount="transit", key_name="molseal"):
        self.base_url, self.mount, self.key_name = base_url, mount, key_name
        self.key, self.down = os.urandom(32), False
        self.urls: list[str] = []
        self.reply: dict | None = None            # when set, returned verbatim

    def post(self, url, body):
        self.urls.append(url)
        if self.down:
            raise ServiceError("503")
        if self.reply is not None:
            return self.reply
        prefix = f"{self.base_url}/v1/{self.mount}/"
        if url == prefix + f"encrypt/{self.key_name}":
            ct = _seal(self.key, base64.b64decode(body["plaintext"], validate=True), b"")
            return {"data": {"ciphertext": "vault:v1:" + base64.b64encode(ct).decode(), "key_version": 1}}
        if url == prefix + f"decrypt/{self.key_name}":
            s = body["ciphertext"]
            if not isinstance(s, str) or not s.startswith("vault:v1:"):
                raise ServiceError("400 invalid ciphertext")
            pt = _open(self.key, base64.b64decode(s[len("vault:v1:"):], validate=True), b"")
            return {"data": {"plaintext": base64.b64encode(pt).decode()}}
        raise ServiceError(f"404 {url}")


NAMES = ["raw", "file", "aws", "gcp", "azure", "infisical", "vault"]


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
    if name == "vault":
        f = FakeVaultTransit()
        return VaultTransitKeyProvider(f.base_url, f.key_name, token="t", mount=f.mount, post=f.post), f
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


@pytest.mark.parametrize("name", ["aws", "gcp", "azure", "infisical", "vault"])
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


def test_vault_posts_to_the_transit_encrypt_and_decrypt_paths(tmp_path):
    p, fake = _make("vault", tmp_path)
    t, secret = uuid4(), keys.new_tenant_secret()
    blob = keys.wrap_tenant_key(p, t, "data", 1, secret)
    assert blob.startswith(b"vault:v1:")                      # Transit's own ciphertext string, stored as is
    assert keys.unwrap_tenant_key(p, t, "data", 1, blob) == secret
    assert fake.urls == [f"{fake.base_url}/v1/transit/encrypt/molseal", f"{fake.base_url}/v1/transit/decrypt/molseal"]


def test_vault_mount_is_configurable(tmp_path):
    fake = FakeVaultTransit(mount="transit-prod")
    p = VaultTransitKeyProvider(fake.base_url, fake.key_name, token="t", mount="transit-prod", post=fake.post)
    assert p.unwrap(b"l", p.wrap(b"l", b"x" * 59)) == b"x" * 59
    assert fake.urls[0].endswith("/v1/transit-prod/encrypt/molseal")


def test_vault_bad_response_is_a_provider_error(tmp_path):
    p, fake = _make("vault", tmp_path)
    for reply in ({}, {"data": {}}, {"data": {"ciphertext": None}}, {"errors": ["permission denied"]}):
        fake.reply = reply
        with pytest.raises(keys.KeyProviderError) as info:
            p.wrap(b"l", b"p")
        assert info.value.__cause__ is not None


def test_vault_real_post_sends_token_and_namespace_headers(monkeypatch):
    """The default HTTP path, with urlopen replaced: the request must carry X-Vault-Token and, when
    configured, X-Vault-Namespace, and read the reply's data.ciphertext."""
    seen = []

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen.append((req.full_url, req.get_method(), dict(req.header_items()), json.loads(req.data)))
        return Reply(json.dumps({"data": {"ciphertext": "vault:v1:AAAA"}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    p = VaultTransitKeyProvider("https://vault.example.com:8200", "molseal", token="s.TOKEN", namespace="admin")
    assert p.wrap(b"l", b"xyz") == b"vault:v1:AAAA"
    url, method, headers, body = seen[0]
    assert (url, method) == ("https://vault.example.com:8200/v1/transit/encrypt/molseal", "POST")
    assert headers["X-vault-token"] == "s.TOKEN" and headers["X-vault-namespace"] == "admin"
    assert body == {"plaintext": base64.b64encode(b"xyz").decode()}
    p2 = VaultTransitKeyProvider("https://vault.example.com:8200", "molseal", token="s.TOKEN")
    p2.wrap(b"l", b"xyz")
    assert "X-vault-namespace" not in seen[1][2]


def test_vault_requires_https_except_for_loopback():
    with pytest.raises(keys.KeyProviderError, match="https"):
        VaultTransitKeyProvider("http://vault.example", "k", "t")
    VaultTransitKeyProvider("http://127.0.0.1:8200", "k", "t")       # `vault server -dev`
    VaultTransitKeyProvider("https://vault.example", "k", "t")


def _azure_blob(kid: str, encrypted: bytes) -> bytes:
    return struct.pack(">H", len(kid.encode())) + kid.encode() + encrypted


def test_azure_rejects_a_blob_that_names_another_key(tmp_path):
    p, fake = _make("azure", tmp_path)
    t, secret = uuid4(), keys.new_tenant_secret()
    blob = keys.wrap_tenant_key(p, t, "data", 1, secret)
    (n,) = struct.unpack(">H", blob[:2])
    encrypted = blob[2 + n:]
    v = "0123456789abcdef0123456789abcdef"
    asked = list(fake.asked)
    for kid in (f"https://evil.example/keys/molseal/{v}",
                f"https://v.vault.azure.net/keys/other/{v}",
                f"http://v.vault.azure.net/keys/molseal/{v}",
                "https://v.vault.azure.net/keys/molseal",
                "https://v.vault.azure.net/keys/molseal/zz23456789abcdef0123456789abcdef",
                f"https://v.vault.azure.net:8443/keys/molseal/{v}",
                f"https://evil@v.vault.azure.net/keys/molseal/{v}",
                f"https://v.vault.azure.net/keys/molseal/{v}?x=1"):
        with pytest.raises(keys.KeyProviderError):
            keys.unwrap_tenant_key(p, t, "data", 1, _azure_blob(kid, encrypted))
        assert fake.asked == asked
    assert keys.unwrap_tenant_key(p, t, "data", 1, blob) == secret
    upper = _azure_blob(f"https://v.vault.azure.net/keys/MOLSEAL/{v}", encrypted)
    assert keys.unwrap_tenant_key(p, t, "data", 1, upper) == secret


def test_azure_wrap_refuses_an_unversioned_key_id(tmp_path):
    p, fake = _make("azure", tmp_path)
    fake.echo_configured_id = True
    with pytest.raises(keys.KeyProviderError, match="keys/get"):
        p.wrap(b"l", b"x" * 59)


def test_secrets_stay_out_of_repr():
    r = repr(keys.RawKeyProvider(b"\xa5" * 32))
    assert "a5" not in r and "\\xa5" not in r
    assert "SECRET-TOKEN" not in repr(InfisicalKmsKeyProvider("https://eu.infisical.com", "k", "SECRET-TOKEN"))
    assert "SECRET-TOKEN" not in repr(VaultTransitKeyProvider("https://vault.example", "k", "SECRET-TOKEN"))


def test_infisical_requires_https_except_for_loopback():
    with pytest.raises(keys.KeyProviderError, match="https"):
        InfisicalKmsKeyProvider("http://kms.example", "k", "t")
    InfisicalKmsKeyProvider("http://localhost:8080", "k", "t")
    InfisicalKmsKeyProvider("https://kms.example", "k", "t")


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
            p.unwrap(b"l", _azure_blob("https://v.vault.azure.net/keys/k/" + "0" * 32, b"x" * 20))
