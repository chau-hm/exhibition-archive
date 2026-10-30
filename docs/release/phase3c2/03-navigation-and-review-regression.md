# Phase 3C.2 — Navigation and review regression

## Result

VERIFIED_BY_BROWSER — the final navigation/review browser matrix passed all 21 cases. The suite exercised dashboard loading, draft editing and persistence, boundary dragging, floor/ceiling validation, stale-session rejection, proposal reload, portal endpoint editing, invalid portal rejection, valid approval, local preview, and the approved portal transition in the static runtime.

The final browser evidence is [browser-review-results.json](../../../.local/validation/phase3c2/browser/navigation-review/review-ui-sandbox-final7/browser-review-results.json). The generated static runtime's related multi-room and tour checks are in [browser-static-results.json](../../../.local/validation/phase3c2/browser/navigation-review/review-ui-sandbox-final7/portal-runtime/browser-static-results.json).

Specific safety results:

- Moving a boundary vertex works even when provisional camera markers overlap it; marker CSS no longer captures the pointer.
- An editor with a stale navigation version receives HTTP 409 when it attempts to save.
- Out-of-range vertical geometry and malformed portal proposals are rejected with HTTP 400.
- Invalid proposals do not receive an approval record. A valid portal approval binds to the changed artifact hash, produces a local preview, and works in the static viewer.
- Later edits invalidate the prior approval, and the test confirms the proposal must be reviewed again.
- The dashboard no longer renders a dead __REVIEW_URL__ iframe when no artwork review server is configured. It shows an explicit unavailable message. The final dashboard has no unexpected console error.
- An inline empty favicon prevents Chrome from requesting a nonexistent optional favicon.

The source changes are small and inspectable in [navigation.py](../../../src/exhibition_archive/navigation.py), [review_hub.py](../../../src/exhibition_archive/review_hub.py), and [dashboard/index.html](../../../dashboard/index.html).

## Test boundary

The dashboard/review APIs and generated HTML were exercised locally in a synthetic project. Invalid-input HTTP 400s are intentional responses that the test expects; the suite records them as PASS. The stale-session HTTP 409 is also expected. Synthetic boundaries and portals do not establish real building geometry.
