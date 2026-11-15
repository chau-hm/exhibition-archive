# Phase 5B — WebXR Visitor Acceptance

- Date: 2026-11-15
- Phase 5B branch: `codex/phase5b-webxr-acceptance`
- Phase 5A commit: `0614d4d4b16e7ee8570f78178f7e4901e2805283`
- Phase 5B implementation commit: `5bbe799df0f697df63b3279606abc803d07b493c`
- Canonical `main` at baseline: `36a4263c8bbd949a2f9e03aec0c0440fb6bb5552`

## Decision

| Area | Status | Finding |
|---|---|---|
| Implementation complete | PASS | XR preflight now checks the opening camera and scene transform; an explicitly approved collision GLB can be preserved byte-for-byte. |
| Enclosure parity | PASS — `GEOMETRICALLY_EQUIVALENT` | The 64-triangle Engine mesh and 68-triangle accepted GLB have identical boundary surfaces, footprint, bounds, floor, ceiling, volume, and surface area. The cap triangulation differs. |
| Technical acceptance | NEEDS_REVIEW | Python and Node tests pass except for two unchanged baseline failures. Desktop browser checks pass. Engine-generated immersive XR was not exercised in visionOS Simulator during this phase. |
| Navigation approval | NEEDS_REVIEW | The Chuen Lung project config is `ADOPTED_VALIDATED` / `CONFIGURED_ADOPTED_INPUT`; a Phase 5B human navigation approval record is absent. Preserve the accepted GLB for republication and retain the existing review gate for approval. |
| Merge readiness | NEEDS_REVIEW | The branch is clean and based directly on current `main`, with no merge conflict identified. Hold the controlled merge until Simulator acceptance and navigation review are complete. No merge was performed. |
| Publication readiness | NEEDS_REVIEW | No public deployment was attempted. Release authorization remains `NEEDS_REVIEW`. |

The previous 6/6 visionOS Simulator result applies to the accepted Phase 4D.2 reference build. It is not carried over to the Phase 5A or Phase 5B Engine-generated package.

## Baseline and scope

The Phase 5A commit is already an ancestor of canonical `main`. The Phase 5B worktree was based on current `main` commit `36a4263`; canonical `main` remained clean. The separate `codex/phase5a-webxr-visitor` ref remains at `0614d4d`.

The original Phase 5A generated website remains at:

`/Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/chuen-full-workflow/output/website-visitor/`

Its collision GLB remains the Engine-generated 64-triangle asset (`6990254e…e024`). The accepted release-candidate GLB was not modified. The Phase 5B exact-input package was generated separately at:

`/Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/phase5b/chuen-full-publisher-accepted-collision/website-visitor/`

It uses build ID `ea-xr-998469a6a0c38d36af46891d`. Its package index and all payload-file hashes match the separately smoke-tested Visitor output byte-for-byte. The output has 22 indexed files, 14 build-manifest resource hashes, no missing references, and no development-only dependency. Its SOG and collision hashes are the accepted hashes.

## Enclosure provenance and comparison

The accepted asset is the Phase 4D.2 release-candidate collision GLB at [the accepted package](</Users/homan/repo/chuen-lung/exhibition-202610/derived/phase4d2/output/release-candidate/artifacts/chuen-lung-visitor-xr-rc.1/site/assets/enclosure-collision.glb>). Its SHA-256 is `7779ad3abc8832b4c2b46a2a04fdbe3d1dbfb2c0fcf57f39530f95b51d4e323f`; it has 36 vertices and 68 triangles. Its provenance identifies the accepted camera-derived horizontal boundary, floor, and ceiling, in SuperSplat Y-up world coordinates. The SOG-only scene transform is translation `[1.1624822777151316, -1.941300745145962, -0.48021530709598637]`, rotation `[-0.6990436511345212, -0.10990438996930395, -0.11049752634118011, 0.697889171392187]`, and uniform scale `6.640819914527`. The transform is applied to the SOG once; the GLB is already in Viewer world coordinates.

The Phase 5A mesh is generated from the configured navigation envelope `fc173be2306ccb05fc757c3be8d5df0910442425418c7ecbad5e6adb637a9a2f`, candidate `convex-hull-m0.350`, using 233 trusted camera stations. Both meshes are watertight, winding-consistent, one connected component, and enclose a valid volume. Quantitative comparison found:

- Bounds match exactly: `[-4.83883762,-2.06009388,-5.25275373]` to `[5.29628992,4.02990627,7.39714575]`.
- Footprint intersection-over-union is `1.0`; symmetric difference is `0`; boundary Hausdorff distance is `0`.
- Footprint area is `87.9561057924`; volume is `535.6526976968`; surface area is `391.7994842854` for both, within the comparison tolerances of `1e-5` area/volume units and `1e-6` position units.
- Both have the same 17-point perimeter and 34 side triangles. The accepted mesh has one extra interior vertex on each cap, yielding 17 cap triangles per layer versus 15 in the generated mesh. These points are inside the outline and do not change the cap surface.
- All 233 trusted stations are inside both footprints and within the floor/ceiling limits. Minimum source-outline wall clearance is `0.349888`; minimum generated collision wall clearance is `0.469888`.
- Opening station `station-046` is inside both: wall clearance `0.845159`, floor clearance `2.309318`, and ceiling clearance `3.380682`.

The full machine comparison, with raw bounds and mesh properties, is in [enclosure-comparison.json](</Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/phase5b/enclosure-comparison.json>). Verdict: `GEOMETRICALLY_EQUIVALENT`. This is geometric equivalence, not a formal navigation approval.

## Publisher correction

The previous Publisher always regenerated `assets/enclosure-collision.glb` from navigation JSON. That could replace a reviewed mesh’s bytes on republication even when its surface was equivalent. Phase 5B adds optional `integrations.navigation.approved_collision_glb` with a path and required SHA-256. The Publisher validates the digest and GLB 2.0 header, copies the bytes unchanged, records the approved-input source and actual vertex/triangle counts, and emits `APPROVED_COLLISION_ENVELOPE`. The runtime already accepts that enforcement mode. When omitted, the Engine-generated path remains unchanged. The source mesh must already be aligned to the configured SuperSplat Y-up world frame; the Publisher does not transform it.

XR preflight also resolves the configured navigation envelope, trusted stations, and transform, then validates the configured opening camera and collision generation inputs. Missing transforms and invalid scale now block preflight. A generated-navigation approval artifact must be review-approved, public-ready, and include its input fingerprint and runtime policy. A configured collision GLB with a stale hash blocks preflight.

The independent Citrine fixture generator now emits the binary little-endian PLY supported by the pinned Viewer 1.42.0. Its previous ASCII PLY failed to load in that runtime; the corrected fixture loads without Chuen Lung coordinates, paths, or transform constants.

## Validation matrix

| Gate | Status | Evidence and limits |
|---|---|---|
| Enclosure parity | PASS | Quantitative comparison above; same boundary surface, different cap triangulation. |
| Navigation approval | NEEDS_REVIEW | Exact approved GLB is now preserved by config and hash, but the Engine project records configured/adopted input rather than a Phase 5B review approval. |
| Phase 5A generated desktop package | PASS | Chrome boundary run with the Engine-generated GLB held at 0.2 Viewer-unit wall/floor/ceiling clearance; additional 2-second wall input moved `0.0072` units. Reset returned to spawn and scene transform stayed invariant. This run used the 64-triangle package. |
| Exact-input Engine package — Chrome | PASS | One canvas; SOG loads; WebGPU; opening camera matches manager target; scale invariant true; camera contained; Reset settles; Traditional Chinese and Help work; no page, console, or HTTP errors. Enter VR remains hidden in desktop Chrome without immersive support. |
| Exact-input Engine package — Safari | PASS | Safari 27.0.1 / macOS 26A434; WebGPU; one canvas; accepted SOG, camera and scale; Reset settles; language and Help work; package index valid. Desktop Safari exposes no WebXR API in this environment. |
| visionOS Simulator six-function UX | NOT_TESTED | CoreSimulatorService could not enumerate available devices (`invalid/connection refused`); the CUA runtime also failed to initialize. No live Simulator interaction occurred. Use the local URL and steps below for manual acceptance. |
| XR transform/reset automation | PASS | 7 Node XR tests pass; browser Reset and scene transform invariants pass. These are not live XR entry/exit tests. |
| XR artificial locomotion and teleport | NEEDS_REVIEW | Guard wrapper and geometry tests pass; desktop Fly wall/floor/ceiling checks pass. Actual XR locomotion and teleport were not exercised in this Simulator run. |
| Independent Citrine fixture | PASS | Separate transform, camera, enclosure and unit scale; Chrome and Safari load; Reset settles; hashes/index verify. |
| Standard XR-disabled Visitor | PASS | Chrome and Safari use the stock Viewer bundle (`408e3cc3…34bec`); 16 indexed files; no XR modules requested; Help, language, and Reset pass; no browser errors. |
| CLI/preflight | PASS | `package --dry-run` reports READY without creating state; approved generated-navigation fixture passes; unapproved status, missing transform, zero scale, and stale GLB hash block. Full output was produced by the existing `build_website()` Publisher path. |
| Full Engine tests | NEEDS_REVIEW | 257 passed, 1 skipped, 2 failed. Both failures reproduce on current `main` and are unchanged by this branch; see baseline attribution below. |
| Patch/build integrity | PASS | Viewer build reproduces SuperSplat 1.42.0 / PlayCanvas 2.23.3 patched bundle SHA-256 `94d9a4c4…f4478ac`; all 14 build resources, all 22 package-index files, and references verify. |
| Git integration | PASS structurally | Clean Phase 5B branch directly based on current `main`; no conflict found; no merge performed. Product acceptance remains gated on Simulator and nav review. |
| Physical Vision Pro | NOT_TESTED | Comfort, physical tracking/walking, physical scale, headset FPS, and thermals are not established by Simulator or desktop results. |

