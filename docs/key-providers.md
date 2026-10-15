# Key providers: setting up the key-encryption key

molseal stores each tenant's data key and HMAC key in the `tenant_keys` table. Both keys are encrypted
with a key-encryption key (KEK). Encrypting a key this way is called *wrapping*; decrypting it is
*unwrapping*. A `KeyProvider` handles these operations.

This guide covers setup, permissions, configuration, verification, rotation and migration for each
provider. For an overview of key management, see [README section 4.3](../README.md#43-key-management).

The examples use two placeholders:

- `URL`: your SQLAlchemy database URL, such as `postgresql+psycopg://user:password@host/database`.
- `TENANT`: the tenant UUID from your configuration.

You can also set `MOLSEAL_DATABASE_URL` and `MOLSEAL_TENANT` instead of passing `--db` and `--tenant`.

## 1. How the vault uses a provider

molseal calls the provider in these four situations:

| When | Calls | What happens |
|---|---|---|
| A `Vault` opens on a tenant that has key rows | 2 unwraps | The data key and HMAC key are unwrapped and kept in memory for the lifetime of that `Vault` instance. |
| A `Vault` opens on a tenant with no key rows and no structures | 2 wraps, then 2 unwraps | molseal generates two 32-byte secrets, wraps them, and checks that unwrapping returns the original values before saving them. If either check fails, nothing is written. |
| `rewrap_tenant` or `molseal rewrap` | Per key row: 1 unwrap, 1 wrap, 1 unwrap | Each key is wrapped with the new provider and verified. All key rows for the tenant are then replaced in one transaction. |
| `molseal key-check` | 1 wrap, 3 unwraps | The command tests the provider with a temporary secret and a random tenant ID. Nothing is stored. |

Azure also reads the key once per wrap when the key URL is unversioned (section 7). Row encryption,
exact-key hashing, the index and snapshots all use the tenant keys already in memory. Queries do not
call the provider.

If your application opens its vaults at startup, the key service receives only a few requests per
vault at that point. Revoking the application's access prevents it from opening more vaults. Vaults
that are already open can keep working because they hold the unwrapped keys in memory.

**How keys are tied to a tenant.** Before wrapping a key, the library includes a label containing
the tenant ID, key kind (`data` or `hmac`) and key version in the plaintext. It checks this label
after unwrapping. If someone copies a wrapped key to a different tenant, kind or version, molseal
rejects it.

AWS and Google Cloud also receive the label as encryption context or associated data, so those
services check it too. AWS includes it in audit logs. The Azure Key Vault, Infisical and Vault Transit
providers rely on the library's label check.

## 2. Choosing a provider

| Situation | Provider | Spec string for `--key` / `MOLSEAL_KEY` | Install |
|---|---|---|---|
| Development, tests, a single host | `FileKeyProvider` | `PATH` or `file:PATH` | built in |
| The KEK already lives in a secrets manager your app reads | `RawKeyProvider` | none; library only, see §4 | built in |
| AWS | `AwsKmsKeyProvider` | `aws:KEY_ID` (id, ARN or `alias/NAME`) | `pip install "molseal[aws]"` |
| Google Cloud | `GcpKmsKeyProvider` | `gcp:projects/P/locations/L/keyRings/R/cryptoKeys/K` | `pip install "molseal[gcp]"` |
| Azure | `AzureKeyVaultKeyProvider` | `azure:https://VAULT.vault.azure.net/keys/NAME[/VERSION]` | `pip install "molseal[azure]"` |
| Infisical, cloud or self-hosted | `InfisicalKmsKeyProvider` | `infisical:BASE_URL/KEY_ID` plus `MOLSEAL_INFISICAL_TOKEN` | built in |
| HashiCorp Vault, self-hosted or HCP | `VaultTransitKeyProvider` | `vault:BASE_URL/MOUNT/KEY_NAME` plus `MOLSEAL_VAULT_TOKEN` or `VAULT_TOKEN` | built in |

The CLI, `provider_from_spec`, and the `MOLSEAL_KEY` environment variable all accept the spec strings
shown above. In Python, you can also create a provider directly. Providers that use an SDK let you
pass your own client or credentials when you need a different region, identity or endpoint.

The same four steps apply to every provider:

1. Create the key and give the application permission to wrap and unwrap keys.
2. Run `molseal key-check --key SPEC` with the credentials your application will use. It wraps and
   unwraps a temporary secret, then checks that unwrapping fails with a wrong version or an altered
   byte. It prints `<ProviderClass>: ok` on success, or an error on failure.
3. Set `MOLSEAL_KEY=SPEC` for the CLI, or pass the provider object to `Vault(...)` in Python.
4. Store and manage the KEK separately from the database. Anyone with both a database dump and the
   KEK can decrypt the data.

## 3. File provider

Use the file provider for development and single-host deployments. Generate the key with the CLI,
which writes 32 random bytes with file permissions `600` and refuses to overwrite an existing file:

```bash
molseal keygen /etc/molseal/kek
molseal key-check --key /etc/molseal/kek
export MOLSEAL_KEY=/etc/molseal/kek
```

```python
from molseal import FileKeyProvider
provider = FileKeyProvider("/etc/molseal/kek")
```

The provider reads the file on every wrap or unwrap call. It must contain exactly 32 bytes and must
not grant any permissions to the group or other users. A path containing a colon needs the `file:`
prefix. Store the file on a different host from the database,
or at least outside the database's backup set.

## 4. Raw provider: a KEK from a secrets manager

When the KEK already lives in AWS Secrets Manager, HashiCorp Vault, Infisical secrets, or similar,
fetch the 32 bytes with your own client and pass them to `RawKeyProvider`. molseal does not include
clients for fetching secrets. Generate the value once:

```bash
python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Store the base64 string in your secrets manager, then fetch and decode it at startup:

```python
import base64
from molseal import RawKeyProvider

kek_b64 = fetch_secret("molseal/kek")           # your secrets-manager call
provider = RawKeyProvider(base64.b64decode(kek_b64))
```

`RawKeyProvider` wraps keys with AES-256-GCM inside your application process. It requires a 32-byte
KEK and excludes the key from its `repr` output. It has no spec string, so to use the same KEK with
CLI commands such as `rewrap`, first write it to a file with permissions `600`.

If your service supports one of the providers below, prefer that provider: the service can wrap and
unwrap keys without sending the KEK to your application.

## 5. AWS KMS

**Create the key.** Create a symmetric encryption key and give it an alias. The alias lets you
change keys without changing the application's configuration. See the rotation instructions below
before pointing the alias at a different key.

```bash
aws kms create-key --description "molseal KEK" --key-usage ENCRYPT_DECRYPT --key-spec SYMMETRIC_DEFAULT
aws kms create-alias --alias-name alias/molseal --target-key-id KEY_ID
```

**Grant the application.** The application's IAM role needs `kms:Encrypt` and `kms:Decrypt` on the
key. The default key policy delegates to IAM, so an identity policy suffices. molseal sends the label
as encryption context under the key `molseal_label`, and you can require it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["kms:Encrypt", "kms:Decrypt"],
    "Resource": "arn:aws:kms:REGION:ACCOUNT:key/KEY_ID",
    "Condition": {"ForAnyValue:StringEquals": {"kms:EncryptionContextKeys": "molseal_label"}}
  }]
}
```

**Credentials.** The default `boto3.client("kms")` uses the standard chain: an instance or task role,
`AWS_PROFILE`, or the `AWS_ACCESS_KEY_ID` variables. It needs a region, from the profile or
`AWS_DEFAULT_REGION`. To control any of this, build the client yourself:

```python
import boto3
from molseal.providers.aws import AwsKmsKeyProvider

provider = AwsKmsKeyProvider("alias/molseal", client=boto3.client("kms", region_name="eu-central-1"))
```

**Configure and verify.**

```bash
pip install "molseal[aws]"
molseal key-check --key aws:alias/molseal          # AwsKmsKeyProvider: ok
export MOLSEAL_KEY=aws:alias/molseal
```

Use the full ARN instead of an alias for a key in another account.

**Rotation.** With automatic rotation, KMS retains older key material under the same key ID and can
still decrypt existing ciphertext. No molseal changes are needed. To move to a different key, rewrap
the tenant keys:

```bash
molseal rewrap --db URL --key aws:arn:aws:kms:REGION:ACCOUNT:key/OLD_ID --to aws:alias/molseal --all
```

After you point an alias at a new key, `Decrypt` calls using that alias can no longer decrypt
ciphertext encrypted with the old key. Rewrap from the old key's ARN to the alias before restarting
the application.

**Audit.** Every `Encrypt` and `Decrypt` appears in CloudTrail with `encryptionContext.molseal_label`,
the label encoded as hexadecimal. Its first 32 hex characters are the tenant UUID.

## 6. Google Cloud KMS

**Create the key.** Create a symmetric encryption key in a key ring. Use the key's resource name
when configuring molseal; you do not need to specify a key version.

```bash
gcloud kms keyrings create molseal --location global
gcloud kms keys create molseal-kek --location global --keyring molseal --purpose encryption
```

**Grant the application.** The service account needs `cloudkms.cryptoKeyVersions.useToEncrypt` and
`useToDecrypt` permissions. The predefined role below includes both. Scope the role to the key.

```bash
gcloud kms keys add-iam-policy-binding molseal-kek --location global --keyring molseal \
  --member serviceAccount:APP@PROJECT.iam.gserviceaccount.com \
  --role roles/cloudkms.cryptoKeyEncrypterDecrypter
```

**Credentials.** The default client uses Application Default Credentials: the attached service
account on GCE, GKE workload identity, `GOOGLE_APPLICATION_CREDENTIALS`, or
`gcloud auth application-default login` on a workstation. Pass `client=` to use anything else.

**Configure and verify.**

```bash
pip install "molseal[gcp]"
export MOLSEAL_KEY=gcp:projects/PROJECT/locations/global/keyRings/molseal/cryptoKeys/molseal-kek
molseal key-check --key "$MOLSEAL_KEY"             # GcpKmsKeyProvider: ok
```

```python
from molseal.providers.gcp import GcpKmsKeyProvider
provider = GcpKmsKeyProvider("projects/PROJECT/locations/global/keyRings/molseal/cryptoKeys/molseal-kek")
```

**Rotation.** Cloud KMS ciphertext records which key version encrypted it. After a scheduled or
manual rotation, existing wrapped keys can still be unwrapped as long as that version stays enabled.
New wraps use the primary version. To retire old versions, rewrap the tenant keys with the primary
version before disabling the old ones:

```bash
gcloud kms keys update molseal-kek --location global --keyring molseal --rotation-period 90d --next-rotation-time "2026-11-01T00:00:00Z"
molseal rewrap --db URL --key "$MOLSEAL_KEY" --to "$MOLSEAL_KEY" --all     # same key: re-wrap under the primary version
gcloud kms keys versions list --key molseal-kek --keyring molseal --location global
gcloud kms keys versions disable OLD_VERSION --key molseal-kek --keyring molseal --location global
```

**Audit.** Cloud KMS checks the label as additional authenticated data. Its logs record the caller
and key, but do not include the label. Data-access logs for the Cloud KMS API
are off by default; enable them in the project's audit-log configuration if you want per-call
records.

## 7. Azure Key Vault and Managed HSM

**Create the key.** Create an RSA key with wrap and unwrap operations enabled. molseal uses RSA-OAEP-256. The same
provider works on a Managed HSM with its `managedhsm.azure.net` URL.

```bash
az keyvault create --name VAULT --resource-group RG --location LOCATION --enable-rbac-authorization true
az keyvault key create --vault-name VAULT --name molseal-kek --kty RSA --size 3072 --ops wrapKey unwrapKey
```

**Grant the application.** It needs `wrapKey`, `unwrapKey` and `get` on the key. With the RBAC
permission model, the built-in role `Key Vault Crypto User` holds all three; scope it to the key:

```bash
az role assignment create --assignee APP_PRINCIPAL_ID --role "Key Vault Crypto User" \
  --scope /subscriptions/SUB/resourceGroups/RG/providers/Microsoft.KeyVault/vaults/VAULT/keys/molseal-kek
```

With the legacy access-policy model:

```bash
az keyvault set-policy --name VAULT --object-id APP_OBJECT_ID --key-permissions get wrapKey unwrapKey
```

The `get` permission is needed when you use an unversioned key URL. Before wrapping, the SDK looks
up the current version, and molseal checks that the returned ID belongs to the configured key and
includes a version. If you cannot grant `get`, use a versioned URL such as
`https://VAULT.vault.azure.net/keys/molseal-kek/VERSION`. This avoids the extra key read.

**Credentials.** The default is `DefaultAzureCredential`: the managed identity inside Azure, the
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_CLIENT_SECRET` variables for a service principal, or
`az login` on a workstation. Pass `credential=` for anything else.

**Configure and verify.**

```bash
pip install "molseal[azure]"
export MOLSEAL_KEY=azure:https://VAULT.vault.azure.net/keys/molseal-kek
molseal key-check --key "$MOLSEAL_KEY"             # AzureKeyVaultKeyProvider: ok
```

```python
from molseal.providers.azure import AzureKeyVaultKeyProvider
provider = AzureKeyVaultKeyProvider("https://VAULT.vault.azure.net/keys/molseal-kek")
```

**Rotation.** Each wrapped key stores the versioned key ID, which the provider uses when unwrapping.
After Key Vault creates a new version, either through a rotation policy or manually, existing wrapped
keys can still be unwrapped as long as their version stays enabled. To retire a version, rewrap the
tenant keys with the current version, then disable the old one:

```bash
molseal rewrap --db URL --key "$MOLSEAL_KEY" --to "$MOLSEAL_KEY" --all
az keyvault key set-attributes --vault-name VAULT --name molseal-kek --version OLD_VERSION --enabled false
```

This command assumes an unversioned key URL, so every wrap uses the current version. If you use a
versioned URL, passing the same URL to `--to` would wrap with the old version again. Instead, pass
the new versioned URL to `--to`, then update `MOLSEAL_KEY` to match.

The provider rejects a wrapped key that names a different vault or key before creating an SDK client.

**Audit.** Key Vault logging is opt-in: add a diagnostic setting that sends the `AuditEvent` category
to a Log Analytics workspace or storage account. Each wrap and unwrap is recorded with the caller's
identity.

```bash
az monitor diagnostic-settings create --name molseal-audit \
  --resource /subscriptions/SUB/resourceGroups/RG/providers/Microsoft.KeyVault/vaults/VAULT \
  --workspace WORKSPACE_ID --logs '[{"category": "AuditEvent", "enabled": true}]'
```

## 8. Infisical KMS

This provider works with Infisical Cloud (`https://eu.infisical.com` or `https://us.infisical.com`,
also available at `https://app.infisical.com`) and self-hosted instances. It uses Python's standard
library to call the REST API, so no SDK is needed.

