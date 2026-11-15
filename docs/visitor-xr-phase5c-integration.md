# Phase 5C — WebXR Visitor Main Integration and Acceptance

Date: 2026-11-16

## Verdicts

| Gate | Result | Evidence |
|---|---|---|
| Implementation | IMPLEMENTATION_COMPLETE | Phase 5B source is integrated into canonical main; this phase finalized the documentation and GUI contract. |
| Main integration | MAIN_INTEGRATION_PASS | Conflict-free rehearsal and local merge commit d05e184c7e0770702ee612f8cbc8f49d7a8ecea5. |
| Technical regression | TECHNICAL_REGRESSION_PASS | Focused Python and XR Node tests pass. The full suite has the same two failures on pre-merge main and post-merge main, with one additional passing test after merge. |
| Chuen Lung navigation approval | NAVIGATION_APPROVAL_NEEDS_REVIEW | No human approval was created. The visitor package retains release authorization NEEDS_REVIEW. |
| Publication readiness | NEEDS_REVIEW | The Chuen Lung navigation approval and public release decisions remain outstanding. |
| GUI handoff | GUI_HANDOFF_READY | One optional toggle maps to the existing XR profile; readiness and review status come from existing Engine commands. |

Technical regression PASS means the merge introduced no new failing test; it does not mean the entire Engine suite is green.

## Baseline and merge

Canonical main was clean at 36a4263c8bbd949a2f9e03aec0c0440fb6bb5552. The Phase 5B source branch codex/phase5b-webxr-acceptance was clean at fd553e937ca1049a49d815e37bfe8fce9e1c9690, with only these two commits ahead of main:

- 5bbe799df0f697df63b3279606abc803d07b493c — preserve approved XR enclosure assets.
- fd553e937ca1049a49d815e37bfe8fce9e1c9690 — record Phase 5B acceptance results.

A disposable local clone at /private/tmp/ea-phase5c-rehearsal merged the branch with Git’s ort strategy. It had no conflicts. The same merge was then performed on canonical main, producing d05e184c7e0770702ee612f8cbc8f49d7a8ecea5. The merge preserved both Phase 5B commits. No remote push, deployment, DNS change, canonical Engine change outside this integration, or modification to the Chuen Lung accepted release was made.

The source merge changed these eight files:

- config/project-manifest.schema.json
- docs/visitor-xr-phase5b-acceptance.md
- docs/visitor-xr-phase5b-results.json
- docs/visitor-xr-publication.md
- src/exhibition_archive/publisher.py
- tests/generate_independent_fixture.py
- tests/test_publisher.py
- tests/test_visitor_xr.py

Phase 5C adds this report and its machine-readable companion, and updates the publication guide and README documentation index. No SOG, enclosure, SfM, training, or generated website asset is committed.

## Publication and approval contract

The optional publication field is publication.visitor_xr. An absent field or enabled=false selects the Standard Visitor profile. XR remains part of the same Publisher workflow and does not add a reconstruction or training stage. The accepted profile supports renderer auto, webgl, or webgpu; the generated default is auto.

The optional integrations.navigation.approved_collision_glb input contains a workspace-relative or absolute path and an expected SHA-256. The Publisher validates the hash and GLB 2.0 header, then copies the exact source bytes into the Visitor package. The GLB must already be in the configured SuperSplat Y-up world frame; the Publisher does not transform or regenerate it. If the field is absent, the existing Engine-generated enclosure path remains in use.

Specifying an approved collision GLB does not create a human approval record, promote navigation approval, or approve a public release. Adopted navigation is reported as CONFIGURED_ADOPTED_INPUT. The package release authorization remains NEEDS_REVIEW until the existing review process approves the exact package. Generated navigation still requires the current review-approved artifact and its existing review state. The Chuen Lung approval remains NEEDS_REVIEW.

The existing CLI contract was verified:

- package WORKSPACE --dry-run is read-only and reports XR readiness.
- With visitor_xr omitted, the result is NOT_REQUESTED and exit code is 0.
- An enabled XR config with a missing approved enclosure is BLOCKED with an actionable missing-file reason and exit code 2.
- The focused CLI tests also cover enabled XR readiness, invalid navigation transforms, stale hashes, and generated navigation that is not approved.
- package WORKSPACE continues to use the existing package-only workflow. It does not create a separate XR pipeline.