### Browser and artifact details

The exact-input Engine package’s build ID is `ea-xr-998469a6a0c38d36af46891d`; runtime hashes are SuperSplat Viewer bundle `94d9a4c4ad9612385708c21602d5a7f481d352468667dee6d3335beb6f4478ac`, SOG `462a32b31a2cedae912cdd037baf124cae46aa08b6ae91a7e0c12d337ed6b919`, and collision GLB `7779ad3abc8832b4c2b46a2a04fdbe3d1dbfb2c0fcf57f39530f95b51d4e323f`. The accepted SOG, accepted collision GLB, Phase 4D reconstruction, SfM data, and Review Viewer sources were not modified.

The existing desktop boundary browser trace is [chuen-desktop-boundary-chrome.json](</Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/phase5b/results/chuen-desktop-boundary-chrome.json>). Exact-input Chrome and Safari smoke traces are [Chrome](</Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/phase5b/results/chuen-approved-chrome-smoke.json>) and [Safari](</Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/phase5b/results/chuen-approved-safari-smoke.json>). The traces are local ignored build evidence, not shipped assets.

### Engine test baseline failures

The Phase 5B suite ran as `257 passed, 1 skipped, 2 failed`. Current `main` previously ran as `256 passed, 1 skipped, 2 failed`; the Phase 5B delta is one added passing test. The two same failures are:

1. `test_accepted_versioned_alignment_is_handed_to_navigation_as_a_proposal`: `KeyError: 'source'` because the test fixture omits `source.photo_directory`, read by `raw_sfm_image_directory()`.
2. `test_reviewer_and_visitor_preview_reference_real_fixture_assets`: stale expectation `UNVERIFIED`; current preview correctly reports `NOT_CONFIGURED` for the absent navigation config.

Neither affected test or its relevant production path was changed by Phase 5B. No new XR-induced failure was observed.

## Reproduction commands

Run from `/Users/homan/repo/chuen-lung/phase5a-webxr-visitor`:

```bash
PYTHONPATH=src /Users/homan/miniconda3/bin/python -m pytest -q tests/test_visitor_xr.py tests/test_publisher.py
PYTHONPATH=src /Users/homan/miniconda3/bin/python -m pytest -q
/Users/homan/.nvm/versions/node/v26.12.1/bin/node --test tests/visitor_xr.test.mjs tests/visitor_xr_patch.test.mjs
PATH=/Users/homan/.nvm/versions/node/v26.12.1/bin:$PATH npm run build:visitor-viewer
```

The full local Publisher build is at [the generated Visitor package](</Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/phase5b/chuen-full-publisher-accepted-collision/website-visitor/>). Its accepted GLB is configured through `integrations.navigation.approved_collision_glb`; the original Phase 5A config and output were preserved.

## Manual Simulator handoff

The local static server is serving the exact-input package at:

`http://127.0.0.1:8766/phase5b/chuen-full-publisher-accepted-collision/website-visitor/`

In visionOS 27.2 Simulator Safari, enter that URL, wait for the exhibition to finish loading, then verify: Enter VR; check spawn and approach/outside boundary feedback; select Recenter; return manually inside; exit with the system Home control; Reset; then repeat Enter → system Exit → Reset three times. Record `[visitor-xr]` diagnostics and compare the SOG transform before/after. Artificial locomotion and teleport should be tested separately from tracked-head movement. The local server remains running for this handoff.

## Merge and release

The code change is committed on `codex/phase5b-webxr-acceptance` as `5bbe799`; the acceptance report and machine-readable results are in this follow-up commit. Canonical `main` remains at `36a4263`. The Phase 5B diff is isolated and has no identified merge conflict. A future merge can be a controlled merge of this branch into `main` after the remaining Simulator and navigation review gates pass. No merge, public upload, DNS change, or Cloudflare deployment was performed.
