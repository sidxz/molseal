# 500k parity run for the search surface (2026-10-14)

Code: commit 452b997 (gates added in 558c19a, with the two fixes 82fe958 and 452b997 described below).
Data: the milestone-2 loads of the same 500k ChEMBL 37 slice on both sides (`docs/results/2026-10-13-500k/`),
reused; nothing was reloaded. The cartridge table gained an `fmfp` column (feature-Morgan radius 2, 2048
bits) for gate 5. No timing legs were run.

| gate | what | queries | result |
|---|---|---|---|
| 1–4 | as in the milestone-2 run | 72 + 50 | ok (gate 1: 0 of 100 mismatches; gate 3: 45 id sets compared, 27 skipped for > 10k hits) |
| 5 | feature-Morgan Tanimoto at 0.55 and 0.7 vs `featmorganbv_fp` | 50 × 2 | ok (max score difference 0.0) |
| 6 | Tversky(1, 0) and Dice on Morgan vs `tversky_sml` / `dice_sml` | 50 × 2 | ok (max score difference 0.0) |
| 7 | generalized substructure vs `@>> mol_to_xqmol` | 50 | ok (counts on all 50; id sets on 45, 5 skipped for > 10k hits) |
| 8 | chirality vs `rdkit.do_chiral_sss` | 39 | ok (23 queries with at least one chirality-on hit) |
| 9 | molblock queries equal their SMILES form | 50 | ok |
| 10 | chiral Morgan and Cosine vs RDKit in Python; fingerprint rows for 100 sampled ids | 50 × 3 + 3 | ok (max score difference 0.0; 0 row mismatches in all three families) |

Wall time: 14.2 minutes (854 s, 11:14:53 to 11:29:07), of which gate 7 about 7.5 minutes and the one-time
`fmfp` refill about 2 minutes. The rebuild was measured on the first run of this code, which found the
stored snapshot in format 1 and rebuilt the index with three fingerprint families from the database: at
most 70 s (process start to the first gate-2 query). That run saved the format-2 snapshot this run opened.
Peak RSS of the parity process: 4.0 GB (`/usr/bin/time -l`).

- Gate 5 pins the cartridge's feature-Morgan size: `rdkit.featmorgan_fp_size` is a session setting that
  defaults to 512 bits, independent of `morgan_fp_size`. The parity connection sets it to 2048 and checks
  `size(featmorganbv_fp(...)) = 2048` before refilling `fmfp`. The first run of this code compared 512 bits
  with 2048 and failed gate 5.
- Gate 8 exercises the E/Z post-filter: the engine's trusted SMILES holder never assigns bond stereo, so a
  chirality-on query with a defined double bond is searched with chirality off and each hit re-checked on a
  fully parsed molecule. Before that fix, the 4 chiral queries with E/Z stereo returned no hits, including
  the query's own molecule, and gate 8 failed.

Query sets are CC BY-SA 3.0 derived from ChEMBL 37 (see the data attribution in the root README).