For future GUI use, the normal publication screen needs only an “Enable immersive VR (WebXR)” toggle mapped to publication.visitor_xr.enabled. Keep renderer auto and current boundary/recenter defaults unless an advanced settings view is justified. The GUI should present package --dry-run’s xr_readiness status and blockers without reproducing Engine validation. It should present verify WORKSPACE --public-release separately, because package readiness and human release authorization are distinct. Build ID and XR profile are in config/exhibition.json; runtime versions, patch identity and resource hashes are in config/build.json; payload hashes are in config/package-index.json.

## Regression results

Commands were run against the pre-merge baseline, the isolated rehearsal, and canonical main after merge. The post-merge full suite was:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/homan/repo/exhibition-archive/.venv/bin/python -m pytest -p no:cacheprovider -q

| Run | Result | Attribution |
|---|---:|---|
| Current canonical main before merge | 268 passed, 2 failed | Baseline |
| Isolated merged rehearsal | 269 passed, 2 failed | Same two baseline failures; one additional passing test |
| Canonical main after merge | 269 passed, 2 failed | Same failures and details as baseline; no XR regression |
| Focused Visitor Publisher and XR Python tests | 18 passed | PASS |
| XR and Viewer patch Node tests | 7 passed | PASS |
| Viewer rebuild | SuperSplat 1.42.0, PlayCanvas 2.23.3; XR bundle SHA-256 94d9a4c4ad9612385708c21602d5a7f481d352468667dee6d3335beb6f4478ac | PASS; matches the accepted patch output |

The two full-suite failures reproduce unchanged on main:

1. test_accepted_versioned_alignment_is_handed_to_navigation_as_a_proposal — the fixture omits source.photo_directory, causing KeyError: source before navigation proposal construction.
2. test_reviewer_and_visitor_preview_reference_real_fixture_assets — the test expects UNVERIFIED while current code reports NOT_CONFIGURED for absent navigation configuration.

Neither test or its exercised production path was changed by the XR integration.

Commands for focused validation and Viewer rebuild:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/homan/repo/exhibition-archive/.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_visitor_xr.py tests/test_publisher.py
    /Users/homan/.nvm/versions/node/v26.12.1/bin/node --test tests/visitor_xr.test.mjs tests/visitor_xr_patch.test.mjs
    PATH=/Users/homan/.nvm/versions/node/v26.12.1/bin:$PATH npm run build:visitor-viewer

The Viewer build reproduced the accepted patched bundle hash. The standard bundle SHA-256 was 408e3cc3cce59d4c438b23b6a27f8e47c385cc6b68c28ee1753af67da0d34bec.

## Generated publication packages

Both packages were generated through the canonical Publisher’s build_website path in a disposable copy of the Chuen Lung full-workflow fixture. The registered SOG and approved enclosure were read-only inputs. Outputs are outside the repository:

- Standard Visitor: /private/tmp/ea-phase5c-standard-visitor/
- XR Visitor: /private/tmp/ea-phase5c-xr-visitor/
- Temporary fixture workspace: /private/tmp/ea-phase5c-chuen/

| Check | Standard | XR |
|---|---|---|
| XR enabled | false | true |
| XR modules and build manifest | omitted | included |
| Viewer | Stock 1.42.0 / PlayCanvas 2.23.3 | Patched 1.42.0 / PlayCanvas 2.23.3 |
| Viewer bundle SHA-256 | 408e3cc3cce59d4c438b23b6a27f8e47c385cc6b68c28ee1753af67da0d34bec | 94d9a4c4ad9612385708c21602d5a7f481d352468667dee6d3335beb6f4478ac |
| Indexed payload files | 16 | 22 |
| Package index and relative references | PASS | PASS |
| SOG SHA-256 | 462a32b31a2cedae912cdd037baf124cae46aa08b6ae91a7e0c12d337ed6b919 | 462a32b31a2cedae912cdd037baf124cae46aa08b6ae91a7e0c12d337ed6b919 |
| Enclosure GLB SHA-256 | 7779ad3abc8832b4c2b46a2a04fdbe3d1dbfb2c0fcf57f39530f95b51d4e323f | 7779ad3abc8832b4c2b46a2a04fdbe3d1dbfb2c0fcf57f39530f95b51d4e323f |
| Release authorization | NEEDS_REVIEW | NEEDS_REVIEW |
| Navigation status / approval gate | ADOPTED_VALIDATED / CONFIGURED_ADOPTED_INPUT | ADOPTED_VALIDATED / CONFIGURED_ADOPTED_INPUT |

