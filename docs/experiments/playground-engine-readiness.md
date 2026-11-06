# Playground Engine readiness

**Status:** Engine implementation and safe readiness validation complete; the actual playground reconstruction has not been run by Codex. The existing locally produced SfM files were parsed read-only to validate the metric fix. CLI reliability work adds locked uv environment inspection/repair, bounded live progress, concise review prompts, actionable stage errors, and a portable COLMAP/Spirula dataset export. Structural export validation does not prove that the Spirula GUI will reopen it. This report does not claim real LiDAR alignment, training completion, GUI reopening, or publication approval.

## Implementation summary

The new folder-based `run` path reuses the existing project state, stage DAG, Spirula SfM and training adapters, COLMAP validation, navigation generator, and bundled SuperSplat Visitor runtime. It adds optional input discovery and PLY inspection, scale-aware LiDAR registration and conservative navigation handoff, a private first-visual Reviewer/Visitor package, and a report/status entry for the output. Existing `run WORKSPACE` and photo-only projects retain their prior path.

The requested command stages are `photo_inventory`, optional `lidar_inspection`, `capture_preflight`, optional `sfm_dataset_prep`, `artwork_association`, `sfm`, optional `lidar_registration`, `navigation`, `training`, and `first_visual`. LiDAR inspection and registration have independent fingerprints, so changing an optional PLY does not invalidate cached photo-based SfM or training. A successful run stops with `AWAITING_VISUAL_REVIEW`; it does not launch ROI retraining, parameter sweeps, package publication, or fine-tuning.

Fresh SfM and training still require **separate, one-use approvals** for their current full-hash plans. The `--interactive-authorization` option presents each plan in the local Terminal and asks for its exact authorization phrase, reviewer, and rationale. If the SfM result is `NEEDS_REVIEW`, the Engine stops for a component-scoped SfM decision; training then requires a separate approval. These existing repository gates remain in force.

The opted-in interactive run offers a guided SfM component decision (accept for experimental training, reject, show details, or defer). It records the same component-scoped audit decision as `sfm-review`; acceptance requires a reviewer, rationale, and explicit limitation. A review decision does not approve LiDAR geometry, navigation, model selection, or publication.

## Changed files

- `src/exhibition_archive/cli.py` — folder `run`, preflight/dry-run, explicit authorization prompts, status output, and read-only `sfm-revalidate` / scoped `sfm-geometry-review` commands.
- `src/exhibition_archive/workflow.py` — independent LiDAR inspection/registration stages, validated geometry handoff, optional dependency/review-scope fingerprints, first-visual integration, and resume/cache wiring.
- `src/exhibition_archive/lidar.py` — mixed input discovery, raw/Gaussian PLY classification, streamed ASCII/binary PLY inspection, bounded global registration, local similarity refinement, registration evidence, and navigation-plane proposals.
- `src/exhibition_archive/input_prep.py` — provenance-bound, byte-identical image-only copy that leaves originals and EXIF bytes untouched.
- `src/exhibition_archive/navigation.py` — scene-type-aware outdoor coverage proposal without fabricated ceiling/walls; missing ground or vertical evidence remains unverified, and aligned LiDAR supplies only a proposed plane.
- `src/exhibition_archive/preview.py` — versioned private Reviewer/Visitor output, candidate identity validation, raw/aligned LiDAR and registration diagnostics, and first-visual summary.
- `src/exhibition_archive/backends.py` — COLMAP reprojection metric recomputation and verified Spirula summary provenance, plus explicit scene-center and training configuration evidence.
- `src/exhibition_archive/sfm_review.py` — artifact-bound read-only revalidation, metric provenance, and a geometry-proposal review scope distinct from training acceptance.
- `src/exhibition_archive/state.py` — read-only SQLite access for revalidation without schema, journal-mode, or fingerprint-cache writes.
- `pyproject.toml` — optional `lidar-registration` extra for the bounded Open3D registration backend.
- `tests/test_sfm_reprojection_metrics.py` — compact binary COLMAP fixtures for camera-based reprojection metrics, metric provenance, backend summary verification, and the unchanged 2.0 px threshold.
- `templates/visitor/app.js` — allow an unbounded technical preview when optional collision geometry is absent.
- `tests/test_end_to_end_engine.py` — synthetic/mocked discovery, PLY, dry-run, resume, navigation, and preview checks.
- `tests/test_authorization.py`, `tests/test_sfm_review.py`, and `tests/test_training_storage.py` — retained gate and training command fixtures cover the new configuration evidence and scene-coordinate option.
- `docs/installation.md` — entry point to the runbook.
- `src/exhibition_archive/environment.py` — read-only environment inspection and bounded `uv sync --locked` repair, required/optional dependency distinction, import validation, and report persistence.
- `src/exhibition_archive/progress.py` — bounded incremental log parsing, observed Spirula/training telemetry, ETA calculation only after observed rates, durable JSONL events, and human-readable formatting.
- `src/exhibition_archive/studio_export.py` — content-addressed, portable COLMAP package export and structural validation for reopening the SfM dataset in Spirula Studio.
- `tests/test_environment.py`, `tests/test_progress.py`, and `tests/test_studio_export.py` — uv bootstrap, progress parsing, 77-image package export, relocation, integrity, and failure tests.
- `uv.lock` — updated with the declared optional LiDAR registration extra and its platform-resolved dependencies.

