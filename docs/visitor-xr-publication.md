# Optional WebXR Visitor Publication

WebXR is an optional profile of the existing Visitor Publisher. It does not add a reconstruction stage and does not change the Review Viewer, selected model, SOG bytes, or Engine enclosure. Projects without `publication.visitor_xr.enabled` keep the standard Viewer bundle and omit the XR modules.

## Enable XR

The project configuration contains the scene-specific model transform, initial camera, navigation enclosure, floor/ceiling limits, and supported languages through the existing Viewer and navigation configuration. Only XR policy is added under `publication.visitor_xr`:

```json
{
  "publication": {
    "visitor_xr": {
      "enabled": true,
      "renderer": "auto",
      "recenter_spawn": "opening",
      "boundary_feedback": {
        "approach_distance_viewer_units": 0.85,
        "outside_enter_clearance_viewer_units": 0.0,
        "outside_exit_clearance_viewer_units": 0.18,
        "fade_duration_ms": 420,
        "maximum_vignette_opacity": 0.38
      },
      "diagnostics": { "enabled": false }
    }
  }
}
```

`renderer` accepts `auto`, `webgl`, or `webgpu`. `auto` retains WebGPU for desktop and selects WebGL for immersive VR when the browser does not expose the WebGPU XR binding. Boundary values are in Viewer scene units; they are not metres unless the exhibition has an explicit calibration. `recenter_spawn` currently supports the generated opening camera only. The defaults apply when optional boundary values are omitted.

When a project already has an approved collision GLB, set `integrations.navigation.approved_collision_glb` to its workspace-relative or absolute `path` and expected `sha256`. The Publisher verifies the file and hash, then copies the GLB byte-for-byte into the Visitor package. The source mesh must already use the configured SuperSplat Y-up world frame and match the approved enclosure; the Publisher does not transform it. If this setting is omitted, the existing Engine-generated enclosure path remains in use.

Set the profile through the existing CLI, then run its read-only preflight and package stages:

```bash
exhibition-archive config-set /path/to/project --key publication.visitor_xr \
  --value '{"enabled":true,"renderer":"auto"}'
exhibition-archive package /path/to/project --dry-run
exhibition-archive package /path/to/project
```

The preflight does not create workflow state or modify package files. It checks the pinned Viewer bundle, patch manifest, configured navigation inputs, accepted model hash, and any explicitly supplied approved collision GLB. Packaging checks that the opening camera is contained by the configured enclosure and that the collision asset matches its publication metadata. An invalid XR request fails with a specific error; it does not silently publish unbounded XR navigation.

## Rebuild the pinned Viewer runtime

Use Node 22 or later. The Viewer and PlayCanvas dependencies are pinned in `package-lock.json`:

```bash
npm ci
npm run build:visitor-viewer
```

The normal Visitor bundle remains the upstream SuperSplat Viewer 1.42.0 / PlayCanvas 2.23.3 output. The separate XR patch generator verifies the upstream bundle SHA-256 before applying `rebaseHome()` compatibility, CameraManager state diagnostics, floor-placement diagnostics, and the localized Palm Menu Recenter hook. It verifies the resulting patched bundle SHA-256 and writes `runtime-xr.json`. Unsupported upstream bundles fail closed. The build is deterministic for the pinned inputs and may be rerun safely.

The publisher stages exactly one Viewer bundle at `vendor/supersplat-viewer.js`: stock for the standard profile, patched for XR. Both profiles create one Viewer instance. XR-enabled packages add `js/xr/*`, `config/build.json`, the build ID, and localized XR controls. The browser verifies resource hashes before exposing Enter VR; a failed hash check keeps XR unavailable.

## Diagnostics and local Simulator testing

When `diagnostics.enabled` is true, the bounded trace can be retrieved in Safari's Web Inspector after returning from XR:

```js
window.__visitorXRDebug.dump()
```

The trace records the generated build ID, resource hashes, selected and actual renderer, XR capability and mode changes, camera/rig state, collision decisions, boundary state, recenter, exit, and reset. The runtime trace is kept in bounded memory and local storage. With diagnostics disabled, the page exposes only the internal boundary state and recenter hooks used by the XR UI.

Serve the generated `website-visitor` directory over localhost or HTTPS. For example:

```bash
python3 -m http.server 8000 --directory /absolute/path/to/output/website-visitor
```

Open `http://localhost:8000/` in one visionOS Simulator Safari tab. Run Enter VR, approach-boundary, boundary crossing, Recenter, return-inside, system Exit VR, Reset, and re-entry checks. For the system exit check, use Simulator Controls → Home while the immersive session is active, then return to Safari. Do not treat mocked WebXR events as Simulator acceptance. Physical Vision Pro scale, eye/hand interaction, performance, and comfort remain untested by Simulator.

Automated regression commands:

```bash
PYTHONPATH=src python3 -m unittest tests/test_visitor_xr.py tests/test_publisher.py
node --test tests/visitor_xr.test.mjs tests/visitor_xr_patch.test.mjs
```

The Python tests cover disabled and enabled publication, read-only preflight, package-index integrity, build hashes, a second non-Chuen configuration, and unchanged model/enclosure bytes across profiles. Node tests cover enclosure geometry, movement and teleport wrapper integration, eye containment, recenter calculations, feedback hysteresis, camera readiness, and patch reproducibility. They do not replace actual Simulator acceptance.

## Visitor Publisher and future GUI contract

The CLI uses `publication.visitor_xr`; the planned GUI should expose one optional “Enable immersive VR” publication setting, renderer preference, and advanced boundary-feedback values. It should write this same configuration and show the existing `package --dry-run` XR readiness report. It should not add an XR reconstruction step, duplicate enclosure geometry, or edit generated Visitor files.

The shared runtime modules live in `templates/visitor/xr/`. Generated model, camera, coordinate frame, enclosure, and floor/ceiling constraints continue to come from the project's existing Visitor publication metadata. There is no Chuen Lung camera, scene scale, asset path, or enclosure coordinate in these modules.

No remote deployment is part of this integration. The local package remains a publication candidate subject to existing release and rights review.

See [the Phase 5A implementation and validation report](visitor-xr-phase5a-report.md) for the generated Chuen and independent packages, test matrix, known collision-geometry difference, and Simulator limitations.