**Create the key.** On the project's KMS page, add a key with a name, encrypt/decrypt usage, and the
AES-256-GCM algorithm. Copy the key's UUID.

**Create an identity and grant access.** In the organization's access control settings, create a
machine identity and add Universal Auth. This gives you a client ID and client secret. The Universal
Auth settings also control the access token's time to live (TTL).

In the project's access control settings, add the identity with a custom project role that grants
only KMS encrypt and decrypt permissions. There is no built-in role specifically for these two
actions. Set the token TTL long enough for the application to open its vaults; see the token lifetime
section below.

**Get a token.** The provider takes a bearer token and never refreshes it. Obtain one from the
identity's credentials, for example:

```bash
export MOLSEAL_INFISICAL_TOKEN=$(curl -s -X POST https://eu.infisical.com/api/v1/auth/universal-auth/login \
  -H 'Content-Type: application/json' \
  -d '{"clientId":"CLIENT_ID","clientSecret":"CLIENT_SECRET"}' | jq -r .accessToken)
```

**Configure and verify.**

```bash
export MOLSEAL_KEY=infisical:https://eu.infisical.com/KEY_ID
molseal key-check --key "$MOLSEAL_KEY"             # InfisicalKmsKeyProvider: ok
```

```python
import os
from molseal.providers.infisical import InfisicalKmsKeyProvider
provider = InfisicalKmsKeyProvider("https://eu.infisical.com", "KEY_ID", os.environ["MOLSEAL_INFISICAL_TOKEN"])
```