The iteration-specific regression tests are `tests/test_sfm_reprojection_metrics.py`, `tests/test_sfm_review.py`, `tests/test_end_to_end_engine.py`, `tests/test_navigation.py`, `tests/test_cli.py`, `tests/test_environment.py`, `tests/test_progress.py`, and `tests/test_studio_export.py`. Geometry registration has only synthetic validation in this checkout; the actual DepthEye cloud has not been registered.

## CLI and input behavior

The implemented command is:

```bash
exhibition-archive run --input INPUT_DIR --output OUTPUT_DIR --project-id STABLE_ID \
  --lidar auto --scene-type auto --profile first-visual --until visitor [--dry-run]
```

`--project-id` is operator supplied and required. The input and output must be separate, non-overlapping directories. The command refuses a non-empty output without its own existing Engine project. Resume uses `exhibition-archive resume OUTPUT_DIR/.engine --profile first-visual --until visitor`.

Supported image suffixes follow the existing Engine/backend list. Images remain read-only. Non-HEIF photos are copied byte-for-byte into an image-only workspace directory with hash mappings; HEIC/HEIF uses the existing libheif conversion and validation path. DNG/CR2/CR3/NEF/ARW/RAF/RW2 are reported as unsupported, because installed Spirula help has not established support for them. PLY headers are classified so Gaussian model PLY files are excluded from raw LiDAR selection. A single non-Gaussian PLY is selected; zero, multiple, corrupt, and unsupported candidates produce explicit states, and LiDAR issues do not block photos.

The dry-run scans input names and PLY headers, validates image inventory, the actual `.venv` interpreter, package versions/importability, backend command support, HEIF converter need, optional registration availability, and bundled Viewer assets. It reports any locked uv sync it would use but creates no output directory, project manifest, SQLite state, cache, image copy, or installation. `--interactive-authorization` plus a local TTY is required before a real run may repair missing or incompatible declared Python dependencies; `--no-auto-install` disables that repair. An installed package that fails to import is reported as an optional warning instead of being blindly reinstalled, since the cause can be a missing native library. Full PLY vertex inspection and reconstruction/training work are deferred until the non-dry run.

### Spirula Studio dataset export

The earlier SfM output split the registered JPEG dataset and the `workspace/sparse/0` COLMAP files into different locations. Opening only the sparse reconstruction directory did not provide the corresponding `images/` folder, so Spirula could not resolve the registered image names. The SfM backend output is preserved; the new `sfm_studio_export` stage creates a movable package with `images/`, `sparse/0/{cameras.bin,images.bin,points3D.bin}`, an integrity manifest, and image derivative provenance. Images are copied from the exact effective dataset used for SfM; originals and sparse model bytes are unchanged.

