# Phase 3C.2 — Authorization and publication safety

## Costly-operation gates

IMPLEMENTED_AND_TESTED — the fresh fake-backend suite returned PASS with 31 check records: 29 PASS, source bytes unchanged, and optimizer-state resume explicitly NOT_TESTED. It exercised fresh SfM and training through fake adapters, exact stage plans, explicit one-use grants, stale/changed-plan rejection, retry boundaries, timeouts, cancellation, interrupted-process reconciliation, failure finalization, cache reuse, and output validation. Workflow retry is not claimed to restore optimizer state.

No real SfM, COLMAP reconstruction, GPU/Gaussian training, or real exhibition processing ran in Phase 3C.2. The Skill sessions likewise did not issue an authorization or start a backend. The bounded suite is [fresh-backend-test-results.json](../../../.local/validation/phase3c2/engine-regression/fresh-backend/run-20261029T162640Z/fresh-backend-test-results.json).

The authorization contract remains: inspect a full-hash current plan; obtain explicit approval for the named stage and exact plan; record reviewer and rationale; then use the returned one-use grant. Grants are stage-specific, expire after 15 minutes, and are invalidated when inputs/configuration/backend/resources change. A newly completed SfM changes the training plan, requiring a separate review and grant. See the canonical [workflow reference](../../../skills/exhibition-archive/references/workflow.md).

## Package and publication gates

The final local candidate passed full-hash integrity verification: 40/40 checks, zero integrity failures, accepted model identity match, 25 synthetic source images checked, and package index SHA-256 8f171fa107163c8652b31afa3234134eaae102b6609e369fd00361d7c81bc24c. Evidence: [full-hash-rc-final4.json](../../../.local/validation/phase3c2/package-verification/full-hash-rc-final4.json).

Public release remains NEEDS_REVIEW and public_release_ready is false. No publication decision is recorded for the candidate. The required human decision groups are model selection, navigation approval, artwork metadata accuracy, image/artwork publication rights, and public/private data policy. The granular command interface separately records image rights, artwork rights, access policy, and public/private policy. Package integrity is not permission to publish or deploy.

The public-release gate command intentionally exits nonzero while approval is missing. This is the expected gate behavior, not an integrity regression. Detailed output is [public-release-rc-final4.txt](../../../.local/validation/phase3c2/package-verification/public-release-rc-final4.txt).
