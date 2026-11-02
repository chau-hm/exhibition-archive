# Phase 4D.1 Acceptance Report — Chuen Lung

- **Date:** 2026-11-03
- **Visitor functional status:** `PASS`
- **SOG status:** `BLOCKED; PLY FALLBACK ACCEPTED FOR VISITOR FUNCTIONALITY`
- **Public release:** `NEEDS_REVIEW`

The Phase 4C accepted PLY and Review website were used read-only. No SfM, Gaussian training, public deployment, or rights-state changes were performed. The accepted master remains 247,738,651 bytes with SHA-256 `bebbec74f033deed6bf4df8811fbd6537947f9e3ba07d684622a79a4f62d9d3e` and 998,940 Gaussians.

## Acceptance gates

| Gate | Result | Evidence |
| --- | --- | --- |
| Camera consistency and reset | **PASS** | Review and Visitor use the shared canonical pose derived from trusted station `station-046`; Visitor maps it to the pinned SuperSplat coordinate frame. The first visible frame now renders after the model transform and Fly spawn settle. Initial load and Reset View match within browser floating-point tolerance. |
| Free XYZ navigation | **PASS** | Chrome acceptance verified W/A/S/D translation, diagonal movement, Q/E vertical movement, and left-drag mouse look while remaining in Fly Mode. |
| Enclosure containment | **PASS** | Free interior motion was established before each tested boundary. Movement stopped at the accepted wall, floor, and ceiling margins; movement parallel to the wall remained possible. Reset and input transitions retained a contained pose. No furniture or artwork collision was enabled. |
| Minimal Visitor UI | **PASS** | Only Reset View, Help, and language selection are shown; Help starts open and can hide/reopen. No tour, route, geometry overlay, diagnostics, or technical status panel is present. |
| Traditional Chinese / English | **PASS** | Active-scene switching verified without reload or pose reset; `zh-TW`, `zh-HK`, and `zh-MO` map to `zh-Hant`; unsupported locales fall back to English. No Simplified Chinese was found in the Traditional Chinese page text. |
| Modular static package | **PASS** | Production package has 16 indexed payload files totaling 250,163,437 bytes (plus a 2,873-byte index); HTML, JavaScript, CSS, JSON, local Viewer runtime, enclosure GLB, and external PLY are separate. Root and `/nested/` relative paths passed; two publisher builds produced identical package indexes. |
| SOG conversion and fidelity | **BLOCKED** | Two explicitly approved full-master CPU attempts timed out at 600 seconds. No SOG was produced; SOG loading, size, and PLY/SOG image comparison are `NOT_TESTED`. The accepted PLY fallback works. |
| Review profile and guided routes | **PASS** | Review package/index remained read-only and retained all five route artifacts. Final Chrome regression passed root, `/nested/`, and the intentional missing-model case. |
| Public release authorization | **NEEDS_REVIEW** | No deployment or rights changes were made. |

## SuperSplat export reference

The supplied Lumo output contains the 59,318,159-byte `output/splat.html` export and a modular `output/splat/` set with `index.html`, `index.js`, `index.css`, `settings.json`, and an external 41,640,430-byte `index.sog`. The modular page bootstraps the official Viewer with the external SOG and separate settings; its settings camera has a 109° FOV. Both exports informed the compact native-style controls and external-asset packaging decision. No Lumo model, camera pose, or exhibition settings were copied. The listed modular set contains no enclosure collision asset, so Chuen Lung continues to use its own accepted boundary shell and verified SuperSplat collision integration.

## Camera and framing root cause

The accepted Review viewer selected `defaultStationId` from `stations.json` and aimed two common-frame units along that station's representative forward vector at 70.75° FOV. For this project that is station `station-046`. The Phase 4D Visitor implementation interpreted that common-frame pose in SuperSplat's PLY/SOG frame while also applying the accepted model scene transform, which produced the tilted ceiling-facing view and foreground obstruction. The current publisher writes the Review pose into `api/initial-camera.json`; Review consumes its common-frame pose, while Visitor serializes the mapped SuperSplat Y-up pose and applies the matching transformed model scene node and collision shell.

The publisher now creates one canonical camera record from the accepted default trusted station and floor normal. Review reads its common-frame pose; Visitor serializes its SuperSplat Y-up pose into Experience Settings v2 and applies a matching transformed model scene node and collision shell. No Chuen Lung coordinates are hardcoded in the viewer runtime.

