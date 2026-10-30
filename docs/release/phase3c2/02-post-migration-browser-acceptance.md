# Phase 3C.2 — Post-migration browser acceptance

## Result

VERIFIED_BY_BROWSER — the migrated generated viewer ran in Google Chrome 156.0.8078.73 with Playwright 1.49.1. Chrome's default sandbox remained enabled; WebGL used software SwiftShader flags only. The run used the actual generated viewer and accepted Chuen Lung PLY from the canonical repository's synthetic review project, served under /nested/exhibition/viewer/.

The viewer loaded all 12 required JSON resources successfully, returned HTTP 200 for the 247,738,651-byte PLY, and visibly rendered the GSplat on a WebGL2 canvas. The loaded PLY SHA-256 was bebbec74f033deed6bf4df8811fbd6537947f9e3ba07d684622a79a4f62d9d3e; the parsed Gaussian count was 998,940. The initial and final camera positions differed after free XYZ navigation. Floor and ceiling limits, concave boundaries, room transitions, Guided Tour behavior, and public/private API policy were exercised by the static viewer suite.

| Suite | Result | Evidence |
| --- | --- | --- |
| Generated static viewer | 22/22 cases passed; no console errors, page errors, or failed requests | [browser-static-results.json](../../../.local/validation/phase3c2/browser/navigation-review/review-ui-sandbox-final7/portal-runtime/browser-static-results.json) |
| Navigation Review UI | 21/21 cases passed; expected invalid-input HTTP 400s were asserted as rejections | [browser-review-results.json](../../../.local/validation/phase3c2/browser/navigation-review/review-ui-sandbox-final7/browser-review-results.json) |
| Visible runtime frame | Captured by the static viewer run | [generated-navigation-gsplat.png](../../../.local/validation/phase3c2/browser/navigation-review/review-ui-sandbox-final7/portal-runtime/generated-navigation-gsplat.png) |

The public viewer excludes the Debug Fly controls. No blocking network error, unexpected console error, page error, or 404 remained. Two deliberate HTTP 400 responses in the review UI reject invalid vertical bounds and a malformed portal; these are passing negative tests. The stale-editor save is separately verified as an HTTP 409.

## Test boundary

This is actual Chrome browser evidence for the migrated runtime and review UI, but the navigation polygons and portal fixture were synthetic geometry injected into the real generated PlayCanvas runtime in memory. The PLY itself is the exact accepted historical model. This proves runtime/browser behavior and artifact identity; it does not validate surveyed architecture or a new exhibition. WebGL ran with software rendering, and the test does not measure production GPU performance.