Automated checks validate all registered image names, content hashes, image dimensions against camera records, camera references, sparse artifacts, and package relocation. The test suite marks GUI status `NOT_GUI_VERIFIED`; no GUI round-trip is claimed. On the next local run, the user must open the exported package in Spirula Studio and verify that registered photos and sparse geometry load without starting a new reconstruction.

The Engine stage report uses an explicit JSON-safe schema with artifact paths relative to the configured output root; it does not stringify arbitrary objects. A content-addressed hidden staging directory retains and revalidates completed copies after interruption, while a fully published package is reused on retry without copying its JPEGs again.

## LiDAR behavior and current limits

The Engine records point count, coordinate bounds, PLY encoding and properties, RGB/normals, invalid values, sampled outlier diagnostics, and header-declared units when present. Units otherwise remain unknown. It streams point records and writes a bounded preview rather than retaining the input cloud in memory.

After SfM, optional Open3D FPFH/RANSAC registration is initialized after independent point-cloud normalization, estimates a 7-DoF similarity transform, then refines it with trimmed mutual-nearest-neighbor correspondences. It checks bidirectional overlap, correspondence support, degeneracy, and normalized residuals. Each execution writes `lidar/registrations/alignment-execution-NNNNNN/alignment-report.json` and its transformed preview; `lidar/alignment-report.json` is the current report copy. Weak or unavailable registration is rejected. Source LiDAR is never passed to COLMAP or converted into Gaussian attributes.

An accepted transform is eligible for navigation diagnostics only when the current SfM component has a separate `sfm-geometry-review` decision. Navigation remains a proposal: it combines accepted aligned LiDAR plane evidence with the camera-pose-first coverage footprint, labels any LiDAR ground-plane candidate as unconfirmed, and retains the independent navigation approval gate. The current training path remains photo-only. Reviewer copies the raw and aligned previews, the current alignment report, the navigation proposal, and camera/SfM diagnostics; Visitor receives no LiDAR controls.

Open3D is an optional dependency, declared by the `lidar-registration` extra. `uv.lock` now records that extra and its dependency set. The current development `.venv` reports NumPy importable, but Open3D import fails because its native `libusb-1.0.0.dylib` dependency is not present at the Homebrew path compiled into the wheel. This is an optional host-library limitation, not a Python lockfile failure; the Engine must keep the photo-only pipeline available and show the precise import diagnostic. No packages or native libraries were installed during this task and no real registration was run. For Apple Silicon local registration, use the uv extra and satisfy the missing native library separately if preflight still reports the import failure.

### SfM reprojection metric correction (2026-11-06)

The local reconstruction's `points3D.bin` has 63,824 point-error scalars, all `0.0`. Those fields were not a usable reprojection statistic for this Spirula output. The Engine had averaged them and emitted `REPROJECTION_ERROR_ZERO_OR_UNINFORMATIVE`, although the backend log separately reported the mean and median **per observation**. The parser now recomputes residuals from COLMAP camera models, poses, 3D points, 2D observations, and reciprocal tracks; it reports per-point and per-observation statistics separately. The backend log statistic is kept separate and is accepted only when observation count and recomputed mean/median agree to the log's three-decimal precision. A genuinely zero recomputation remains a valid zero; missing or unprojectable data is reported as unavailable.

Read-only parsing of the saved artifact at `backend-runs/sfm/20261105T152542Z-execution-0007/workspace/sparse/0` measured 77/77 registered images, 63,824 points, and 201,005 track observations. The recomputed mean of per-point mean residuals is `1.825250 px`; observation-weighted mean and median are `1.999459 px` and `1.784009 px`. Spirula's `1.999 px` / `1.784 px` / `201005 observations` summary independently verifies within `0.0011 px`. Revalidation passes the existing 2.0 px per-point threshold with no current warnings. The original `sfm-validation.json`, project database, point cloud, and backend log were not modified. These values establish parser behavior on the existing COLMAP output; they do not establish training suitability or LiDAR alignment.

Use `sfm-revalidate` to print the current artifact-bound result without rewriting the historical report or opening SQLite for writes. This command was run successfully against the existing workspace in this implementation phase. The separate training acceptance remains required because the durable SfM stage is still `NEEDS_REVIEW`. A new `sfm-geometry-review` records only diagnostic registration and an unapproved navigation proposal; it cannot authorize training or navigation approval.

