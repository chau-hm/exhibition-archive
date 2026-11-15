# Phase 5A — Reusable WebXR Visitor Integration Report

## Verdict

**NEEDS_REVIEW.** The optional XR publication path, CLI preflight, reproducible pinned Viewer patch, Chuen Lung package, independent synthetic package, and desktop Chrome regression are implemented. The Engine-generated Chuen package has not been exercised in the visionOS Simulator during this task. Its regenerated enclosure GLB also differs from the accepted Chuen fixture, so XR publication should remain a reviewed publication choice rather than a production-ready default.

No public deployment, DNS change, Review Viewer source change, SfM run, training run, SOG regeneration, or accepted asset modification was performed.

## Baseline and isolation

- Canonical Engine checkout was clean on `main` at `5ef152012b8b9fc14e1082b0295097b360accbf1` before this work.
- Changes are isolated in `/Users/homan/repo/chuen-lung/phase5a-webxr-visitor`, branch `codex/phase5a-webxr-visitor`.
- The Chuen Lung project and accepted release-candidate directories were read-only inputs. Generated validation packages are under the ignored `.local/phase5a-artifacts/` directory in this worktree.
- The pinned runtime remains SuperSplat Viewer 1.42.0 / PlayCanvas 2.23.3. The stock Viewer bundle is `408e3cc3cce59d4c438b23b6a27f8e47c385cc6b68c28ee1753af67da0d34bec`; the guarded XR bundle is `94d9a4c4ad9612385708c21602d5a7f481d352468667dee6d3335beb6f4478ac`.

## Implementation

XR is an optional setting on the existing publication profile:

```json
{
  "publication": {
    "visitor_xr": {
      "enabled": true,
      "renderer": "auto",
      "recenter_spawn": "opening",
      "diagnostics": { "enabled": false }
    }
  }
}
```

The existing Engine model, transform, station/camera, navigation envelope, runtime policy, and locale inputs supply all exhibition-specific data. XR modules consume generated Visitor metadata; they contain no Chuen Lung camera, scale, enclosure coordinates, or asset paths.

| Responsibility | Engine implementation |
|---|---|
| XR profile validation and defaults | `src/exhibition_archive/visitor_xr.py`, `config/project-manifest.schema.json` |
| Read-only XR readiness in the existing package command | `src/exhibition_archive/cli.py`, `src/exhibition_archive/publisher.py` (`visitor_xr_preflight`) |
| Optional Visitor staging, one Viewer instance, generated build ID and resource manifest | `src/exhibition_archive/publisher.py`, `templates/visitor/app.js` |
| Capability, renderer choice, lifecycle, camera readiness, diagnostics, boundary UX and Recenter | `templates/visitor/xr/*.mjs`, `templates/visitor/xr/xr-controls.js` |
| Hash-guarded upstream patch generation | `scripts/patch_visitor_viewer_xr.mjs`, `scripts/build_visitor_viewer.mjs` |
| Generated localized Enter VR, status and boundary text | `templates/visitor/index.html`, `templates/visitor/style.css`, `templates/visitor/locales.json` |

With XR omitted or disabled, packaging stages the stock Viewer and omits `js/xr/` and `config/build.json`. With XR enabled, the publisher stages the patched Viewer, copies the XR modules, writes a unique build ID and SHA-256 manifest, and refuses entry when the runtime or navigation hashes do not validate. Both profiles create one Viewer instance and use the same camera, model transform, Fly controls, Reset, Help, and language selection.

`exhibition-archive package <project> --dry-run` is read-only and reports Viewer, patch, model, and navigation readiness. The future GUI contract is one optional “Enable immersive VR” publication toggle, a renderer preference, and advanced boundary feedback settings. It should write `publication.visitor_xr` and display the existing preflight result; it should not add a reconstruction stage or a second publication flow.

## Generated packages

### Chuen Lung full-workflow package

Generated through `publisher.build_website()` from an isolated copy of the Chuen project configuration and existing registered SOG; no SfM or training stage ran:

`/Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/chuen-full-workflow/output/website-visitor/`

- Build ID: `ea-xr-704599258d8c0a32e3b0212a`
- Viewer / PlayCanvas: `1.42.0` / `2.23.3`
- Package index: 22 files, all hashes verified.
- SOG: `462a32b31a2cedae912cdd037baf124cae46aa08b6ae91a7e0c12d337ed6b919` (matches the accepted SOG).
- Opening camera and scene transform match the accepted Chuen fixture exactly; scale is `6.640819914527`.
- Generated collision: `6990254e81c5aeda2078dc89ed81c4f2d3ec350c8a77b90719ea19e7b3d4e024`, 64 triangles / 34 vertices.

The accepted source collision remains unchanged at `7779ad3abc8832b4c2b46a2a04fdbe3d1dbfb2c0fcf57f39530f95b51d4e323f` (68 triangles / 36 vertices). Geometry comparison shows the accepted shell has one additional horizontal boundary point; the point is about `0.05023` Viewer units from the corresponding straight edge in the current Engine-generated mesh. This is a boundary-shape difference, not only an index/triangulation difference. The current Engine package is generated from the configured 17-point envelope. Confirm which enclosure revision is authoritative before publishing Chuen Lung XR.

