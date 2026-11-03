# Publish and preservation outputs

The Publish Package builds separate Review and Visitor static websites from approved public inputs. Both contain the selected model, accepted navigation, public metadata, runtime assets, and integrity manifests; the profiles have distinct viewer interfaces. They exclude source photographs and private review records.

The Preservation Package is a separate future deliverable. It is not implemented as a self-contained package. A source index or visitor website does not replace original image preservation, SfM reconstruction, review history, provenance, reproduction instructions, or source checksums.

Before publication:

1. Select and review the exact model candidate.
2. Review navigation for the exact proposal and viewer transform.
3. Review artwork identity and public catalogue fields.
4. Decide image/artwork reproduction rights and access restrictions.
5. Build the local static candidate with package.
6. Run verify --full-hash and browser acceptance.
7. Record publication-review for the exact package candidate.
8. Run verify --public-release --full-hash and inspect all public files.

The Engine records a local human decision; it cannot establish the reviewer's legal authority or independently determine rights. A local package is not permission to publish or deploy. Do not automatically deploy.

## Build a static website

From the canonical Engine checkout, use the package-only command against a project workspace whose accepted model and navigation artifacts are already validated:

```sh
./bin/exhibition-archive package /path/to/project-workspace
./bin/exhibition-archive verify --full-hash /path/to/project-workspace
```

`package` builds the independent Review and Visitor websites, archive metadata, and verification outputs from the selected inputs. It skips SfM and Gaussian training. The websites are written to the project's configured output directory at `website/` and `website-visitor/`.

Before deployment, confirm that `config/package-index.json` reports `exhibition-archive.website-index.v2` and that `verify` passes. The index records size and SHA-256 for every regular website payload file except the index itself. Verification rejects omitted, extra, duplicate, unsafe, or changed paths and resolves local HTML, CSS, JavaScript, and model references within the package.

## Publication profiles

The Engine ships two separate static profiles:

- **Review** (`output/website/`) retains the Engine's diagnostic viewer, route inspection, technical controls, and existing guided-route data. Keep this profile for review and regression work.
- **Visitor** (`output/website-visitor/`) opens directly into the scene using the official SuperSplat Viewer embedding API. Its presentation contains only Reset View and brief navigation help. It does not publish route selectors, tours, diagnostics, geometry overlays, or technical status panels.

The Visitor runtime is pinned in `package-lock.json`. From the repository root, install and build its local bundle before packaging:

```sh
npm ci
npm run build:visitor-viewer
```

