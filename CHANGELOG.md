# Changelog

All notable changes to Molseal are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor
version may change the public API; check the `Changed` and `Removed` sections before upgrading.

## [Unreleased]

<!-- Add next-version entries here. To release: move them under a new version heading, set
     __version__ in src/molseal/__init__.py to match, then push a tag vX.Y.Z. -->

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
