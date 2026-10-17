# Changelog

All notable changes to Molseal are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor
version may change the public API; check the `Changed` and `Removed` sections before upgrading.

## [Unreleased]

<!-- Add next-version entries here. To release: move them under a new version heading, set
     __version__ in src/molseal/__init__.py to match, then push a tag vX.Y.Z. -->

### Added
- `rotate_tenant_keys` and `molseal rotate-keys`: give a tenant new data and HMAC keys and re-encrypt its
  rows in place, while vaults keep serving, resumable after a crash. `resume_only=True` (`--resume`) only
  finishes an interrupted rotation.
- `KeyVersionChanged`, a `VersionConflict` for a write sealed under a data key version that is no longer
  current.

### Changed
- **Upgrade every process before the first key rotation.** A 0.1.0 writer bypasses the new store check and
  can write rows under a deleted key; a 0.1.0 `rewrap` can delete the new key rows.
- `StructureStore` gains `start_key_rotation`, `reseal_structures` and `finish_key_rotation`, and a third
  guarantee: `put_structures` refuses records sealed under a data key version other than the tenant's.
- `replace_tenant_keys` replaces exactly the key rows present and raises `VersionConflict` if they changed
  since they were read; `rewrap_tenant` reports that instead of deleting or reviving key rows.
- A vault holds every tenant key version it has seen and loads new ones when it meets them.
- A snapshot under a retired data key version is rebuilt with a warning instead of failing the vault open.
- `molseal rewrap` reports any molseal error per tenant instead of a traceback.
- A row whose nonce or row version was edited raises `SealError` when read, instead of `ValueError` or
  `struct.error`.

### Security
- Exception messages no longer repeat the SMILES, the search query or decrypted payload values, since
  exception text reaches logs and error trackers. `Vault.put` and `Vault.put_many` name the failing
  record id instead.

## [0.1.0] - 2026-10-15

First public release.

### Added
- Encrypted structure store: each record is sealed with AES-256-GCM under a per-tenant data key
  before it reaches SQL storage (PostgreSQL or SQLite through SQLAlchemy).
- In-process search over decrypted structures: substructure search and count, similarity search
  with Tanimoto, Dice, cosine and Tversky metrics, and top-k ranking.
- Fingerprint families `morgan2`, `morgan2-chiral` and `featmorgan2` (Morgan radius 2, 2,048 bits).
- Exact-key lookup through tenant-specific HMAC-SHA256 tokens.
- Encrypted, authenticated index snapshots for fast restarts.
- Key providers: key file, raw key, AWS KMS, Google Cloud KMS, Azure Key Vault, HashiCorp Vault
  Transit and Infisical KMS; tenant keys can be re-wrapped under another provider.
- `molseal` command line: `keygen`, `load`, `search`, `count`, `similar`, `rewrap`, `key-check`.

[Unreleased]: https://github.com/sidxz/molseal/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sidxz/molseal/releases/tag/v0.1.0
