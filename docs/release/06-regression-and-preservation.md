# 06 - Regression and preservation

> This report records the Phase 3C.1 regression snapshot. Phase 3C.2 adds the successful post-migration browser run and final engine/package checks in [report 02](phase3c2/02-post-migration-browser-acceptance.md) and [report 06](phase3c2/06-engine-package-and-preservation-regression.md).

## Migrated Engine regressions

The 28-test unit suite, fake backend integration, and independent portability suite passed from the canonical repository. The fake backend exercised fresh adapter flow, PLY/metadata rejection, authorization, cache reuse, process failure, timeout, cancellation, interrupted process recovery, and finalization. The synthetic portability workflow exercised init, plan/run/recovery, cache-only package, configuration invalidation, and verification. The test outputs live under ignored `.local/validation/phase3c1/`; summaries are linked in report 03.

For the copied prior RC project, the migrated Engine adopted existing SfM, model, and navigation, ran packaging and full-hash verification, and did not start a fresh backend. The verified output had 292 photos, zero source-image integrity failures, the same accepted model identity, and a 24-file website package. Publication rights stayed `NEEDS_REVIEW`.

## Browser acceptance

The previous Phase 3C browser run is evidence for the same viewer behavior before migration: Chromium 149 / Playwright 1.62.1 passed all 11 cases, including nested viewer loading, guided tour controls, concave boundary and live boundary rejection, portal restrictions, public Debug Fly absence, metadata separation, and clean network/console. Its record is `/Users/homan/repo/chuen-lung/exhibition-202610/analysis/phase3c-skill-release/browser-validation/release-viewer-final/browser-static-results.json`.

A browser run was not repeated in this environment. Playwright was absent from system and bundled Python; downloading it failed because DNS/network access was unavailable. Node REPL kernels failed before execution, and the sandbox denied a local HTTP bind (`Operation not permitted`). The static Engine generation and package checks passed, but those do not replace a post-migration browser test. No elevated bind or sandbox bypass was attempted. Current browser rerun status: `BLOCKED`; prior browser status: `VERIFIED_BY_BROWSER`.

## Historical asset preservation

The current read-only SHA-256 comparison against the saved Phase 3C protection snapshot passed for all six groups:

| Group | Files | Current manifest SHA-256 | Result |
|---|---:|---|---|
| Accepted PLY | 1 | `fc2080614439846bdee920a3cc0af9770952812efd7a97a015016cce4a819134` | PASS |
| Original photo folder | 293 (292 photos plus metadata file) | `6711468bb19518055e86ae653a62bfdc06b571783c36dff6ef548eecebf2dcfb` | PASS |
| COLMAP sparse model | 4 | `7cdcea63bd0057b33f4c5679bab77d1c301f45856b09eda4f8f02019d7284973` | PASS |
| Accepted navigation | 1 | `88f6daa224bf8b65511dace6303943c17a9f15cb459d103427d139bc84398ef4` | PASS |
| Existing public website | 25 | `9e87bcae1cef37fbb6cb7594500b4ca69b657838f499c8d35a2ccbbd6cff078d` | PASS |
| Historical artwork review data | 322 | `1a51959dce06e04fcffd9b278475b8973501faf3448e23c1f7b4cbf701cf4a07` | PASS |

The accepted PLY is 247,738,651 bytes, SHA-256 `bebbec74f033deed6bf4df8811fbd6537947f9e3ba07d684622a79a4f62d9d3e`. Photo counts differ between the folder manifest (includes `.DS_Store`) and the Engine's image inventory (292 images). Historical product source files marked byte-for-byte in the inventory also still match their pre-copy hashes. All checks read historical data only; no old project output was written.

## Gates

The Engine blocks public packaging when required navigation is absent or stale. Rights review is a separate human gate: a locally verified package is not publication approval. The Publish Package contains static viewer/model/navigation/metadata assets. A self-contained Preservation Package with photographs, SfM, review history, and reproduction record remains `NOT_IMPLEMENTED`.
