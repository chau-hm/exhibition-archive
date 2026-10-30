# Phase 3C.2 — Release acceptance and pilot readiness

## Decision

RELEASE_CANDIDATE_READY_FOR_SUPERVISED_REAL_EXHIBITION_PILOT

The eight stated criteria are met: canonical Engine/Skill integration, indirect Skill evidence, Chrome browser acceptance, Engine/artifact regression, tested authorization gates, preserved review/publication controls, unchanged protected assets, and the Phase 3D runbook. This decision means readiness to begin a supervised pilot when an independent dataset and its operating policies are available. It does not indicate public-release approval, complete venue automation, or a completed independent exhibition.

Current repository HEAD remains 224eb46c93a8a55b7c985d242e96d8e4a46192cf on main; the acceptance fixes and reports are uncommitted. Product version remains 0.1.0. No tag, push, publication approval, or deployment was made.

## Explicit handoff answers

1. **Current canonical HEAD?** 224eb46c93a8a55b7c985d242e96d8e4a46192cf on main. **VERIFIED_BY_EXECUTION** — [baseline record](../../../.local/validation/phase3c2/baseline/repository-baseline.json).
2. **Existing user changes present?** No. The initial tree was clean. The current four-file source diff and new Phase 3C.2 docs are this acceptance work. **VERIFIED_BY_EXECUTION**.
3. **Did the migrated product install and run correctly?** Yes. Python 3.13.5 editable install, CLI 0.1.0, canonical import path and external-workspace CLI operation were verified. **VERIFIED_BY_EXECUTION** — [installation report](../03-installation-and-cli-verification.md).
4. **Did actual browser testing run from the new repository?** Yes; generated runtime and review UI were served from the canonical repository's synthetic project. **VERIFIED_BY_BROWSER** — [report 02](02-post-migration-browser-acceptance.md).
5. **Browser and Playwright versions?** Google Chrome 156.0.8078.73 and Playwright 1.49.1. Chrome's normal sandbox remained enabled; WebGL used SwiftShader. **VERIFIED_BY_BROWSER**.
6. **Did the accepted PLY visibly render?** Yes. Chrome displayed the 998,940-Gaussian PLY; byte count and SHA-256 matched the accepted artifact. **VERIFIED_BY_BROWSER**.
7. **Did Free XYZ movement work?** Yes; browser interaction changed the camera position freely in XYZ. **VERIFIED_BY_BROWSER** — [static viewer results](../../../.local/validation/phase3c2/browser/navigation-review/review-ui-sandbox-final7/portal-runtime/browser-static-results.json).
8. **Were floor/ceiling and concave boundaries enforced?** Yes in the synthetic geometry browser matrix, including invalid boundary/vertical moves. This does not infer architecture from real images. **VERIFIED_BY_BROWSER**.
9. **Did multi-room portal behavior pass?** Yes: invalid portal rejection, approved transition, later edit invalidation, local preview, and static runtime transition passed. **VERIFIED_BY_BROWSER** — [report 03](03-navigation-and-review-regression.md).
10. **Did Guided Tour UI interaction pass?** Yes; controls and live boundary checks passed in the static viewer suite. **VERIFIED_BY_BROWSER**.
11. **Did Navigation Review editing and approval pass?** Yes, 21/21 cases passed, including vertex dragging, saved/reloaded edits, auditable approval, HTTP 409 stale-editor rejection, HTTP 400 invalid-geometry/portal rejection, and portal approval. **VERIFIED_BY_BROWSER** — [review results](../../../.local/validation/phase3c2/browser/navigation-review/review-ui-sandbox-final7/browser-review-results.json).
12. **Unexpected browser errors?** None in the final static suite: no console errors, page errors, failed requests, or 404s. Two intentional review API HTTP 400 rejections were asserted as PASS. **VERIFIED_BY_BROWSER**.
13. **Did the public viewer exclude Debug Fly?** Yes; the browser suite verified that public control is absent. **VERIFIED_BY_BROWSER**.
14. **Was the canonical Skill selected for indirect requests?** Yes, in 3/3 relevant positive tests. The prior direct canonical invocation remains accepted. **VERIFIED_BY_SKILL_INVOCATION** — [report 04](04-skill-functional-evaluation.md).
15. **Did negative-trigger tests avoid unintended selection?** Yes, 0/5 unrelated prompts selected the Exhibition Archive Skill; relevant PDF/image skills were used instead. **VERIFIED_BY_SKILL_INVOCATION**.
16. **Did Skill-triggered CLI delegation occur?** Yes. The fresh positive sessions read the canonical Skill and invoked plan, status, initialization/safe run, or verification commands as appropriate. **VERIFIED_BY_SKILL_INVOCATION**.
17. **Did the Skill interpret SQLite state accurately?** Yes. It reported durable Engine stage state and current plan readiness from status/plan. **VERIFIED_BY_SKILL_INVOCATION**.
18. **Did it explain recorded state versus plan readiness differences?** Yes; it retained COMPLETED/ADOPTED alongside BLOCKED_NO_CONFIGURED_ARTIFACT and explained why both can be true. This clarification is now in the Skill. **VERIFIED_BY_SKILL_INVOCATION**, **IMPLEMENTED_AND_TESTED**.
19. **Did ambiguous continuation avoid fresh SfM/training?** Yes. With no active workspace it asked for the path; safe intake without authorization stopped at the missing backend. **VERIFIED_BY_SKILL_INVOCATION**.
20. **Were grants and stale-plan behavior tested?** Yes in unit/fake-backend coverage: one-use, stage-scoped grants, changed-plan rejection, consumed-grant handling, and the separate training plan were exercised. No real backend ran. **VERIFIED_BY_EXECUTION** — [report 05](05-authorization-and-publication-safety.md).
21. **Were publication rights and navigation gates preserved?** Yes. Navigation and rights decisions remain bound to current artifacts; the package integrity passes while the publication gate stays NEEDS_REVIEW. **IMPLEMENTED_AND_TESTED**, **NEEDS_REVIEW**.
22. **Did the complete Python unit suite pass?** Yes, 28/28. **VERIFIED_BY_EXECUTION**.
23. **Did fake-backend integration pass?** Yes, the suite returned PASS; 29 checks passed, source bytes were unchanged, and optimizer-state resume is explicitly NOT_TESTED. **VERIFIED_BY_EXECUTION**.
24. **Did external-workspace portability pass?** Yes, against an independent synthetic workspace, including package-only operation with zero backend processes. It is not a second exhibition. **VERIFIED_BY_EXECUTION**.
25. **Did full-hash verification pass?** Yes, 40/40 checks, zero integrity failures, matching accepted model identity. **VERIFIED_BY_EXECUTION** — [full-hash record](../../../.local/validation/phase3c2/package-verification/full-hash-rc-final4.json).
26. **Were all six protected Chuen Lung groups unchanged?** Yes; every saved manifest matches and there are no changed/unlisted files. **ADOPTED_AND_VALIDATED** — [preservation comparison](../../../.local/validation/phase3c2/preservation/chuen-lung-current-comparison.json).
27. **Were product-source changes required?** Yes, four focused files: camera-marker pointer handling, review dashboard's unconfigured-artwork state and favicon, and one Skill state-interpretation sentence. **IMPLEMENTED_AND_TESTED** — [report 01](01-baseline-and-remaining-gaps.md).
28. **Are failures environmental blockers or product defects?** No unresolved regression remains. The earlier browser environment limitation was superseded by the successful sandbox-enabled Chrome run. Three browser defects were fixed; public-release human decisions and the missing independent dataset are planned gates, not test failures. **VERIFIED_BY_BROWSER**.
29. **Is the current Skill suitable for supervised day-to-day use?** Yes, with an operator-supplied stable project ID, review of exact plans, and explicit human decisions. One first-use session inferred an ID instead of asking; a separate missing-ID evaluation did ask, and no backend/release approval resulted. **VERIFIED_BY_SKILL_INVOCATION**.
30. **Is the Engine RC ready for an independent real-exhibition pilot?** Yes, supervised only; the real dataset has not yet been processed. **RELEASE_CANDIDATE_READY_FOR_SUPERVISED_REAL_EXHIBITION_PILOT**.
31. **What remains incomplete in the Publish Package?** The local static candidate passes integrity but is not approved for public release. Model selection, navigation, artwork metadata, image/artwork rights, access policy, and public/private policy must be reviewed and bound to the exact candidate. No deployment occurred. **NEEDS_REVIEW**.
32. **What remains unimplemented in the Preservation Package?** The product does not create a complete self-contained archival bundle of source media, reconstruction/training history, checkpoints, and preservation metadata. It currently creates a deployable static Publish Package. **NOT_IMPLEMENTED**.
33. **What human reviews are required during the pilot?** Capture sufficiency; candidate model/fidelity; navigation geometry and portals; artwork identity/association and metadata; image and artwork rights; access/public/private policy; and approval of the exact package. Architecture proposals may require geometry correction. **NEEDS_REVIEW**.
34. **Recommended next operational step?** Obtain one independent exhibition's read-only photo folder, a stable ID, an external workspace, and its access/rights policy; run safe intake; then present exact full-hash backend plans for separate approval before any fresh SfM or training. Follow [the Phase 3D runbook](08-phase3d-pilot-runbook.md). **NOT_TESTED** for the future exhibition itself.

## Limits on this decision

The browser navigation fixture was synthetic. No real SfM/training, new exhibition processing, independent architectural assessment, public approval, or deployment occurred. See the [pilot runbook](08-phase3d-pilot-runbook.md) for the next bounded operation and measurements.