## Navigation and publishing

Navigation remains camera-pose-first. Indoor proposals retain floor/ceiling and enclosure evidence where supplied. Outdoor proposals derive a horizontal camera-coverage footprint and do not infer physical walls or a ceiling. If accepted aligned LiDAR and the registered camera trajectory support a plane candidate, the Engine records it as `PROPOSED_REQUIRES_HUMAN_GROUND_IDENTIFICATION`; it is never treated as an approved physical ground surface. The bounded candidate finder requires at least 100 sampled LiDAR points and 6 camera centers, screens camera-trajectory planarity, uses 400 deterministic RANSAC trials, a 15-degree alignment band, and a 0.3%-of-scene-diagonal point band. These are proposal-screen settings in arbitrary SfM units, not confidence scores or survey tolerances. If evidence is insufficient, ground and navigation remain unverified. No furniture/equipment collisions, walls, or ceiling are fabricated.

The private first-visual Visitor uses the bundled SuperSplat runtime and the actual validated Gaussian PLY with relative local asset paths. The first-visual banner marks navigation unverified and public release unapproved. The technical Reviewer embeds that Visitor preview and links copied SfM, camera, LiDAR, navigation, training, and log diagnostics when they exist. Navigation and release remain human-reviewed gates. No browser was used in this readiness phase.

## Safe validation results

The lightweight regression suite passed: 161 tests and 3 subtests, with one warning from the installed Dash pytest plugin about its deprecated hook configuration. `compileall`, `uv lock --check`, and the real input-directory `--dry-run --json` check also passed. The dry run found 77 HEIC images and one unambiguous ASCII RGB PLY; Open3D is installed but fails to import because `libusb-1.0.0.dylib` is missing. It confirmed that photo-only processing remains available, reported optional LiDAR registration as unavailable, and did not create the requested output directory. No real SfM, DepthEye registration, 3DGS training, model-backed viewer generation, or GUI import was performed in Codex; synthetic HTML construction remains covered by tests.

The structural export and synthetic tests do **not** establish Spirula GUI reopening, real LiDAR registration quality, training success, model fidelity, or browser/WebGPU compatibility. No actual playground model has been generated in this phase.

## Local execution runbook

### Environment preparation

From the canonical checkout, use the repository's uv-managed Python environment and locked dependencies:

```bash
cd /Users/homan/repo/exhibition-archive
uv venv .venv --python 3.12
uv sync --locked --python .venv/bin/python
ENGINE="/Users/homan/repo/exhibition-archive/.venv/bin/exhibition-archive"
```

To prepare the optional registration extra explicitly (the CLI will also offer its locked uv repair during an authorized local run):

```bash
uv sync --locked --python .venv/bin/python --extra lidar-registration
.venv/bin/python -c 'import numpy, open3d; print(numpy.__version__, open3d.__version__)'
```

On this machine Open3D is installed but its import reports that `libusb-1.0.0.dylib` is missing. A Python sync will not fix that native link; satisfy it separately (for example `brew install libusb`) and rerun the import check. The Python bootstrap does not install Homebrew packages. The PLY remains optional if Open3D is unavailable.

The Python package installs Pillow from `pyproject.toml`. Fresh reconstruction requires the Spirula Studio CLI discoverable as `spirula` on `PATH` or in `/Applications/Spirula Studio.app/Contents/MacOS/spirula`. The profile uses `--quality high`, one 30,000-iteration 3DGS pass, divisor `1`, no `max_image_size`, `--scene-center none`, automatic device selection, a full checkpoint every 5,000 steps, and latest-only checkpoint retention. `--scene-center none` preserves COLMAP coordinates so the candidate PLY stays in the frame used by the opening camera and navigation proposal. The Engine passes `--device auto` and has no separate Metal environment switch; confirm the device Spirula selects in its training log before approving the stage.