The generated XR website is byte-for-byte identical across all 23 files, including its package index, to the accepted Phase 5B Engine-generated package at /Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/phase5b/chuen-full-publisher-accepted-collision/website-visitor/. Its build ID is ea-xr-998469a6a0c38d36af46891d. This exact equality is why the user-reported 6/6 visionOS Simulator UX acceptance applies to this generated XR output. No additional Simulator cycle or trace is claimed here.

The generated Standard package differs from the earlier Phase 5B standard sample in its enclosure asset and resulting README and metadata because this run explicitly supplied the accepted collision GLB. The Viewer, Visitor UI, and XR-disabled behavior remain stock. The new standard output has no XR modules or XR build manifest.

## Desktop browser and Simulator evidence

A local headless Chrome smoke loaded both generated packages over loopback. Each returned HTTP 200, rendered one canvas, hid the loading card, showed Reset, and allowed Help to collapse/reopen and language selection to switch English and Traditional Chinese. Chrome reported immersive-vr unsupported and the Enter VR control stayed hidden. The XR package diagnostics reported buildMatches and hashesMatch true, 64 rendered frame-end events during sampling, WebGPU graphics device, one enabled SOG entity with 998,940 splats, an unchanged scene transform invariant, zero page errors, zero console errors, and zero failed HTTP responses.

The user reports all six visionOS 27.2 Simulator UX checks passed on the Phase 5B Engine-generated package: normal XR entry; progressive approach feedback; out-of-bounds safety view; Recenter selection; automatic restoration on return inside; and XR exit, desktop reset, and XR re-entry. Because the regenerated package is byte-identical to that package, the same user-reported acceptance applies. No new Simulator interaction was performed in this Phase 5C tool run; the desktop CUA bridge did not initialize. This is not physical Vision Pro testing.

The prior Phase 5B Chrome and Safari package smokes remain recorded in the Phase 5B report. This phase freshly exercised the generated Standard and XR websites in headless Chrome. A new Safari run was not performed.

| Acceptance area | Result | Scope |
|---|---|---|
| Standard Visitor output | PASS | Generated package, stock Viewer, no XR modules, package hashes and references pass |
| XR Visitor output | PASS | Generated package is byte-identical to the user-tested Phase 5B package |
| Chrome desktop smoke | PASS | Local headless Chrome; SOG render, Reset, Help, localization, unsupported-device fallback, no browser/runtime/network errors |
| visionOS Simulator UX | PASS, user-reported | Six checks on the exact same package bytes; not re-run during this phase |
| Safari in this phase | NOT_TESTED | Historical Phase 5B Safari smoke is documented; no fresh Safari run |
| Physical Vision Pro | NOT_TESTED | Comfort, actual eye/hand tracking, physical scale, walking behavior, FPS and thermal results unavailable |
| Live artificial XR locomotion and teleport | NOT_TESTED | The previous unit/wrapper guards pass; no user-reachable live movement acceptance is claimed |
| Chuen Lung navigation human approval | NEEDS_REVIEW | No approval decision was recorded or inferred by this integration |

The accepted enclosure comparison from Phase 5B classified the 68-triangle reference and 64-triangle generated shell as GEOMETRICALLY_EQUIVALENT; the difference was cap triangulation. This phase reused the exact accepted 68-triangle GLB without regeneration. The previously recorded desktop boundary test used the Engine-generated shell; live artificial XR locomotion and teleport across this exact adopted GLB remain unverified.

## Reproduction and local review

To serve the generated XR package locally:

    python3 -m http.server 8000 --bind 127.0.0.1 --directory /private/tmp/ea-phase5c-xr-visitor

Open http://127.0.0.1:8000/ in a local browser or visionOS Simulator Safari. This only serves the local package; it does not deploy it. For Simulator verification, test entry and spawn, approach and outside feedback, Recenter, return inside, system exit, Reset, and re-entry. Keep one Simulator Safari tab open.

The existing Phase 5B result report retains the test state as it was recorded at that time. The later user-reported Simulator completion is recorded here rather than backfilling unobserved raw logs or cycles.

No public deployment or remote Git push was performed.