### Independent synthetic package

`/Users/homan/repo/chuen-lung/phase5a-webxr-visitor/.local/phase5a-artifacts/independent-fixture/fixture/workspace/deliverable/website-visitor/`

The Citrine fixture uses a distinct square envelope, camera, identity scene transform, renderer/boundary thresholds, and one-point synthetic PLY. It was produced through `build_website()` without changing XR source code. Its 22-file package index verifies cleanly. It is a configuration portability test only; its synthetic model is not a visual or XR quality claim.

## Validation matrix

| Check | Result | Evidence / limit |
|---|---|---|
| Viewer patch build reproducibility | **PASS** | Two consecutive Node 24.3.0 builds produced identical stock, XR, app, and runtime hashes. Unsupported stock input and idempotence are covered by Node tests. |
| XR config, preflight, and publisher integration | **PASS** | 17 focused Python tests passed, including disabled-default packaging, XR package integrity, dry-run no-side-effects, and the independent fixture. |
| Boundary, movement wrapper, teleport, eye containment, hysteresis, camera readiness | **PASS** | 7 Node tests passed. These are automated tests, not live XR movement evidence. |
| Broader Engine tests | **NEEDS_REVIEW** | 224 passed, 1 skipped. Two existing tests failed on this branch and failed identically on canonical `main`: `test_accepted_versioned_alignment_is_handed_to_navigation_as_a_proposal` (`source` missing in its fixture) and `test_reviewer_and_visitor_preview_reference_real_fixture_assets` (`NOT_CONFIGURED` vs expected `UNVERIFIED`). Neither touches the XR diff. |
| Chuen Lung desktop Visitor | **PASS** | Chrome 157.0.8103.79 / WebGPU, 53/53 checks at root and nested paths; SOG and accepted PLY comparison, camera, navigation walls/floor/ceiling, Reset, Help, language, responsive UI; no browser, console, network, or HTTP errors. The browser harness uses the Engine's acceptance build flag to expose test-only camera probes. The full-workflow production package was generated separately with that flag off. Report: `.local/phase5a-artifacts/chuen-lung-chrome-acceptance/visitor-browser-acceptance.json`. |
| Standard non-XR package | **PASS** | Automated packaging test confirms stock Viewer, no XR modules/build manifest, and deterministic package index. |
| Engine-generated Chuen build in visionOS Simulator | **NOT_TESTED** | CUA failed to initialize twice. `xcrun simctl` reported CoreSimulatorService disconnected and could not discover runtimes. The prior six manually accepted Chuen UX checks were user-reported for the reference build; they do not validate this Engine-generated build. |
| Safari outside Simulator | **NOT_TESTED** | The available browser automation bridge was unavailable in this session. |
| Physical Vision Pro | **NOT_TESTED** | No physical headset test; scale, tracking comfort, rendering performance, and thermal behavior remain unknown. |
| Live artificial XR locomotion / teleport | **NOT_TESTED** | Active Viewer movement and teleport wrappers are unit-tested, but not exercised in a real XR session for this generated package. |

## Compatibility and release risks

- Runtime patching is limited to the pinned upstream bundle hash. A Viewer or PlayCanvas change must deliberately update the input hash, patch generator, expected output hash, and tests; unknown bundles fail closed.
- Immersive WebXR requires a secure context and browser permission. Unsupported devices keep the standard Visitor UI and stock publication path.
- The generated Chuen collision differs from the accepted enclosure as quantified above. This is the main scene-specific compatibility item needing boundary-owner review.
- Two pre-existing Engine tests remain red on `main` and this branch. No reconstruction, training, Review Viewer source, or accepted asset was modified.
- The public Chuen deployment and all remote routes were left untouched. Release authorization remains `NEEDS_REVIEW`.

## Reproduction

From the Engine worktree:

```bash
npm ci
node scripts/build_visitor_viewer.mjs
PYTHONPATH=src python3 -m pytest -q tests/test_visitor_xr.py tests/test_publisher.py
node --test tests/visitor_xr.test.mjs tests/visitor_xr_patch.test.mjs
PYTHONPATH=src python3 -m pytest -q -k 'not test_accepted_versioned_alignment_is_handed_to_navigation_as_a_proposal and not test_reviewer_and_visitor_preview_reference_real_fixture_assets'
exhibition-archive package /path/to/project --dry-run
exhibition-archive package /path/to/project
```

The generated site is static. For local testing, serve its directory over HTTP or HTTPS, for example:

```bash
python3 -m http.server 8000 --directory /absolute/path/to/website-visitor
```

Use one visionOS Simulator Safari tab. The Simulator's system Home control remains the system XR exit path; then return to Safari and exercise Reset View and re-entry. The six accepted reference-build checks should be repeated against the Engine-generated package before publication. No deployment has been performed.
