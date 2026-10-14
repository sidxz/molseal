# Parked: similarity bit-count band index

**Status:** parked 2026-10-14 (user). Similarity is a full scan: 22 ms at 500k on one thread, growing
linearly. Acceptable for now.

**Design when picked up:** per family, a matrix kept sorted by bit count with an unsorted tail for new
rows, merged at a size threshold or at snapshot save. A threshold query scans only the contiguous range
of bit counts that can pass the metric's bound; a top-k query walks outward from the query's own count
and stops when no remaining band can beat the k-th score. Exact results, same metric functions.
Expected at 500k: threshold search 22 ms → about 8–10 ms, top-k from 26 ms → a few ms. Cost: a 4-byte
position map per molecule per family and the merge logic.

**Also parked from the same decision:** count-based fingerprints; the 1M and 2.4M rungs; the five bench
harness items from the milestone-2 ledger (statement-log positive control, `run_m2.sh` step-3 header
note, gate through a function instead of CLI stdout, GIT_SHA build argument, `_pack` profiling). For the next 500k run: add a leg that opens the snapshot with one family missing and
compares the recomputed rows to gate 10's Python reference, since the recompute-on-load path has only unit
coverage; and add a handful of E/Z fragment queries to gate 8, whose current chiral queries are whole
molecules that match essentially themselves.