For this project, the canonical station is `station-046`. Its common-frame opening position is `[4.0741464, 0.5227502, -1.5821057]`, target `[2.4144166, -0.1638657, -2.4617962]`, floor-normal up `[-0.0150942, 0.9998575, 0.0075566]`, and FOV 70.75°. The pose passed floor, ceiling, and wall clearance checks, and the rendered camera matched the requested SuperSplat position within `1.4e-7` Viewer units with no roll. The clear initial Chrome screenshot shows the photo wall and headphone installations, with open floor for movement; the wall, installations, and floor remain visible without the prior ceiling-facing obstruction. Reset View returns to that same contained opening.

A separate first-frame defect explained why an earlier opening screenshot still looked blurred even after the camera pose matched. SuperSplat resolves `state.loaded` after its first complete frame and then uses on-demand rendering. The Engine applies the shared model scene transform after that event, but previously did not guarantee a new completed frame before hiding the loading curtain. The Reset action later requested rendering and revealed the correctly transformed, sorted scene. The Visitor host now keeps the upstream render loop active through the Fly spawn reset, waits for the camera transition and two rendered frames, then returns to on-demand rendering. The initial screenshot is now clear in both the acceptance build and the production bundle.

## Navigation recovery and SuperSplat compatibility

The pinned upstream Viewer is `@playcanvas/supersplat-viewer` 1.42.0 with PlayCanvas 2.23.3. The Engine uses the supported `createViewer({ ui: false })` integration and the public `setMoveInput(x, z)` API. Official API documentation describes this input as normal camera movement, including collision handling: [SuperSplat Viewer API](https://github.com/playcanvas/supersplat-viewer/blob/main/docs/api.md). The Viewer accepts a GLB collision asset and uses its FlyController; its pinned Fly camera sphere radius is 0.2 Viewer units: [upstream FlyController](https://github.com/playcanvas/supersplat-viewer/blob/main/src/cameras/fly-controller.ts).

The earlier no-translation result had two interacting causes. The upstream desktop WASD shortcut listener changed or intercepted controls intended for captured Gaming Controls, and the Viewer changes to on-demand rendering after load, so ongoing input did not advance the camera update loop. The Visitor host leaves gaming controls and pointer lock disabled, routes W/A/S/D through the public Fly input API, and briefly enables the upstream render loop during held input. Q/E remain native vertical Fly input; left-button dragging remains upstream mouse look. No per-frame camera teleport or alternate collision bypass is used.

The collision GLB is 2,012 bytes, 68 triangles, and SHA-256 `7779ad3abc8832b4c2b46a2a04fdbe3d1dbfb2c0fcf57f39530f95b51d4e323f`. It contains only the accepted horizontal enclosure, floor, and ceiling. The shell is offset for the pinned Fly sphere radius and accepted 0.08-unit camera edge clearance. Collision and model transforms share the SuperSplat Y-up frame. Upstream coordinate-axis behavior and Fly radius are compatibility constraints; rerun browser boundary checks when changing the Viewer version.

## Browser and performance evidence

Real-scene acceptance used Google Chrome 156.0.8078.80 with WebGPU, the accepted Chuen Lung PLY, and the generated local static website. The harness kept the headless Chrome page active during input to avoid background lifecycle throttling. Root and `/nested/` deployment sessions completed **42/42 checks**. It observed 20 HTTP requests and zero HTTP failures, browser errors, console errors, network errors, package-index failures, or local-reference failures. Separate production-bundle smoke runs passed at both root and `/nested/`, with zero browser, console, or network errors; each loaded the external PLY with HTTP 200.

The root and nested local-loopback PLY loads rendered in 3.159 and 3.174 seconds. The model response was 247,738,651 bytes; Chrome reported a 247,738,951-byte transfer size including protocol overhead. This is a local browser/server measurement, not an estimate of public-host load performance. SOG browser performance cannot be measured without a SOG asset.

Visitor acceptance JSON and screenshots, including the corrected initial frame, are in:

`/Users/homan/repo/chuen-lung/exhibition-202610/derived/phase4d1/evidence/visitor-browser-acceptance/`

Representative captures include `screenshots/opening-ply.png`, `screenshots/opening-zh-Hant-active.png`, `screenshots/opening-en.png`, `screenshots/narrow-zh-Hant.png`, and `screenshots/wall-boundary-ply.png`. Production-only Visitor root and nested captures are in `evidence/visitor-production-root/` and `evidence/visitor-production-nested/`; the Review regression report and screenshots are in `evidence/review-regression/`. These local validation artifacts are not included in the static Visitor package.

## SOG investigation

The accepted PLY was not modified. The two separately approved attempts used `@playcanvas/splat-transform` 3.10.3 (build `4f90012`), Node.js 26.12.1, CPU mode, an 8 GiB Node heap limit, 600-second timeout, WebP effort 9, no filters, no decimation, and no coordinate transform. SH clustering used 20 iterations in the first attempt and 10 in the second. `--list-gpus` found no GPU adapter. Both attempts timed out without a usable output.

Upstream performance research found that release 3.6.0 added single-pass gathering, concurrent texture pipelines, and radix Morton sorting; the pinned 3.10.3 build is newer than those changes. The official CLI supports bounded worker counts and verbose/memory reporting. Its documentation cautions that CPU compression can be substantially slower than GPU compression. This supports CPU availability as a plausible contributor but does not prove the timeout cause. The two earlier attempts did not preserve per-stage output or peak RSS.

A diagnostic sample was generated by retaining every 16th complete PLY row. It contains 62,434 Gaussians, preserves all 62 scalar columns including SH3 and normals, and is 15,485,162 bytes (sample SHA-256 `ded40db05bc7233e97f8ae5a9e6114821389fc3ef73f16c8af4f3ddb4097e266`). `splat-transform --info` accepted it as a valid Gaussian PLY. The sample and its provenance note are preserved under `derived/phase4d1/evidence/sog-diagnostic/`; it is not a visitor asset and has not been converted. A separate, exact 120-second sample-conversion approval is awaiting the user; no additional conversion has started in this phase.

The previous timeout evidence did not preserve per-stage progress output or peak RSS, so it is not possible to identify a specific stalled conversion stage or attribute the timeout to SH clustering, WebP, workers, runtime compatibility, or memory pressure. The acceptance package records its own SOG status as `NOT_REQUESTED`; it does not falsely represent those earlier attempts. SOG size and fidelity comparisons at pale hanging text, headphones, wall-mounted works, central installations, and architectural surfaces remain `NOT_TESTED`.

The visitor build keeps SOG conversion opt-in and bounded. The sample conversion is the next diagnostic step and awaits its named approval. Until then, publication remains modular and PLY-based, with the accepted PLY preserved as the reference master. Upstream conversion tooling is documented at [PlayCanvas SplatTransform](https://github.com/playcanvas/splat-transform).

## Package and release

The generated Visitor package is external to the Engine source and contains separate `index.html`, `js/viewer.js`, `css/viewer.css`, `config/*.json`, `vendor/*`, and `assets/*` files. It runs over HTTP/HTTPS at a root or nested prefix and deliberately does not support `file://`. It has no Engine Python/runtime service dependency and no third-party CDN dependency. It is a static publication candidate, not a Preservation Package.

The reproducibly rebuilt production package is at:

`/Users/homan/repo/chuen-lung/exhibition-202610/derived/phase4d1/output/website-visitor/`

Its `config/package-index.json` lists all 16 payload file sizes and SHA-256 values. Two successive calls to the publisher produced byte-identical package indexes (SHA-256 `28dbbebe65b762fb96350d0dfa0c9f6672ef5556dbf17edcc55f470352a5cc52`). The external PLY SHA-256 matches the Phase 4C master. Production-bundle HTTP smoke screenshots and results are recorded alongside the browser evidence above.

The Review profile and its five accepted guided routes remain available independently. Public rights review and destination-specific release approval remain separate; release authorization is still `NEEDS_REVIEW` and nothing has been deployed.

## Validation record

- Visitor real-browser acceptance: **42/42 passed**; see the JSON evidence path above.
- Review browser regression: **PASS** for root, `/nested/`, and the intentional missing-model case after disabling Chrome headless background throttling; see `derived/phase4d1/evidence/review-regression/browser-acceptance.json`. The read-only package retained all five guided routes and their waypoints.
- Unit/regression suite: **81 passed, 3 subtests passed** with `PYTHONPATH=src .venv/bin/python -m pytest tests -q`.
- Production Visitor package build was repeated with an identical package index; package inventory, paths, hashes, and references passed browser acceptance. Phase 4C and Phase 4D were read-only; the new candidate and evidence were written to `derived/phase4d1/`.
- SOG conversion, SOG loading, and visual fidelity comparison: **blocked / not tested**, as described above.