The key ID is the text after the last `/` in the spec. Everything before it is the base URL, which
can include a path prefix for a self-hosted instance. The base URL must use `https` because the bearer
token grants access to the tenant keys. For local development, `http` is accepted only for
`localhost`, `127.0.0.1` and `::1`.

**Token lifetime.** A `Vault` unwraps its keys when it opens. It does not need the token again for
queries. If your application opens more vaults later, it must obtain a fresh token when needed and
create a new provider with it. The library does not refresh tokens. An expired token produces
`Infisical KMS decrypt failed: HTTP Error 401`.

If the token expires during `rewrap --all`, tenants that have not been processed keep their old key
rows. Export a fresh token and rerun the command. If you are moving to a different key or provider,
tenants already rewrapped will be reported as failures because the old provider can no longer
unwrap their keys. Their rows are left unchanged.

**Rotation.** Rotating an Infisical KMS key creates a new version and retains earlier versions.
Decryption uses the version that encrypted the ciphertext, so existing wrapped keys still work
without any molseal changes. To rewrap with the current version, use the same spec for `--key` and
`--to`. To move to a different key, use its spec for `--to`:

```bash
molseal rewrap --db URL --key infisical:https://eu.infisical.com/OLD_ID --to infisical:https://eu.infisical.com/NEW_ID --all
```