HEIC/HEIF requires `heif-convert` from libheif and the Pillow/LittleCMS validation path. If HEIF is present and the converter is missing, dry-run reports a blocker; install libheif (for example `brew install libheif`) and rerun preflight. Open3D is optional. When the selected PLY requests registration and its locked Python dependencies are absent or version-incompatible, the explicitly authorized interactive local run uses `uv sync --locked --extra lidar-registration`. The current development machine's Open3D import reports a missing `libusb-1.0.0.dylib`; the CLI reports this as an optional import failure and does not attempt a Python reinstall, because uv cannot install native Homebrew libraries. Resolve it separately (for example `brew install libusb`) if registration is wanted. Otherwise alignment is skipped with diagnostics and photo-only processing continues. The Engine does not require Open3D to produce the first visual model.

The generated resource policy requests a 10 GiB free-space floor for SfM and a 20 GiB training floor plus a 20 GiB checkpoint-replacement reserve. The reserve basis remains `NOT_RECORDED`; inspect current free space and the exact authorization plan, then record an appropriate operator-provisioned or measured basis before approving training if required by the local operating policy. These are configured floors, not predictions of total output size.

### Preflight

Set the actual absolute paths and an operator-supplied stable project identifier:

```bash
INPUT="/Users/homan/repo/nam-shan"
OUTPUT="/Users/homan/repo/nam-shan-output-first-visual-cli"
PROJECT_ID="playground-20261105"
"$ENGINE" run --input "$INPUT" --output "$OUTPUT" --project-id "$PROJECT_ID" \
  --lidar auto --scene-type auto --profile first-visual --until visitor --dry-run --json
```

Use `--scene-type outdoor` if the operator has established that open-air coverage should be the geometry assumption. `auto` deliberately keeps the enclosure type unclassified; neither mode invents physical boundaries.

### Full execution

After reviewing a `READY` or `READY_WITH_OPTIONAL_WARNINGS` dry-run and confirming the output directory is appropriate, run one command:

```bash
"$ENGINE" run --input "$INPUT" --output "$OUTPUT" --project-id "$PROJECT_ID" \
  --lidar auto --scene-type auto --profile first-visual --until visitor \
  --interactive-authorization
```

In the local Terminal, the CLI asks for separate exact-plan approvals before fresh SfM and fresh training. It automatically proceeds through successful stages and offers the guided component review if technical SfM validation needs an explicit decision. It stops at the first visual review gate. `--interactive-authorization` is required because costly-stage approval must be explicit and stage scoped. The chosen output is new and separate; this command does not delete or overwrite `/Users/homan/repo/nam-shan-output`.

The exact full execution command is:

```bash
cd /Users/homan/repo/exhibition-archive
ENGINE="/Users/homan/repo/exhibition-archive/.venv/bin/exhibition-archive"
INPUT="/Users/homan/repo/nam-shan"
OUTPUT="/Users/homan/repo/nam-shan-output-first-visual-cli"
PROJECT_ID="playground-20261105"
"$ENGINE" run --input "$INPUT" --output "$OUTPUT" --project-id "$PROJECT_ID" \
  --lidar auto --scene-type auto --profile first-visual --until visitor \
  --interactive-authorization
```

### Resume and review stops

For an interrupted local stage or a corrected recoverable blocker, first run status and inspect the reason, then resume:

```bash
"$ENGINE" status "$OUTPUT/.engine"
"$ENGINE" resume "$OUTPUT/.engine" --profile first-visual --until visitor \
  --interactive-authorization
```

The command reuses successful stages whose fingerprints and artifacts remain valid. A resumed fresh backend attempt needs a new exact-plan approval. Checkpoint files are preserved, but Engine resume is not a guarantee of optimizer-state restoration; the backend's `--resume` behavior must be separately configured and verified.

If status reports SfM `NEEDS_REVIEW`, inspect `work/sfm-validation.json` and the sparse reconstruction, then record a component-scoped decision only if justified. For a reviewer-approved experimental training scope, the existing command shape is:

```bash
"$ENGINE" sfm-review "$OUTPUT/.engine" --component COMPONENT_ID \
  --decision accept-experimental-training --reviewer "REVIEWER" \
  --rationale "REASONED COMPONENT REVIEW" --limitation "UNRESOLVED LIMITATION"
"$ENGINE" resume "$OUTPUT/.engine" --profile first-visual --until visitor \
  --interactive-authorization
```

Replace `COMPONENT_ID` with the component directory reported by the validation. An invalid `SFM_INVALID` reconstruction cannot be accepted. If a process may still be alive, do not resume until it is confirmed inactive and reconciled using the existing `reconcile-interrupted` procedure. Failed attempts are bounded; after the limit, correct the cause and use the existing `reset-stage` command.

On an SfM component review stop, use the interactive prompt when running with `--interactive-authorization`, or record a decision using the documented `sfm-review` command shape above. For details, run:

```bash
"$ENGINE" sfm-revalidate "$OUTPUT/.engine"
"$ENGINE" status "$OUTPUT/.engine"
```

To retry an optional LiDAR import after satisfying a native library requirement, rerun the same `resume` command. Successful SfM outputs remain cacheable; only stages whose input fingerprints or optional dependency evidence changed become eligible to rerun. Training still requires a fresh separate exact-plan approval after the SfM component decision.

### Monitoring and expected outputs

Pipeline state and recent run summary:

```bash
"$ENGINE" status "$OUTPUT/.engine"
"$ENGINE" logs "$OUTPUT/.engine" sfm --lines 80
"$ENGINE" logs "$OUTPUT/.engine" training --lines 80
```

Persistent workflow state is in `$OUTPUT/.engine/workflow.sqlite3`. Environment preflight is `$OUTPUT/.engine/environment-preflight.json`. Stage attempts, exact commands, checkpoints, and backend logs are under `$OUTPUT/backend-runs/sfm/` and `$OUTPUT/backend-runs/training/`. LiDAR reports are `$OUTPUT/lidar/inspection.json` and `$OUTPUT/lidar/alignment-report.json`. Navigation proposals and their review HTML are under `$OUTPUT/navigation/proposals/`. The source directory remains read-only.

Live progress is printed to stderr and appended to `$OUTPUT/logs/pipeline-progress.jsonl`. The SfM and training attempt directories each retain `progress.jsonl`, backend logs, and checkpoint state. Use `--progress-format jsonl` for structured stderr events or `--progress-format off` to suppress console progress while keeping the event files.

After a successful first-visual stage, `status` reports the versioned page paths. Expected deliverables are:

- Candidate Gaussian PLY: `$OUTPUT/first-visual/revision-NNNNNN/visitor/assets/exhibition.ply` (a private copy; the original candidate/checkpoints remain under `$OUTPUT/backend-runs/training/`).
- Reviewer: `$OUTPUT/first-visual/revision-NNNNNN/reviewer/index.html`.
- Visitor: `$OUTPUT/first-visual/revision-NNNNNN/visitor/index.html`.
- Navigation proposal/review: `$OUTPUT/navigation/proposals/navigation-proposal-vNNNNNN.json` and matching `navigation-review-vNNNNNN.html`, or an explicit unverified fallback.
- LiDAR inspection/alignment: `$OUTPUT/lidar/inspection.json` and `$OUTPUT/lidar/alignment-report.json`.
- Spirula dataset: `$OUTPUT/sfm/spirula-studio-export/revision-<fingerprint>/`, containing `images/`, `sparse/0/`, and `manifest.json`. The manifest reports structural validation and `gui_reopen: NOT_GUI_VERIFIED`.
- Summary: `$OUTPUT/reports/first-visual-summary.md`.

Successful completion is reported by the run as `AWAITING_VISUAL_REVIEW`; the first-visual stage details contain Reviewer/Visitor paths and local validation status. This is not approval to publish.

### Spirula Studio dataset check

The reported missing-image behavior is consistent with opening the sparse reconstruction directory by itself: the Engine stores effective SfM photos and backend sparse outputs separately, while Spirula's documented COLMAP dataset convention uses a dataset root containing `images/` and `sparse/0/`. The new export puts the exact registered image bytes beside the unchanged reconstruction. Its manifests validate the structure, names, camera mapping, dimensions, hashes, and relocation; the GUI itself still needs a manual check. See the [Spirula Studio viewer dataset format](https://github.com/harry7557558/spirula-studio/blob/master/viewer/README.md).

