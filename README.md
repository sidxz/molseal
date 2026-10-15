# Molseal

**Encrypted molecular storage with RDKit**

Molseal addresses a security problem in cheminformatics applications: how to keep molecular data
encrypted in the database while retaining the chemical operations that make the data useful. It
encrypts molecular records before they reach SQL storage and can perform substructure search,
similarity search, and exact-key lookup.

Molecular records use **AES-256-GCM authenticated encryption**, with 256-bit
keys, randomly generated 96-bit nonces, and 128-bit authentication tags. This provides confidentiality
and detects ciphertext tampering during decryption.
It uses the Python `cryptography` library's
[AES-GCM primitive](https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM).

The underlying molecular payloads remain encrypted in the database. Structures and fingerprints
are available in plaintext within the application process, where chemical computation takes place.


**Project status:** pre-1.0. The public API may change between minor versions.

**Contents:** [1. Motivation](#1-motivation) · [2. Objective and approach](#2-project-objective-and-approach) ·
[3. Architecture](#3-system-architecture) · [4. Security model](#4-security-model-and-boundaries) ·
[5. Chemical search](#5-chemical-search-methods) · [6. Evaluation](#6-evaluation) ·
[7. Installation and usage](#7-installation-and-usage) · [8. Limitations](#8-limitations-and-future-work) ·
[9. Development](#9-development-and-reproducibility) · [License and data attribution](#license-and-data-attribution)

## 1. Motivation

Molecular structures can encode confidential research and proprietary compound designs. An
application may need to store these structures in a managed or shared database without granting the
database operator access to their chemical content. The security requirement is therefore to keep
readable molecular data outside the database's trust boundary while preserving useful computation.

This requirement creates a practical difficulty for applications built around the RDKit PostgreSQL
cartridge. Their chemical queries depend on representations that PostgreSQL can interpret. Encrypting
those representations at the application boundary removes the information that the cartridge needs
to execute its ordinary chemistry functions.

### 1.1 How database-side chemistry depends on readable structures

The RDKit cartridge adds molecular and fingerprint types, chemical operators, descriptor functions,
and GiST index support to PostgreSQL. Substructure matching operates on molecular graphs; similarity
search operates on fingerprints that encode structural features. These operations consume RDKit
representations rather than arbitrary encrypted bytes. The documented interface provides no native
mechanism for applying these operators directly to application-encrypted molecular payloads. [RDKit cartridge reference](https://www.rdkit.org/docs/Cartridge.html).

Binary storage does not itself provide confidentiality. Greg Landrum's RDKit community tutorial
demonstrates transferring binary molecules into PostgreSQL, searching them, and reconstructing them
in Python. Serialization changes the representation while retaining the molecular information. [Binary molecules and the cartridge](https://greglandrum.github.io/rdkit-blog/posts/2023-05-07-binary-molecules-and-the-cartridge.html).

### 1.2 Why adding encryption alone is insufficient

PostgreSQL supports several forms of encryption with different security boundaries. Transport
encryption protects connections, and disk or filesystem encryption protects storage media. These can
coexist with the cartridge because the running database still receives readable values. PostgreSQL's
documentation identifies client-side encryption as the approach for keeping plaintext away from an
untrusted database host. [PostgreSQL encryption options](https://www.postgresql.org/docs/current/encryption-options.html).

Column encryption with `pgcrypto` can also coexist with RDKit if the database decrypts values before
using them. However, decryption then occurs inside PostgreSQL. Its documentation explicitly requires
trust in the system and database administrators and recommends application-side cryptography when
that trust is unavailable. [pgcrypto security limitations](https://www.postgresql.org/docs/current/pgcrypto.html).

The consequence for the cartridge follows from these interfaces: encrypting SMILES, serialized
molecules, or fingerprints before insertion makes their ciphertext unusable by the corresponding
ordinary chemical operators. Decrypting them in SQL restores functionality but also restores the
database's access to their content. Keeping a readable molecular column or fingerprint index alongside
an encrypted source field similarly leaves chemical information exposed.

| Storage and computation arrangement | Can the database access chemical content? | Where chemical operations execute |
|---|---|---|
| Cartridge with transport and disk encryption | Yes, during normal operation | PostgreSQL |
| Encrypted columns decrypted in SQL | Yes, during decryption and computation | PostgreSQL |
| Encrypted source structures with readable molecular or fingerprint columns | Yes, through the derived representations | PostgreSQL |
| Application-encrypted payloads with keys outside PostgreSQL | Payloads remain encrypted; ordinary cartridge operators cannot use them directly | Requires computation outside PostgreSQL |
| Molseal | Payloads remain encrypted; metadata and equality tokens are visible | Trusted application process |

The gap is thus **preserving chemical functionality when the database cannot decrypt molecular
content**. Addressing it requires a storage and execution model that keeps both the source structures
and their derived search representations within the intended confidentiality boundary.

### 1.3 Fingerprints are also confidential chemical data

Encrypting the original structure while exposing its fingerprint is insufficient for this security
objective. Fingerprints retain information about molecular features. The Neuraldecipher study
demonstrated reconstruction of molecular structures from exposed extended-connectivity fingerprints
under its evaluated conditions; reconstruction depends on the representation and data and is not
guaranteed for every molecule. This nevertheless establishes that fingerprints should not be treated
as a cryptographic substitute for encryption. [Le et al., Chemical Science, 2020](https://pubs.rsc.org/en/content/articlelanding/2020/sc/d0sc03115a).

The issue has also been discussed directly in the RDKit community: an `rdkit-discuss` thread on
reverse engineering fingerprints includes the study author's explanation of recovering SMILES from
ECFP descriptors. This provides community context for protecting derived representations as well as
source structures. [RDKit fingerprint reconstruction discussion](https://sourceforge.net/p/rdkit/mailman/rdkit-discuss/thread/7DED5A78-CA72-4A26-BA82-2F5F76458466%40bayer.com/).

![Comparison of database-side chemical search and application-side search with encrypted storage.](docs/images/before-after.svg)

## 2. Project objective and approach

Molseal provides an encrypted storage and search layer for applications that need RDKit functionality
while keeping molecular content confidential from the database. Its central design requirement is:

> Persist molecular payloads only as ciphertext, keep decryption keys and readable search
> representations outside the database, and execute chemical operations within the trusted application.

PostgreSQL or SQLite provides persistence and transactional updates. The application encrypts and
decrypts records, maintains an in-memory RDKit search index, and returns results through the vault API.
The database does not require the RDKit extension. Fingerprints are computed in the application and
persisted only within encrypted index snapshots. A keyed lookup table supports exact-key queries
without storing the corresponding values in plaintext.

The current interface provides the following operations:

| Application requirement | Molseal support |
|---|---|
| Find molecules containing a chemical pattern | Substructure search and counts, including generalized and chirality-aware options |
| Retrieve compounds similar to a reference | Fingerprint similarity with configurable families, metrics, thresholds, and result limits |
| Retrieve records sharing a defined identifier | Authenticated exact-key lookup using tenant-specific HMAC tokens |
| Perform further RDKit calculations | Decrypted payloads and fingerprint access within the trusted application |
| Keep searchable state consistent with stored records | Transactional writes, cursor-based synchronization, and encrypted snapshots |

This is an application API with a defined set of operations. Existing SQL queries that invoke the
cartridge must be adapted to that API; Molseal does not implement every RDKit function or provide a
transparent replacement for the cartridge's SQL interface. Additional descriptors and analyses can
be computed by the application from retrieved structures, subject to the same confidentiality boundary.

The contribution is the integration of encrypted persistence, key separation, and chemical execution
within one library. The evaluation assesses whether this separation preserves the documented search
semantics and provides practical performance. Collection size is an evaluation parameter, while
confidentiality from the database is the motivating requirement.

### 2.1 Relationship to privacy-preserving chemical search

Cryptographic chemical search has an existing research literature. For example, Shimizu et al.
developed a similarity-search protocol using additive homomorphic encryption to protect query and
database information during an interaction between separate parties. [Privacy-preserving search for chemical compound databases, 2015](https://link.springer.com/article/10.1186/1471-2105-16-S18-S6).

Molseal uses a different trust model: the application is authorized to decrypt the collection, and
the database is responsible for encrypted persistence. Chemical computation occurs on plaintext in
application memory. Its security therefore depends on protecting that process and its keys; it does
not provide homomorphic execution or conceal structures from the application performing the search.

## 3. System architecture

![System architecture showing the trusted application and key provider, and the database storage boundary.](docs/images/architecture.svg)

The system comprises three principal components:

| Component | Responsibility | Data held |
|---|---|---|
| Application process | Encryption, decryption, index maintenance, and chemical search | Plaintext structures, fingerprints, and active keys |
| Key provider | Supply the key-encryption key independently of database storage | Key-encryption key |
| SQL database | Persist records and coordinate updates | Ciphertexts, wrapped tenant keys, keyed lookup hashes, and operational metadata |

A `Vault` is the application-facing interface for one tenant. It combines a `StructureStore`, a
`KeyProvider`, and a `StructureIndex`. The supplied `SqlStore` supports PostgreSQL and SQLite. The
index uses RDKit's substructure library and a packed fingerprint matrix for each configured
fingerprint family.

Record identifiers belong to the calling application. Searches return identifiers, or identifiers
with similarity scores, which the application can associate with its own registry and experimental
metadata. Molseal does not impose a compound registry schema or manage those external tables.

### 3.1 Record ingestion and encryption

![Write path: canonicalization, payload encoding, padding, authenticated encryption, and database storage.](docs/images/write-path.svg)

For each inserted or updated structure, the application:

1. Parses and canonicalizes the input SMILES with RDKit.
2. Encodes the canonical SMILES, caller-supplied exact keys, and deletion state in a JSON payload.
3. Pads the payload to a power-of-two size class, with a minimum of 256 bytes.
4. Encrypts the payload using AES-256-GCM and a fresh random 96-bit nonce.
5. Computes HMAC-SHA256 values for any exact keys using a separate tenant-specific key.
6. Commits the encrypted record, lookup hashes, and change-sequence update in one transaction.
7. Updates the local search index.

Authenticated associated data binds a ciphertext to its tenant, record identifier, data-key version,
and row version. Changing these labels causes authentication to fail during decryption. Concurrent
writers use row-version comparisons and retry conflicting writes.

`put_many` performs validation and commits in chunks. Each chunk is atomic; a failure in a later
chunk does not undo earlier committed chunks. Deletion creates an encrypted tombstone and removes
the record from the active search set.

### 3.2 Search and synchronization

![Search path: inspect the tenant cursor, synchronize changed records, and query the in-memory index.](docs/images/search-path.svg)

Before a substructure or similarity search, the vault reads the tenant's change cursor. If the
cursor has advanced, it retrieves changed records, decrypts their payloads, and updates the local
index before evaluating the query. With an unchanged cursor, this path requires a tenant-state
read followed by in-memory search.

A successful local write updates the index in the same call. Other processes discover committed
changes through subsequent cursor checks. A backward-moving cursor triggers a rebuild; the refresh
path also rebuilds when the observed live count disagrees with the index state. These mechanisms
support synchronization but do not establish the authenticity of the database's complete history.

Exact-key lookup follows a separate path: the application computes the keyed hash, retrieves
candidate records from the database, and verifies the requested value against their decrypted
payloads.

### 3.3 Encrypted snapshots

The in-memory index can be saved as an encrypted snapshot to reduce startup work. On initialization,
the vault loads an available, compatible snapshot and synchronizes subsequent database changes.
Without a usable snapshot, it rebuilds the index from encrypted database records.

Snapshots are saved explicitly through `save_snapshot()`. Fingerprint families missing from a
snapshot are computed from its stored structures when loaded. Saving a new snapshot preserves those
computed families for later starts.

## 4. Security model and boundaries

The principal adversary has read access to the database, including its data files, backups, replicas,
write-ahead log, and statement logs. The design also considers modification of database records.
The application process and key provider are trusted, and the key-encryption key must remain outside
the database host and its backup set.

### 4.1 Confidentiality and integrity

Under these assumptions, molecular payloads are encrypted before they reach SQL storage. Plaintext
structures and fingerprints are retained in application memory, while exact-key values are stored
inside encrypted payloads and represented in the lookup table by keyed hashes. Without the HMAC key,
a database-only attacker cannot directly hash a candidate identifier to confirm a guess.

Authenticated encryption detects altered payloads and substitutions across the bound tenant, record,
and version labels. Wrapped keys are similarly bound to tenant, key type, and version. Deletion state
and exact-key values are verified from authenticated payloads, and snapshots are authenticated when
loaded.

These properties do not establish completeness or freshness. An attacker may delete records,
suppress lookup results, replay an earlier valid record, or restore an older database. Without an
external trusted record of state, Molseal cannot reliably distinguish all such actions from valid
history or guarantee availability.

### 4.2 Observable information

Encryption does not conceal all database information. Visible metadata includes record and tenant
identifiers, row counts, timestamps, versions, deletion flags, change sequences, and exact-key categories. Repeated
lookup hashes reveal equality within a tenant. Database access also exposes which records are
retrieved and when.

Padding reduces precision in payload-length observations, but the resulting size class remains
visible and may correlate with molecular size. The benchmark includes a leakage report that measures
this relationship. Equality and metadata may also support inferences when combined with outside
knowledge.

The confidentiality boundary applies to Molseal's database representation. Input files, application
logs, exported payloads, and process memory require protection by the surrounding application. The
CLI loader creates a plaintext `.ids.tsv` sidecar for validation; this file contains structures and
exact keys and belongs within the trusted environment.

### 4.3 Key management

Each tenant has a data-encryption key and a separate HMAC key, both random 32-byte secrets generated
in the application. They are stored wrapped under a key-encryption key (KEK) that a `KeyProvider`
controls. The library binds the tenant identifier, key type and version into every wrapped key and
checks them on unwrap, so a key row moved to another tenant, type or version does not open, whichever
provider is configured.

Two provider models are supported.

| Model | Where the KEK lives | Providers |
|---|---|---|
| In-process | In application memory, loaded from a file or fetched from a secrets manager by the application's own client | `FileKeyProvider`, `RawKeyProvider` |
| Key service | In a key management service that wraps and unwraps on request and never releases the key | `AwsKmsKeyProvider`, `GcpKmsKeyProvider`, `AzureKeyVaultKeyProvider`, `InfisicalKmsKeyProvider` |

With a key service the KEK never enters the application process. Only the tenant keys do, and they
must, because they encrypt rows and hash exact keys. Revoking the application's access stops any new
process from opening the vault, and every wrap and unwrap appears in the service's audit log. The AWS
and Google providers pass the tenant label as an encryption context or associated data; Azure Key Vault
and Infisical accept none, which the library-side binding covers. The cloud providers are optional
extras (`molseal[aws]`, `molseal[gcp]`, `molseal[azure]`); the Infisical provider uses the standard
library.

Each service needs both directions of the operation: AWS `kms:Encrypt` and `kms:Decrypt` on the key;
Google Cloud `cloudkms.cryptoKeyVersions.useToEncrypt` and `useToDecrypt`, with a key name rather than
a key version name; Azure `get`, `wrapKey` and `unwrapKey`, or a versioned key id when `get` is not
granted; an Infisical machine identity allowed to encrypt and decrypt with the key. Operations appear
in the service's audit log where that log is enabled; Google Cloud data-access logs and Azure
diagnostics are opt-in. An AWS alias on decrypt refuses ciphertext made under a key the alias no
longer points to; after re-pointing an alias, rewrap from the old key's ARN to the alias.

`rewrap_tenant`, and the `molseal rewrap` command, re-wrap a tenant's keys under another provider.
This serves KEK rotation and migration from a file to a service. Each new wrapped key is verified
before the key rows are replaced in one transaction, no structure row is touched, and running
processes keep working because they hold the unwrapped tenant keys. Rotation of the tenant keys
themselves, which re-encrypts rows, is not implemented; the version columns anticipate it.

Every deployment names at least one tenant. The tenant identifier is the unit of key scope (data key,
HMAC key, change cursor and live count), not an opt-in multi-tenancy feature. A single-tenant
deployment uses one fixed UUID kept in configuration next to the database URL. The identifier is an
address, not a secret: it is stored in the clear and can be recovered from the `tenants` table.

In rising order of damage: database access alone exposes the metadata of §4.2; the HMAC key in
addition allows testing whether a known identifier is present; a tenant's data key decrypts that
tenant's rows and snapshots and nothing else; the KEK, or the service access that stands in for it,
unwraps every tenant's keys.

Encryption uses randomly generated nonces. The implementation does not enforce a per-key encryption
budget.

## 5. Chemical search methods

### 5.1 Fingerprint representations

A fingerprint family is described by `FingerprintSpec(name, bits, generator)`. Its generator is a
zero-argument factory returning an RDKit fingerprint generator. The bit width must be a positive
multiple of 64 and less than 65,536. A family name identifies its semantics and must change if the
generator definition changes.

| Family | Representation | Width | Packed storage per indexed position |
|---|---|---|---|
| `morgan2` (default) | Morgan, radius 2 | 2,048 bits | 258 bytes |
| `morgan2-chiral` | Morgan, radius 2, with chirality | 2,048 bits | 258 bytes |
| `featmorgan2` | Feature-Morgan, radius 2 | 2,048 bits | 258 bytes |

The storage figure includes 256 bytes of fingerprint bits and a two-byte population count; it
excludes the rest of the index and spare allocation capacity. Families are configured through
`Vault(..., families=...)` and used during rebuilds and snapshot loading. Only binary fingerprints
are supported.

### 5.2 Similarity search

Similarity search computes bit intersections against the stored fingerprint matrix. For query and
target bit counts `a` and `b`, and intersection count `c`, the supplied metrics are:

| Metric | Definition |
|---|---|
| Tanimoto | `c / (a + b − c)` |
| Dice | `2c / (a + b)` |
| Cosine | `c / sqrt(a × b)` |
| Tversky | `c / (α(a − c) + β(b − c) + c)` |

The implementation returns zero when the denominator is zero. Tversky's `query_weight` and
`target_weight` correspond to `α` and `β`; both lie in `[0, 1]`. With `tversky(1, 0)`, the score is the
fraction of query fingerprint bits present in the target. This is a fingerprint containment measure,
not a molecular substructure test.

```python
vault.similar(smiles, threshold=0.7, top_k=None, family="morgan2", metric=metrics.tanimoto)
```

Results are `(identifier, score)` pairs ordered by decreasing score. Search exhaustively compares
fingerprints; it uses no approximate nearest-neighbor method or similarity index. Scores are exact
for the selected fingerprint representation and metric. Custom metrics may be supplied as functions
accepting intersection counts, a query bit count, and target bit counts, and returning a NumPy score
array.

### 5.3 Substructure search

```python
vault.substructure(query, kind="smiles", max_results=None,
                   generalized=False, use_chirality=False)
vault.count_substructure(query, kind="smiles",
                         generalized=False, use_chirality=False)
```

| Option | Behavior |
|---|---|
| `kind="smiles"` | Parse a molecular structure as the query |
| `kind="smarts"` | Interpret the supplied query pattern literally, without aromaticity perception |
| `kind="molblock"` | Sanitize the molblock and remove explicit hydrogens |
| `generalized=True` | Use RDKit's extended query representation for tautomer-insensitive matching, with the defaults used by the cartridge's `@>>` operation |
| `use_chirality=True` | Require defined query stereochemistry to match defined target stereochemistry |
| `max_results` | Limit returned matches to an arbitrary subset; subset stability across runs requires one thread |

Substructure matching uses RDKit's substructure library with pattern-fingerprint screening; the screen
is RDKit's tautomer pattern holder, so generalized queries keep it. Searches
can use multiple RDKit threads; a vault serializes its search and update operations with one lock.

### 5.4 Chemical interpretation and constraints

Canonicalization is the only structure preparation performed during ingestion. Salt handling,
charge normalization, and tautomer standardization remain application responsibilities. Stored
structures and queries should follow a consistent preparation policy.

Similarity remains tautomer-sensitive even when generalized substructure search is enabled.
Thresholds are specific to the fingerprint family and metric; a meaningful search preset therefore
specifies all three of family, metric, and threshold.

With chirality enabled, undefined query stereochemistry does not constrain the target, whereas a
defined query stereocenter requires a matching defined target. The RDKit trusted SMILES holder does
not assign double-bond stereochemistry, so E/Z queries first retrieve candidates without chirality
and then reparse and verify each candidate with chirality enabled. Broad queries can consequently
incur substantial additional work while holding the vault lock.

Generalized queries have a further limitation: RDKit's extended query representation can discard
tetrahedral stereochemistry within tautomeric regions. For example, generalized matching of
`N[C@@H](C)C(=O)O` returns both alanine enantiomers even with chirality enabled.

SMARTS aromaticity is literal: a Kekulé SMARTS pattern does not match an aromatic target merely
because the forms represent the same chemistry. Molblock queries remove explicit hydrogens so that
hydrogens added by a sketcher do not become required query atoms.

### 5.5 Exact lookup and analytical access

`find_exact(kind, value)` retrieves records by a caller-supplied exact key. The library does not
define chemical identity or automatically generate those keys; their meaning depends on the
application's registration and standardization policy. A deployment without a registry can use the
structure itself as the key: register with `vault.put(id, smiles, {"smiles": canonicalize(smiles)})`
and look up with `vault.find_exact("smiles", canonicalize(query))`, where `molseal.canonicalize` is
the same function `put` applies before encrypting.

Bulk access supports downstream analysis:

```python
payloads = vault.get_many(ids)                          # {id: Payload}, with .smiles and .keys
found, rows = vault.fingerprints(ids, family="morgan2")  # corresponding ids and packed uint8 rows
```

Unknown and deleted records are omitted. Fingerprint access returns live, indexed records in the
requested order. A packed row can be converted to an RDKit bit vector as follows:

```python
import numpy as np
from rdkit import DataStructs

bv = DataStructs.ExplicitBitVect(2048)
bv.SetBitsFromList(np.flatnonzero(np.unpackbits(row)).tolist())
```

Applications can use these representations for clustering, diversity selection, or scaffold
analysis; Molseal does not implement those workflows. Fingerprints encode structural information
and should be treated as confidential. The [registry adapter example](docs/examples/registry-adapter.md)
illustrates how the primitives support registration, sketcher queries, deduplication, and analysis.

## 6. Evaluation

The evaluation addresses two questions: whether Molseal returns the same results as database-side
chemistry, and whether its stored representation exposes molecular content.

Correctness is assessed with parity gates. Each gate runs the same queries through Molseal and
through a reference implementation and requires identical hit sets, counts, and scores. The reference
is the RDKit PostgreSQL cartridge where it provides the operation and RDKit in Python where it does
not. Ten gates cover fingerprint bytes, substructure matching, similarity for each family and metric,
generalized and chirality-aware matching, and molblock queries. A benchmark run publishes results
only when every gate passes.

Confidentiality is assessed with an absence check (`bench/leak.py`). It searches for every stored structure and exact
key in the database columns, in a `pg_dump` output, and in a statement log, using a planted control
record that each search must detect. A leakage report measures how much the ciphertext size class
reveals about molecular size.

On 500,000 ChEMBL structures, single-threaded substructure search matched the speed of the indexed
cartridge, and similarity search ran within a few milliseconds of it. The procedure, result files,
and leakage report are available under [`docs/results/`](docs/results/README.md).

## 7. Installation and usage

### 7.1 Environment

The project requires Python 3.12 or later. Dependencies include RDKit, NumPy, SQLAlchemy, psycopg,
cryptography, and Pydantic. From a local checkout, install the project with:

```bash
pip install .
```

Key service providers are optional extras: `pip install ".[aws]"`, `".[gcp]"` or `".[azure]"`. The
Infisical provider needs no extra.

For development, use `uv sync`. PostgreSQL integration tests can use the supplied development service:

```bash
docker compose up -d db
```

This starts PostgreSQL on port 5439. SQLite can be used for a self-contained local example.

### 7.2 Example

The walkthrough below runs as is on SQLite in a temporary directory and exercises every public
operation: registration, substructure and similarity search, exact lookup, bulk reads, edits,
deletion, error handling, and snapshots. For a deployment, use a PostgreSQL URL such as
`postgresql+psycopg://user:password@host/database`, keep one fixed tenant UUID in configuration,
and store the key file, or a key-service reference, away from the database host and its backups.

```python
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import sqlalchemy as sa
from rdkit import Chem

from molseal import (FEATMORGAN2, MORGAN2, FileKeyProvider, MolsealError, SqlStore, StructureError,
                     Vault, canonicalize, generate_key_file, metrics)

DRUGS = {
    "aspirin": "CC(=O)Oc1ccccc1C(=O)O",
    "salicylic acid": "O=C(O)c1ccccc1O",
    "paracetamol": "CC(=O)Nc1ccc(O)cc1",
    "ibuprofen": "CC(C)Cc1ccc(cc1)C(C)C(=O)O",
    "caffeine": "Cn1cnc2c1c(=O)n(C)c(=O)n2C",
}


def inchikey(smiles: str) -> str:       # the application computes exact keys; the library never does
    return Chem.MolToInchiKey(Chem.MolFromSmiles(smiles))


with TemporaryDirectory() as directory:
    root = Path(directory)

    # --- open ----------------------------------------------------------------------------------
    key_path = root / "molseal.key"
    generate_key_file(key_path)                      # 32 random bytes, mode 600
    tenant = uuid.UUID("3f6f0f8e-9c7e-4b1c-9a2a-0d9c1f2e3a4b")   # the unit of key scope; keep it in config
    store = SqlStore(sa.create_engine(f"sqlite:///{root / 'structures.db'}"))
    store.create_tables()
    vault = Vault(
        store,
        FileKeyProvider(key_path),                   # or provider_from_spec("aws:alias/molseal"), see §4.3
        tenant,
        snapshot_dir=root / "snapshots",
        families=(MORGAN2, FEATMORGAN2),             # fingerprint families the index carries
    )

    # --- register ------------------------------------------------------------------------------
    ids = {name: uuid.uuid4() for name in DRUGS}
    vault.put(ids["aspirin"], DRUGS["aspirin"],
              keys={"inchikey": inchikey(DRUGS["aspirin"]), "name": "aspirin"})
    vault.put_many((ids[n], s, {"inchikey": inchikey(s), "name": n})     # bulk: one transaction per chunk
                   for n, s in DRUGS.items() if n != "aspirin")
    assert len(vault) == 5

    # --- substructure --------------------------------------------------------------------------
    ring_oxygen = vault.substructure("c1ccccc1O")                 # aspirin, salicylic acid, paracetamol
    acids = vault.substructure("[CX3](=O)[OX2H1]", kind="smarts")  # aspirin, salicylic acid, ibuprofen
    first_two = vault.substructure("c1ccccc1", max_results=2)
    assert vault.count_substructure("c1ccccc1") == 4
    # generalized=True also matches tautomers; use_chirality=True makes the query's stereo mandatory;
    # kind="molblock" takes a sketcher's drawing

    # --- similarity ----------------------------------------------------------------------------
    hits = vault.similar(DRUGS["aspirin"], threshold=0.3)          # Morgan r2 Tanimoto: aspirin 1.0, salicylic acid 0.45
    best_two = vault.similar(DRUGS["aspirin"], threshold=0.0, top_k=2)
    scaffold_hops = vault.similar(DRUGS["aspirin"], threshold=0.3, family="featmorgan2")
    containing = vault.similar("O=C(O)c1ccccc1", threshold=0.7,    # Tversky(1, 0): how much of the query
                               metric=metrics.tversky(1, 0))       # each structure contains
    assert [i for i, _ in hits] == [ids["aspirin"], ids["salicylic acid"]]

    # --- exact lookup --------------------------------------------------------------------------
    assert vault.find_exact("inchikey", inchikey(DRUGS["caffeine"])) == [ids["caffeine"]]
    assert vault.find_exact("name", "ibuprofen") == [ids["ibuprofen"]]
    # Without a registry, the structure itself is a key:
    #   vault.put(id, smiles, keys={"smiles": canonicalize(smiles)})
    #   vault.find_exact("smiles", canonicalize(query))

    # --- read back -----------------------------------------------------------------------------
    payload = vault.get(ids["aspirin"])
    assert payload.smiles == canonicalize(DRUGS["aspirin"]) and payload.keys["name"] == "aspirin"
    payloads = vault.get_many(ids.values())                       # {id: Payload}; unknown and deleted ids absent
    found, rows = vault.fingerprints(ids.values(), family="morgan2")   # (5, 256) uint8, for your own analytics

    # --- edit, delete, errors ------------------------------------------------------------------
    assert vault.put(ids["caffeine"], "Cn1c(=O)c2c(ncn2C)n(C)c1=O") == 2   # same id, row version 2
    vault.delete(ids["ibuprofen"])
    assert vault.get(ids["ibuprofen"]) is None and len(vault) == 4
    try:
        vault.put(uuid.uuid4(), "not a molecule")                # nothing is written
    except StructureError as exc:
        assert isinstance(exc, MolsealError)                      # every library error shares this base

    # --- snapshot and reopen -------------------------------------------------------------------
    vault.save_snapshot()                                         # encrypted under the tenant's data key
    reopened = Vault(store, FileKeyProvider(key_path), tenant,
                     snapshot_dir=root / "snapshots", families=(MORGAN2, FEATMORGAN2))
    assert reopened.substructure("c1ccccc1O") == ring_oxygen      # loaded from the snapshot, then caught up
```

Every exception the library raises derives from `molseal.MolsealError`. The public names are listed
in `molseal.__all__`, and the package ships `py.typed`.

### 7.3 Command-line interface

```text
molseal keygen PATH
molseal key-check                      # wrap and unwrap a throwaway secret through --key
molseal rewrap --to SPEC [--all]       # re-wrap tenant keys under another provider
molseal load FILE                      # one SMILES per line
molseal search QUERY [--kind smiles|smarts|molblock] [--generalized] [--chirality] [--max N]
molseal count QUERY [--kind smiles|smarts|molblock] [--generalized] [--chirality]
molseal similar SMILES [--threshold T] [--top K]
```

The evaluation tools are not part of the CLI. The absence check and the leak report run as
`python -m bench.leak absence-check SIDECAR [--run-pg-dump] [--dump PATH] [--log PATH]` and
`python -m bench.leak leak-report SIDECAR`, taking `--db` and `--tenant` only.

Database commands accept `--db`, `--key`, `--tenant`, and `--snapshot-dir`, or the corresponding
environment variables `MOLSEAL_DATABASE_URL`, `MOLSEAL_KEY`, `MOLSEAL_TENANT`, and
`MOLSEAL_SNAPSHOT_DIR`. `--key` takes a provider specification: a file path (or `file:PATH`),
`aws:KEY_ID`, `gcp:KEY_NAME`, `azure:KEY_URL`, or `infisical:BASE_URL/KEY_ID` with the bearer token in
`MOLSEAL_INFISICAL_TOKEN`. `--kind` names the query language; `--generalized` matches tautomers and
`--chirality` makes the query's stereo mandatory, as the library's `substructure` options do. `rewrap` reads the old provider from `--key` and the new one from `--to`;
`key-check` proves a provider works before a vault is pointed at it. Key generation takes a file path
and refuses to overwrite an existing key.

## 8. Limitations and future work

The current design requires the searchable collection and its configured fingerprints to fit in
application memory. Each process maintains its own index, similarity search scans the fingerprint
matrix, and a single vault lock serializes searches and updates. These properties define the current
scaling constraints.

Future work includes similarity indexing, count-based fingerprints, molfile storage, an asynchronous
API, more concurrent index access, and rotation of the tenant keys themselves.
Cross-process synchronization currently relies on cursor checks rather than a separate invalidation
service. Rollback detection would require an additional trusted source of state.

Application-specific registration rules, chemical standardization, authorization, and analytical
workflows remain responsibilities of the integrating application. Design specifications, milestone
reviews, and proposed extensions are retained under [`docs/`](docs/).

## 9. Development and reproducibility

Install development dependencies and run the test suite with:

```bash
uv sync
uv run pytest
uv run mypy          # strict; the configuration is in pyproject.toml
```

Set `MOLSEAL_TEST_DATABASE_URL` to a PostgreSQL connection URL to include PostgreSQL store tests.
The [benchmark guide](bench/README.md) describes dataset preparation, parity checks, timing, and
report generation. Benchmark slices are generated locally and are not committed to the repository.

Issues and pull requests are welcome. Changes to chemistry or storage behavior should include
validation appropriate to the affected operation and preserve the documented security boundary.

## License and data attribution

The project code is distributed under the [BSD-3-Clause license](LICENSE). Benchmark queries and
results derive from ChEMBL 37 (EMBL-EBI, CC BY-SA 3.0, https://www.ebi.ac.uk/chembl/). Slices are
generated locally and not redistributed; the published query sets under `docs/results/` contain ChEMBL
SMILES and fragments of them and are CC BY-SA 3.0, not BSD-3.