**Audit.** Infisical's project audit log records each encrypt and decrypt with the identity and the
key. The label is not visible to the service; the library binds it inside the plaintext.

## 9. HashiCorp Vault Transit

In this section, Vault means HashiCorp Vault; `Vault` in code formatting refers to molseal's class.
The provider works with self-hosted Vault, Vault Enterprise and HCP Vault Dedicated. It uses Python's
standard library to call Transit's encrypt and decrypt endpoints, so no SDK is needed.

**Enable the engine and create the key.** Use the default key type, `aes256-gcm96`. Leave key export
and deletion disabled, as they are by default.

```bash
vault secrets enable transit                   # once per mount; the mount path is part of the spec
vault write -f transit/keys/molseal
```

**Grant the application access.** Create a policy that grants `update` on the encrypt and decrypt
paths, then attach it to the application's authentication role:

```hcl
# molseal.hcl
path "transit/encrypt/molseal" { capabilities = ["update"] }
path "transit/decrypt/molseal" { capabilities = ["update"] }
```

```bash
vault policy write molseal molseal.hcl
vault write auth/approle/role/molseal token_policies=molseal token_ttl=1h    # or the Kubernetes, AWS, ... auth method
```

**Token.** The provider sends the token as `X-Vault-Token` and does not renew it. When you use a spec
string, molseal reads `MOLSEAL_VAULT_TOKEN` first, then falls back to `VAULT_TOKEN`. If you obtain a
token through `vault login` or a Vault Agent file, export it in one of these variables before running
molseal.

