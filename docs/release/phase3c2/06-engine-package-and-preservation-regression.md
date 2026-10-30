# Phase 3C.2 — Engine, package, and preservation regression

## Engine verification

- **Unit suite:** 28 tests passed. See [unit-tests-final4.txt](../../../.local/validation/phase3c2/engine-regression/unit-tests-final4.txt).
- **Fake-backend integration:** PASS, 31 checks, using synthetic data and fake SfM/training adapters. No real COLMAP, GPU, Chuen Lung input, or photo decoding. See [fresh-backend-test-results.json](../../../.local/validation/phase3c2/engine-regression/fresh-backend/run-20261029T162640Z/fresh-backend-test-results.json).
- **Portability:** PASS in an independent synthetic workspace, including a package-only/cache path with zero backend processes, recovery, and static package verification. This is software portability, not a second exhibition. See [portability-test-results.json](../../../.local/validation/phase3c2/engine-regression/portability/run-20261029T162640Z/portability-test-results.json).
- **Package integrity:** PASS, full-hash requested, 40/40 checks and zero integrity failures. Accepted model identity matches. The public-release gate separately remains NEEDS_REVIEW. See [full-hash-rc-final4.json](../../../.local/validation/phase3c2/package-verification/full-hash-rc-final4.json).

## Protected historical assets

ADOPTED_AND_VALIDATED — all six protected groups match their saved manifests, with no changed or unlisted files:

| Group | Protected files | Result |
| --- | ---: | --- |
| Accepted PLY | 1 | unchanged |
| Chuen Lung source image directory | 293 files (292 photos plus one protected non-photo file) | unchanged |
| COLMAP sparse model | 4 | unchanged |
| Accepted Phase 2ER navigation | 1 | unchanged |
| Historical public website | 25 | unchanged |
| Artwork review data | 322 | unchanged |

The read-only comparison is [chuen-lung-current-comparison.json](../../../.local/validation/phase3c2/preservation/chuen-lung-current-comparison.json). The browser loaded the accepted PLY with 998,940 Gaussians, 247,738,651 bytes, SHA-256 bebbec74f033deed6bf4df8811fbd6537947f9e3ba07d684622a79a4f62d9d3e.

The historical repository was not modified. Detailed generated evidence remains under the ignored .local/validation/phase3c2/ tree; no private fixture, PLY, checkpoint, or browser output was added to the product package.

## Limits

These results verify Engine behavior, software portability, artifact integrity, and preservation manifests. They do not rate a fresh real model, validate a new venue's architecture, provide a self-contained Preservation Package, or grant public-release approval.
