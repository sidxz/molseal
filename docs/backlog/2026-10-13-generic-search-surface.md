# Generic search surface: fingerprint families, metrics, query kinds, bulk reads

**Status:** implemented in milestone 3; spec at docs/superpowers/specs/2026-10-14-generic-search-surface-design.md. The similarity band index is parked separately.

## Why

A consumer audit (a compound-registry application) showed that real deployments need more than
Morgan/Tanimoto substructure+similarity: feature-class (FCFP) fingerprints for scaffold hopping,
asymmetric Tversky for fragment-in-target ranking, chiral Morgan, tautomer-insensitive
("generalized") substructure, and bulk access to decrypted structures and fingerprints for
analyses the caller runs itself (scaffold networks, R-group decomposition, MCS, clustering,
diverse picking). Every one of these must be exposed generically. The library never learns a
consumer's mode names, thresholds or column names; the consumer maps its own vocabulary onto
these primitives.

## Design (keep it small)

1. **Fingerprint families are data, not code paths.** The index holds N packed bit matrices keyed
   by a caller-registered spec: `name`, `bits`, and an RDKit generator factory (e.g. Morgan r2
   2048 achiral; Morgan r2 2048 chiral; feature-Morgan r2 2048). Default = today's Morgan r2 2048.
   Each family costs `bits/8` bytes per molecule. Because the library already holds canonical
   SMILES, a family registered after load is computed in one pass from memory with no DB read.
   Snapshot format stores each matrix under its spec name; a missing family on load is recomputed.
2. **Metrics are pure functions of three counts.** The scan already yields `|A∩B|`, `|A|`, `|B|`.
   Expose Tanimoto, Tversky(alpha, beta), Dice and Cosine over those counts. One scan path.
3. **Substructure query options, not modes.** `substructure(query, kind="smiles"|"smarts"|"molblock",
   generalized=False, use_chirality=False, max_results=None)`. `generalized=True` builds RDKit's
   extended query mol (tautomer-insensitive, adjusted query properties) and passes it to the
   substructure library. Verify by test that the library accepts extended query mols.
4. **Bulk reads for caller-side analytics.** `get_many(ids) -> dict[id, Payload]` (one store batch,
   decrypt in Python) and `fingerprints(ids, family) -> ndarray` straight from the matrix. The
   library does **not** implement scaffold trees, MCS, R-group decomposition, clustering or
   picking; it hands out decrypted structures and bits, and the caller's RDKit code does the rest.
5. **Exact keys stay caller-defined.** InChIKey, Murcko scaffold, formula or anything else is just a
   `(kind, value)` HMAC the caller chooses to register. Scaffold-exact search is therefore already
   covered without new engine code.
6. **Parity.** Each new family and metric gets a parity gate in `bench/` against the cartridge's
   equivalent function (feature-Morgan, Tversky) before it is called done.

## Non-goals

No plugin registry, no config files, no consumer-specific presets ("scaffold hop", "similar") in
the library. Those are three lines in the consumer's adapter.

## Also noted from the same audit (consumer side, not library work)

Structure-derived plaintext that a consumer must move inside the payload or turn into HMAC keys:
InChI, InChIKey, formula, Murcko scaffold SMILES, stored fingerprint bytes, cached scaffold-tree
results, R-group tables, exports and depictions on disk. Counts and numeric descriptors are a
per-consumer policy decision.