On Vault Enterprise and HCP Vault, set `MOLSEAL_VAULT_NAMESPACE` to the namespace you use. HCP Vault
Dedicated uses `admin`. The provider sends this value as `X-Vault-Namespace`.

**Configure and verify.** The spec is the base URL followed by the mount path and the key name, in
the order the Vault CLI uses:

```bash
export VAULT_TOKEN=$(vault write -field=token auth/approle/login role_id=ROLE_ID secret_id=SECRET_ID)
export MOLSEAL_KEY=vault:https://vault.example.com:8200/transit/molseal
molseal key-check --key "$MOLSEAL_KEY"             # VaultTransitKeyProvider: ok
```

```python
import os
from molseal.providers.vault import VaultTransitKeyProvider
provider = VaultTransitKeyProvider("https://vault.example.com:8200", "molseal", os.environ["VAULT_TOKEN"],
                                   mount="transit", namespace=None)
```

The base URL must use `https` because the token grants access to the tenant keys. For local
development, including `vault server -dev`, `http` is accepted only for `localhost`, `127.0.0.1`
and `::1`.

**Rotation.** Transit ciphertext starts with `vault:vN:`, where `N` is the key version used to
encrypt it. Decryption uses that version automatically. After rotation, existing wrapped keys still
work, and new wraps use the latest version. To retire old versions, rewrap the tenant keys with the
latest version, then raise the minimum decryption version:

```bash
vault write -f transit/keys/molseal/rotate
molseal rewrap --db URL --key "$MOLSEAL_KEY" --to "$MOLSEAL_KEY" --all     # same key: re-wrap under the latest version
vault write transit/keys/molseal/config min_decryption_version=2
```

**Audit.** Enable an audit device to log encrypt and decrypt calls with the token's accessor and
policies. Request body values are recorded as HMAC hashes, so the logs do not contain the wrapped
keys themselves.

```bash
vault audit enable file file_path=/var/log/vault/audit.log
```

## 10. Migrating between providers

`molseal rewrap` unwraps each of a tenant's keys with the provider specified by `--key` and wraps it
with the provider specified by `--to`. It checks that the new wrapped value decrypts to the same
secret, then replaces all of the tenant's key rows in one transaction. Structure rows do not change.

Existing `Vault` instances keep using the tenant keys in memory. Any instance opened after the
migration must use the new provider.

A typical move from a key file to a key service:

```bash
molseal key-check --key aws:alias/molseal                         # 1. the new provider works from this identity
molseal rewrap --db URL --key /etc/molseal/kek --to aws:alias/molseal --all   # 2. every tenant
molseal count --db URL --key aws:alias/molseal --tenant TENANT 'c1ccccc1'     # 3. any command that opens the vault
export MOLSEAL_KEY=aws:alias/molseal                              # 4. switch configuration, restart
shred -u /etc/molseal/kek                                         # 5. retire the file
```

Step 3 checks that `count` can open the vault with the new provider and run a substructure count.
Vaults opened before step 2 keep working throughout the migration. Between steps 2 and 4, opening a
vault for a migrated tenant with the old configuration fails because the old provider cannot unwrap
its keys. This also applies to new `Vault` instances in a running process. Keep that interval short,
or pause the creation of vault instances during the migration.

Without `--all`, `rewrap` handles only the tenant specified by `--tenant`. With `--all`, it processes
every tenant and continues if one fails. It reports failures on stderr and exits with status 1 if
any tenant failed. Failed tenants keep their original key rows.

To move from a key service back to a file, use the same command with the source and destination specs
swapped.

Rewrapping does not rotate the tenant keys themselves. That would require re-encrypting the structure
rows. The database has version columns for tenant keys, but molseal does not yet have a command to
rotate them.

## 11. Troubleshooting