After the export stage completes, open the packaged folder without rerunning SfM:

```bash
STUDIO_DATASET="$(find "$OUTPUT/sfm/spirula-studio-export" -maxdepth 1 -type d -name 'revision-*' -print -quit)"
test -n "$STUDIO_DATASET" && spirula "$STUDIO_DATASET"
```

Confirm that all registered photos resolve and that the sparse cameras/points are visible. Do not save over the Engine source reconstruction. GUI reopening has not been tested in this development session.

### Local HTTP viewer

Serve the output root (do not open the pages with `file://`):

```bash
python3 -m http.server 8000 --bind 127.0.0.1 --directory "$OUTPUT"
```

Read `reviewer_html` and `visitor_html` from `"$ENGINE" status "$OUTPUT/.engine"`; for example the current revision uses paths like `first-visual/revision-000001/reviewer/index.html` and `first-visual/revision-000001/visitor/index.html`. Open the matching URLs under `http://127.0.0.1:8000/` while the server is running. Pages must be served over HTTP rather than opened using `file://`.

## CLI output examples

These short snippets show the text and JSONL formats. Values below are formatting examples; they are not measurements from a new playground run.

```text
Exhibition Archive Engine · preflight READY_WITH_OPTIONAL_WARNINGS
Project: playground-20261105
Photos: 77 · input: /Users/homan/repo/nam-shan
uv: AVAILABLE
LiDAR registration dependency: UNAVAILABLE_OPTIONAL
Warning: Open3D import failed because libusb-1.0.0.dylib is missing
Effect: LiDAR registration is skipped; photo-only processing remains available
```

```text
[6/11] sfm: started · RUNNING
[sfm] sfm image extraction: 4/77 images · ETA: calculating · elapsed 00:23
[sfm] sfm mapping: 32/77 images · ETA 318s · elapsed 02:11
[10/11] training: started · RUNNING
[training] training iteration: 5000/30000 iterations · 3.40 it/s · ETA 7352s · loss 0.02
```

`--progress-format jsonl` emits timestamped events to stderr and persists them in `pipeline-progress.jsonl`. `ETA: calculating` is used until two observations provide an actual rate. If the backend emits no supported progress line, heartbeats show elapsed time and say progress is unavailable.

```text
[REVIEW REQUIRED] SfM
Result:
  Registered: 77/77
  Sparse points: 63824
  Mean per-point reprojection error: 1.825 px
  Metric source: recomputed_from_colmap_camera_models_poses_points_and_tracks
  Quality: SFM_VALID_WITH_WARNINGS
Decision required: accept a component for experimental training, reject it, or defer review.
```

Recoverable failures name the affected stage, retain successful caches, and print a resume command. A successful first-visual run ends with `AWAITING_VISUAL_REVIEW`; this is not publication approval.

## Readiness classification

- **Implemented and safe-tested:** locked uv preflight and optional repair behavior, no-side-effect dry-run, photo-only/mixed PLY discovery, Gaussian PLY separation, recoverable invalid LiDAR, streamed PLY reports, COLMAP package export/integrity/relocation, observed log progress parsing, concise scoped SfM review, stage diagnostics, image-only source-preserving preparation, outdoor navigation proposal/fallback, versioned Reviewer/Visitor asset generation, and resume/cache wiring.
- **Implemented but not runtime-validated:** automatic dependency repair on an operator Terminal, actual SfM, real global LiDAR registration, training, real Gaussian PLY rendering, Spirula GUI reopening of the new dataset export, runtime boundary enforcement, full project resume after a real interruption, and browser/WebGPU loading.
- **Deferred to manual local execution:** the playground dataset, backend stages, model inspection, and the first human review.
- **Not implemented:** LiDAR-assisted training initialization, SOG conversion for this preview path, browser overlay toggles for every geometry source, and automatic quality tuning. These are not blockers for photo-led first-visual generation.