The Visitor profile pins `@playcanvas/supersplat-viewer` 1.42.0 and PlayCanvas 2.23.3. It uses `createViewer({ ui: false, ... })`, keeping upstream loading, rendering, camera, and collision behavior while the Engine supplies its small visitor presentation. Official API references: [embedding API](https://github.com/playcanvas/supersplat-viewer/blob/main/docs/api.md) and [Viewer guide](https://developer.playcanvas.com/user-manual/supersplat/viewer/).

| Integration option | Maintenance and behavior | Assessment |
| --- | --- | --- |
| Official SuperSplat Viewer `createViewer` API | Reuses upstream PLY/SOG loading, renderer selection, camera controls, reset, and supported collision loading. `ui: false` gives the host a clean presentation surface. | Selected for Visitor. The upstream `.glb` collision API accepts the enclosure shell; its hidden axis transform and Fly radius are pinned and covered by compatibility checks. |
| Current Engine PlayCanvas viewer with simplified presentation | Keeps the current renderer and accepted navigation code, but requires the Engine to keep maintaining its own model loader, camera controls, collision behavior, and format support. | Retained as the Review profile, including diagnostics and route inspection. Its technical presentation stays separate from the Visitor package. |

The Visitor uses a small Engine-owned presentation layer because the upstream UI has a fixed built-in locale set that includes Simplified Chinese (`zh-CN`) but no Traditional Chinese (`zh-Hant`) or supported locale-registration hook. Recreating only the visitor's help, reset, and language controls keeps translation complete without forking upstream UI or duplicating camera/navigation behavior. The upstream runtime remains pinned; repeat the language, camera, and browser checks when upgrading it.

The Engine provides an enclosure-only GLB to the Viewer collision API. It is built from the accepted horizontal boundary, floor, and ceiling. The mesh is offset for the upstream Fly camera's fixed 0.2-unit collision sphere so its center remains within the accepted camera clearances. This collision asset contains no furniture or artwork. Navigation poses and collision geometry are mapped into the Viewer 1.42.0 default PLY/SOG coordinate frame without transforming the accepted PLY master. The runtime's default axis rotation and collision radius are compatibility constraints; re-pin and repeat boundary checks when upgrading the Viewer. Fly Mode remains active, with unrestricted XYZ movement inside the enclosure. Walk Mode and orbit/framing transitions are blocked in this profile.

The current official Viewer accepts collision meshes through its `.glb` collision URL and runs the same Fly movement/collision path used by the first-party Viewer. Its `ui: false` API does not supply the host's visitor help or reset presentation, so those two controls are implemented by the Engine. See the [public handle API](https://github.com/playcanvas/supersplat-viewer/blob/main/docs/api.md#controlling-the-camera) and the upstream [FlyController](https://github.com/playcanvas/supersplat-viewer/blob/main/src/cameras/fly-controller.ts).

Visitor input keeps the upstream `gamingControls` preference disabled so it never requests pointer lock. Upstream automatically treats desktop WASD as a shortcut to captured Gaming Controls, so the Visitor host maps W/A/S/D to SuperSplat's public `setMoveInput(x, z)` API. This uses the normal Fly movement and collision path without changing camera mode or capturing the pointer. Q/E use upstream vertical movement, and holding the left mouse button while dragging uses upstream mouse look. The host briefly enables the render loop while input is held because the Viewer otherwise switches to on-demand rendering after loading. Test-only acceptance hooks are excluded from normal Viewer builds.

SuperSplat resolves `state.loaded` after its first complete frame, then switches to on-demand rendering. The Engine applies the shared model transform after that event, so the Visitor keeps the upstream loop active through the Fly spawn reset and waits for the camera transition plus two rendered frames before revealing the scene. This prevents the first visible frame from preceding the model transform or its depth-order refresh. Reset still returns to the same canonical opening pose.

### SOG derivative

The publisher uses the pinned open-source `@playcanvas/splat-transform` 3.10.3 CLI. SOG conversion uses 10 SH clustering iterations, four workers, the converter's default lossless WebP effort, no filters, no decimation, and no coordinate transform. Conversion is opt-in: `EXHIBITION_ARCHIVE_BUILD_SOG=1` is required. `EXHIBITION_ARCHIVE_SOG_DEVICE=auto` (default) enumerates supported GPU adapters and selects the first reported adapter; a numeric value selects that adapter. If no adapter is visible, conversion stops with `GPU_UNAVAILABLE` and the PLY publication profile is built. It never changes silently to CPU. CPU execution requires both the SOG opt-in and `EXHIBITION_ARCHIVE_SOG_DEVICE=cpu`. Each conversion is capped at 600 seconds and an 8 GiB Node heap. Outputs are written to a staging filename, checked with `splat-transform --info`, compared against the PLY Gaussian count and SH bands, and ZIP/CRC validated before promotion.

To reuse an SOG converted on a GPU-capable host, register the external artifact without a GPU or recompression:

```bash
exhibition-archive sog-import /path/to/project \
  /path/to/exhibition.sog \
  --converter-version 'splat-transform v3.10.3 (4f90012)' \
  --conversion-command 'splat-transform -g 0 --verbose --memory accepted-master.ply exhibition.sog -i 10' \
  --device 'Apple M4 Pro' --gpu-selection 0 --sh-iterations 10 \
  --execution-metadata-json '{"elapsed_seconds":17.274,"gpu_peak_memory":"122.3 MB","cpu_peak_memory":"1.21 GB"}'

exhibition-archive package /path/to/project
```

The import command verifies the project's configured PLY hash, inspects the PLY and SOG formats, compares Gaussian count and SH bands, validates SOG archive members and CRCs, and records both input and output hashes. The registered reusable derivative and private provenance are stored under `output/artifacts/visitor-sog/`. Publication rechecks those bindings before using the SOG. A SOG-primary Visitor package contains the independent `assets/exhibition.sog` but omits the unused 247.7 MB PLY. The accepted PLY remains at its original master location. When no valid SOG is registered or converted, the Visitor PLY profile contains the accepted PLY instead.

For Chuen Lung, the three earlier CPU attempts timed out after 600 seconds (full master, 20 then 10 iterations) and 120 seconds (1-in-16 sample). The old Engine explicitly passed `--gpu cpu` because its process could not enumerate an adapter. The user's confirmed host conversion used the same SplatTransform 3.10.3 build, `-g 0`, 10 iterations, Apple M4 Pro GPU, about 17.274 seconds, and produced a visually inspected 16,137,572-byte SOG. The current Engine process still reports no visible GPU adapter. These facts explain the mode mismatch and support an environment-dependent visibility difference; they do not prove sandboxing was the sole cause of the CPU stalls. Exact CPU stall stage remains unknown. See the Phase 4D.2 acceptance report for hashes, screenshots, measured browser load, and navigation checks. Conversion or a static package does not approve public release.

Repeated publication from the same registered SOG bytes, accepted PLY identity, settings, and runtime produces a deterministic package index. GPU compression is not promised to be byte-identical across separate runs; every output hash is recorded. SOG is lossy relative to PLY, and package equivalence does not claim byte-level or visual equivalence.

## Static hosting

Upload the *contents* of the intended profile directory to the static host's document root or to a nested prefix, preserving its directory layout. The Review package has:

```text
index.html
viewer/
api/
config/
assets/exhibition.ply
```

Serve the website over HTTP or HTTPS. Do not open the files with `file://`. Both viewers and their bundled runtime assets use relative paths, so the same package works at `/` and at paths such as `/exhibitions/chuen-lung/`. Serve the selected Visitor SOG or PLY model and enclosure GLB as unchanged binary assets; allow full model downloads and byte ranges. A SOG-primary package does not include the unused PLY master. The package needs no Engine checkout, Python installation, backend process, API service, database, or runtime asset proxy after deployment.

`README.md` is included in each website with its selected model hash. Keep `config/package-index.json` alongside the files. The Visitor package instead has:

```text
index.html
assets/exhibition.sog                 # SOG-primary Visitor profile
# or assets/exhibition.ply            # PLY publication profile when SOG is unavailable
assets/enclosure-collision.glb
config/exhibition.json
config/settings.json
config/locales.json
config/package-index.json
js/viewer.js
css/viewer.css
vendor/supersplat-viewer.js
vendor/supersplat-viewer.css
README.md
INTEGRATION.md
```

`settings.json` carries the official Experience Settings v2 camera and rendering settings; `exhibition.json` records the selected model paths, accepted PLY identity, transform, collision geometry, and runtime versions. Both Review and Visitor derive their opening pose from the same canonical Engine camera representation, then the Visitor publisher maps it into the official Viewer coordinate frame. This fixes the earlier mismatch, where the Visitor camera and accepted navigation/model transform were interpreted in different coordinate frames. The published Chuen Lung opening is station `station-046`: common-frame position `[4.0741464, 0.5227502, -1.5821057]`, target `[2.4144166, -0.1638657, -2.4617962]`, floor-normal up, and 70.75° FOV. It is validated against the accepted walls, floor, and ceiling before publication.

The browser acceptance command used for the Visitor profile is:

```sh
npm run build:visitor-viewer
EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE=1 PYTHONPATH=src ./bin/exhibition-archive package /path/to/project-workspace
PYTHONPATH=src .venv/bin/python tests/run_visitor_browser_acceptance.py \
  --website /path/to/project-workspace/output/website-visitor \
  --review-website /path/to/project-workspace/output/website \
  --output /path/to/project-workspace/evidence/visitor-browser
```

The opt-in package flag enables test-only camera and input hooks for the acceptance driver; ordinary packages omit them. Do not deploy the test package. The driver exercises root and nested hosting paths in separate browser sessions, visitor UI, camera mode and containment, keyboard transitions, reset, model loading, performance, and browser errors. It captures paired PLY/SOG viewpoints only when a SOG derivative exists. It is a development tool, not part of the visitor deployment. The modular package requires HTTP/HTTPS; opening `index.html` with `file://` is not supported.

The existing Review-profile regression command remains:

```sh
PYTHONPATH=src .venv/bin/python tests/run_browser_acceptance.py \
  --website /path/to/project-workspace/output/website \
  --output /path/to/project-workspace/evidence/browser
```

The Review acceptance harness serves that static package locally at both `/` and `/nested/`, then checks a deliberately missing model response. It is a development verification tool, not part of the visitor deployment.

The Chuen Lung Phase 4D.1 camera, navigation, SOG, package, and browser findings are recorded in [the acceptance report](phase4d1-acceptance.md).

## Scope and release gates

The Review website contains the selected PLY, its diagnostic viewer runtime, accepted navigation, guided route poses, and public metadata. The Visitor website contains the accepted PLY master, optional SOG derivative, official Viewer runtime, enclosure collision, and minimal visitor presentation. Both exclude source photographs and private review records. Neither is a self-contained Preservation Package: source materials, full preservation metadata, review history, reproduction instructions, and source checksums are not bundled with them.

Technical package acceptance and public publication approval are separate. Passing `package`, `verify`, and browser checks records only local technical readiness. Rights, access restrictions, public-release approval, and deployment remain explicit human decisions; this workflow does not change those states or publish the site.