| Message | Meaning | Fix |
|---|---|---|
| `wrapped key did not open: wrong KEK, wrong label, or altered blob` | The file or raw KEK is not the one the rows were wrapped with. | Point `--key` at the right file, or rewrap from the old key if you still have it. |
| `wrapped key label does not match tenant, kind and version` | The blob opened but was made for another tenant, kind or version. | Key rows were moved or edited; restore them from a trusted backup. |
| `AWS KMS decrypt failed: ... AccessDeniedException` | The identity lacks `kms:Decrypt`, or the key policy excludes it. | Grant both actions on the key; check the encryption-context condition if you added one. |
| `AWS KMS decrypt failed: ... IncorrectKeyException` | The ciphertext was made under a different key than `KeyId` names. | An alias was re-pointed. Rewrap from the old key's ARN. |
| `Key Vault returned key id ..., which is not a versioned id of the configured key` | `get` is not granted, or the URL points at another key. | Grant `get`, or configure the versioned key URL. |
| `Azure wrapped key blob names a key id outside the configured key` | The row was wrapped under a different vault or key name. | Rewrap from the provider that wrote it, or fix the configured URL. |
| `Infisical base_url must use https` | A plain-http base URL on a non-loopback host. | Use `https://`, or a loopback address for a local instance. |
| `infisical spec is infisical:BASE_URL/KEY_ID with MOLSEAL_INFISICAL_TOKEN set` | The spec lacks a slash or the token variable is empty. | Check the spec and export a current token. |
| `Infisical KMS decrypt failed: HTTP Error 401` | The bearer token expired or was revoked. | Log in again with the identity's credentials and export the new token. |
| `Infisical KMS decrypt failed: HTTP Error 403` | The identity is not in the project, or its role lacks KMS decrypt. | Add the identity to the project with a role that has encrypt and decrypt. |
| `Vault Transit decrypt failed: HTTP Error 403` | The token expired, or its policy lacks `update` on the decrypt path. | Log in again, or fix the policy; `vault token lookup` shows the token's policies and TTL. |
| `Vault Transit encrypt failed: HTTP Error 404` | Wrong mount path in the spec, or the Transit engine is not enabled there. | `vault secrets list` shows the mounts; the spec is `vault:BASE_URL/MOUNT/KEY_NAME`. |
| `Vault Transit decrypt failed: HTTP Error 400` | The blob was altered, was made under another key, or its version is below `min_decryption_version`. | Rewrap from the provider that wrote it, or lower the minimum decryption version. |
| `vault spec is vault:BASE_URL/MOUNT/KEY_NAME with MOLSEAL_VAULT_TOKEN (or VAULT_TOKEN) set` | The spec is missing the mount or key segment, or no token variable is set. | Check the spec and export a token. |
| `... provider needs boto3: pip install molseal[aws]` (or gcp, azure) | The scheme's SDK is not installed. | Install the extra in the application's environment. |
| `tenant key rows are missing for the current key versions` | The `tenants` row points at a key version that has no `tenant_keys` row. | Restore the key rows; this is a damaged database, not a provider issue. |
| `tenant ... has structures but no key rows; refusing to mint new keys` | Key rows were deleted but structures remain. | Restore the key rows. Minting new ones would orphan every row. |
| `key provider does not round-trip the new data key; nothing was written` | The provider can wrap but not unwrap, usually a missing decrypt permission. | Grant the decrypt or unwrap permission and retry; no key rows were created. |
| `/etc/molseal/kek is readable by group or others` | The key file's mode is too open. | `chmod 600` it. |

Most errors above raise `KeyProviderError`. The two damaged-database cases, missing key rows and
structures without key rows, raise `VaultError`. Both inherit from `MolsealError`. The
cloud providers preserve the underlying exception as `__cause__`, so you can see the original error
in the message and traceback.

The test suite uses simulated cloud services. Before using a new provider, run `molseal key-check`
with your application's credentials to check access to the live service.
