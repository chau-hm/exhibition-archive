# Playground Engine readiness

**Status:** Engine implementation and safe readiness validation complete; the actual playground reconstruction has not been run by Codex. The existing locally produced SfM files were parsed read-only to validate the metric fix. This report does not claim real LiDAR alignment, training completion, or publication approval.

## Implementation summary

The new folder-based `run` path reuses the existing project state, stage DAG, Spirula SfM and training adapters, COLMAP validation, navigation generator, and bundled SuperSplat Visitor runtime. It adds optional input discovery and PLY inspection, scale-aware LiDAR registration and conservative navigation handoff, a private first-visual Reviewer/Visitor package, and a report/status entry for the output. Existing `run WORKSPACE` and photo-only projects retain their prior path.

The requested command stages are `photo_inventory`, optional `lidar_inspection`, `capture_preflight`, optional `sfm_dataset_prep`, `artwork_association`, `sfm`, optional `lidar_registration`, `navigation`, `training`, and `first_visual`. LiDAR inspection and registration have independent fingerprints, so changing an optional PLY does not invalidate cached photo-based SfM or training. A successful run stops with `AWAITING_VISUAL_REVIEW`; it does not launch ROI retraining, parameter sweeps, package publication, or fine-tuning.

Fresh SfM and training still require **separate, one-use approvals** for their current full-hash plans. The `--interactive-authorization` option presents each plan in the local Terminal and asks for its exact authorization phrase, reviewer, and rationale. If the SfM result is `NEEDS_REVIEW`, the Engine stops for a component-scoped SfM decision; training then requires a separate approval. These existing repository gates remain in force.

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

The iteration-specific regression tests are `tests/test_sfm_reprojection_metrics.py`, `tests/test_sfm_review.py`, `tests/test_end_to_end_engine.py`, `tests/test_navigation.py`, and `tests/test_cli.py`. Geometry registration has only synthetic validation in this checkout; the actual DepthEye cloud has not been registered.

## CLI and input behavior

The implemented command is:

```bash
exhibition-archive run --input INPUT_DIR --output OUTPUT_DIR --project-id STABLE_ID \
  --lidar auto --scene-type auto --profile first-visual --until visitor [--dry-run]
```

`--project-id` is operator supplied and required. The input and output must be separate, non-overlapping directories. The command refuses a non-empty output without its own existing Engine project. Resume uses `exhibition-archive resume OUTPUT_DIR/.engine --profile first-visual --until visitor`.

Supported image suffixes follow the existing Engine/backend list. Images remain read-only. Non-HEIF photos are copied byte-for-byte into an image-only workspace directory with hash mappings; HEIC/HEIF uses the existing libheif conversion and validation path. DNG/CR2/CR3/NEF/ARW/RAF/RW2 are reported as unsupported, because installed Spirula help has not established support for them. PLY headers are classified so Gaussian model PLY files are excluded from raw LiDAR selection. A single non-Gaussian PLY is selected; zero, multiple, corrupt, and unsupported candidates produce explicit states, and LiDAR issues do not block photos.

The dry-run scans input names and PLY headers, validates image inventory, backend command support, HEIF converter need, optional registration availability, and bundled Viewer assets. It captures the selected training preset's installed help defaults and explicit command overrides, including effective resolution divisor and coordinate-frame setting. It creates no output directory, project manifest, SQLite state, cache, or image copy. Full PLY vertex inspection and reconstruction/training work are deferred until the non-dry run.

## LiDAR behavior and current limits

The Engine records point count, coordinate bounds, PLY encoding and properties, RGB/normals, invalid values, sampled outlier diagnostics, and header-declared units when present. Units otherwise remain unknown. It streams point records and writes a bounded preview rather than retaining the input cloud in memory.

After SfM, optional Open3D FPFH/RANSAC registration is initialized after independent point-cloud normalization, estimates a 7-DoF similarity transform, then refines it with trimmed mutual-nearest-neighbor correspondences. It checks bidirectional overlap, correspondence support, degeneracy, and normalized residuals. Each execution writes `lidar/registrations/alignment-execution-NNNNNN/alignment-report.json` and its transformed preview; `lidar/alignment-report.json` is the current report copy. Weak or unavailable registration is rejected. Source LiDAR is never passed to COLMAP or converted into Gaussian attributes.

An accepted transform is eligible for navigation diagnostics only when the current SfM component has a separate `sfm-geometry-review` decision. Navigation remains a proposal: it combines accepted aligned LiDAR plane evidence with the camera-pose-first coverage footprint, labels any LiDAR ground-plane candidate as unconfirmed, and retains the independent navigation approval gate. The current training path remains photo-only. Reviewer copies the raw and aligned previews, the current alignment report, the navigation proposal, and camera/SfM diagnostics; Visitor receives no LiDAR controls.

Open3D is an optional dependency, declared by `.[lidar-registration]`. The Open3D 0.20 documentation lists macOS arm64 and Python 3.12 support, and PyPI publishes a matching macOS arm64 wheel ([installation docs](https://www.open3d.org/docs/latest/getting_started.html), [0.20.0 wheel files](https://pypi.org/project/open3d/0.20.0/#files)). Install it into the project virtual environment before resuming if LiDAR registration is desired. The registration-stage fingerprint includes the installed Open3D version, so installing the dependency invalidates a prior optional-registration result and makes that stage eligible to rerun without invalidating SfM or training. NumPy and Open3D are absent from the current project virtual environment; no package was installed and no real registration was run.

### SfM reprojection metric correction (2026-11-06)

The local reconstruction's `points3D.bin` has 63,824 point-error scalars, all `0.0`. Those fields were not a usable reprojection statistic for this Spirula output. The Engine had averaged them and emitted `REPROJECTION_ERROR_ZERO_OR_UNINFORMATIVE`, although the backend log separately reported the mean and median **per observation**. The parser now recomputes residuals from COLMAP camera models, poses, 3D points, 2D observations, and reciprocal tracks; it reports per-point and per-observation statistics separately. The backend log statistic is kept separate and is accepted only when observation count and recomputed mean/median agree to the log's three-decimal precision. A genuinely zero recomputation remains a valid zero; missing or unprojectable data is reported as unavailable.

Read-only parsing of the saved artifact at `backend-runs/sfm/20261105T152542Z-execution-0007/workspace/sparse/0` measured 77/77 registered images, 63,824 points, and 201,005 track observations. The recomputed mean of per-point mean residuals is `1.825250 px`; observation-weighted mean and median are `1.999459 px` and `1.784009 px`. Spirula's `1.999 px` / `1.784 px` / `201005 observations` summary independently verifies within `0.0011 px`. Revalidation passes the existing 2.0 px per-point threshold with no current warnings. The original `sfm-validation.json`, project database, point cloud, and backend log were not modified. These values establish parser behavior on the existing COLMAP output; they do not establish training suitability or LiDAR alignment.

Use `sfm-revalidate` to print the current artifact-bound result without rewriting the historical report or opening SQLite for writes. This command was run successfully against the existing workspace in this implementation phase. The separate training acceptance remains required because the durable SfM stage is still `NEEDS_REVIEW`. A new `sfm-geometry-review` records only diagnostic registration and an unapproved navigation proposal; it cannot authorize training or navigation approval.

## Navigation and publishing

Navigation remains camera-pose-first. Indoor proposals retain floor/ceiling and enclosure evidence where supplied. Outdoor proposals derive a horizontal camera-coverage footprint and do not infer physical walls or a ceiling. If accepted aligned LiDAR and the registered camera trajectory support a plane candidate, the Engine records it as `PROPOSED_REQUIRES_HUMAN_GROUND_IDENTIFICATION`; it is never treated as an approved physical ground surface. The bounded candidate finder requires at least 100 sampled LiDAR points and 6 camera centers, screens camera-trajectory planarity, uses 400 deterministic RANSAC trials, a 15-degree alignment band, and a 0.3%-of-scene-diagonal point band. These are proposal-screen settings in arbitrary SfM units, not confidence scores or survey tolerances. If evidence is insufficient, ground and navigation remain unverified. No furniture/equipment collisions, walls, or ceiling are fabricated.

The private first-visual Visitor uses the bundled SuperSplat runtime and the actual validated Gaussian PLY with relative local asset paths. The first-visual banner marks navigation unverified and public release unapproved. The technical Reviewer embeds that Visitor preview and links copied SfM, camera, LiDAR, navigation, training, and log diagnostics when they exist. Navigation and release remain human-reviewed gates. No browser was used in this readiness phase.

## Safe validation results

- `.venv/bin/python -m pytest tests -q`: `131 passed, 3 skipped, 3 subtests passed` after the final code changes.
- `python -m pytest tests -q`: `134 passed` after the final code changes.
- The three `.venv` skips are NumPy-dependent similarity and LiDAR ground-plane synthetic checks; the system Python run includes NumPy and passes those tests.
- Read-only parser check against the saved playground COLMAP directory reproduced 77/77 registered images, 63,824 points, 201,005 observations, mean per-point error `1.825250 px`, and observation mean/median `1.999459` / `1.784009 px`; Spirula's log line agrees within printed precision.
- The actual `sfm-revalidate /Users/homan/repo/nam-shan-output/.engine` CLI completed read-only: original gate `SFM_VALID_WITH_WARNINGS`, current gate `SFM_VALID`, no current warnings, component `0`, 77 registered images, and 63,824 sparse points. No SfM, training, registration, or navigation stage was run.
- Focused tests verify that read-only SfM revalidation does not update the existing validation report, workflow database, or create SQLite WAL sidecars.
- A real CLI dry-run against two temporary 32×24 JPEG fixtures returned `READY`, discovered the local Spirula Studio CLI (`2026.10.16 (65246c5)`), captured 247 effective training settings for the `high` 3DGS preset, and confirmed divisor `1` and `--scene-center none`. It left the output directory absent and did not launch SfM or training.
- No input-discovery pipeline, SfM, LiDAR registration, training, or viewer generation was run. The read-only snapshot did hash the 77 source photos to verify the saved reconstruction's binding; it did not modify them.

The unit and synthetic checks do **not** establish reconstruction quality, real LiDAR registration quality, training success, model fidelity, or browser/WebGPU compatibility. No real model has been exported in this phase.

## Local execution runbook

### Environment preparation

From the canonical checkout, use the repository's uv-managed Python environment and locked dependencies:

```bash
cd /Users/homan/repo/exhibition-archive
uv venv .venv --python 3.12
uv sync --locked --python .venv/bin/python
ENGINE="/Users/homan/repo/exhibition-archive/.venv/bin/exhibition-archive"
```

The Python package installs Pillow from `pyproject.toml`. Fresh reconstruction requires the Spirula Studio CLI discoverable as `spirula` on `PATH` or in `/Applications/Spirula Studio.app/Contents/MacOS/spirula`. The preflight on this host found the latter. The profile uses `--quality high`, one 30,000-iteration 3DGS pass, divisor `1`, no `max_image_size`, `--scene-center none`, automatic device selection, a full checkpoint every 5,000 steps, and latest-only checkpoint retention. `--scene-center none` preserves COLMAP coordinates so the candidate PLY stays in the frame used by the opening camera and navigation proposal. The Engine passes `--device auto` and has no separate Metal environment switch; confirm the device Spirula selects in its training log before approving the stage.

HEIC/HEIF requires `heif-convert` from libheif and the Pillow/LittleCMS validation path. If HEIF is present and the converter is missing, dry-run reports a blocker; install libheif (for example `brew install libheif`) and rerun preflight. Open3D is optional. Without it, the alignment stage records `UNAVAILABLE_OPTIONAL` while photos continue. Install it in the project environment with `.venv/bin/python -m pip install -e '.[lidar-registration]'`; no system/global installation is needed. The Engine does not require Open3D to produce the first visual model.

The generated resource policy requests a 10 GiB free-space floor for SfM and a 20 GiB training floor plus a 20 GiB checkpoint-replacement reserve. The reserve basis remains `NOT_RECORDED`; inspect current free space and the exact authorization plan, then record an appropriate operator-provisioned or measured basis before approving training if required by the local operating policy. These are configured floors, not predictions of total output size.

### Preflight

Set the actual absolute paths and an operator-supplied stable project identifier:

```bash
INPUT="/absolute/path/to/playground-input"
OUTPUT="/absolute/path/to/playground-output"
PROJECT_ID="operator-supplied-stable-id"
"$ENGINE" run --input "$INPUT" --output "$OUTPUT" --project-id "$PROJECT_ID" \
  --lidar auto --scene-type auto --profile first-visual --until visitor --dry-run
```

Use `--scene-type outdoor` if the operator has established that open-air coverage should be the geometry assumption. `auto` deliberately keeps the enclosure type unclassified; neither mode invents physical boundaries.

### Full execution

After reviewing a `READY` dry-run and confirming the output directory is appropriate, run one command:

```bash
"$ENGINE" run --input "$INPUT" --output "$OUTPUT" --project-id "$PROJECT_ID" \
  --lidar auto --scene-type auto --profile first-visual --until visitor \
  --interactive-authorization
```

In the local Terminal, the CLI asks for a separate exact-plan approval before fresh SfM and again before fresh training. It automatically proceeds through successful stages. The run stops at the first visual review gate. `--interactive-authorization` is required because the repository's costly-stage policy does not allow an unattended implicit approval.

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

#### Resume the existing playground SfM without rerunning it

For the existing output, set the repository CLI and workspace, then inspect the revalidation report. This command reads and hashes the saved COLMAP artifacts and backend log; it does not rewrite the original report or SfM stage state:

```bash
cd /Users/homan/repo/exhibition-archive
ENGINE="/Users/homan/repo/exhibition-archive/.venv/bin/exhibition-archive"
OUTPUT="/Users/homan/repo/nam-shan-output"
"$ENGINE" sfm-revalidate "$OUTPUT/.engine"
```

If the current report is `SFM_VALID` and component `0` is the component the reviewer chooses, record separate, scoped decisions. The geometry decision only permits a registration diagnostic and an unapproved navigation proposal. The existing training decision only permits an experimental 3DGS candidate. Neither approves navigation or publication:

```bash
REVIEWER="Your name"
"$ENGINE" sfm-geometry-review "$OUTPUT/.engine" --component 0 \
  --reviewer "$REVIEWER" \
  --rationale "Allow a diagnostic alignment and an unapproved navigation proposal for the revalidated component." \
  --limitation "DepthEye alignment and ground-plane identity remain unverified until the real registration report is reviewed."
"$ENGINE" sfm-review "$OUTPUT/.engine" --component 0 \
  --decision accept-experimental-training --reviewer "$REVIEWER" \
  --rationale "Allow one experimental first-visual training pass from the revalidated component." \
  --limitation "This is not a final-quality, navigation, archive, or publication approval."
```

To enable the optional real alignment, install Open3D only into the project virtual environment and verify its import:

```bash
cd /Users/homan/repo/exhibition-archive
.venv/bin/python -m pip install -e '.[lidar-registration]'
.venv/bin/python -c 'import open3d as o3d; print(o3d.__version__)'
```

Then resume once. The Engine reuses the successful SfM and reruns only stages whose inputs, review scope, or optional dependency fingerprint changed. Registration is part of this resume; no separate registration command is implemented. Fresh training still asks for its own exact-plan approval:

```bash
"$ENGINE" resume "$OUTPUT/.engine" --profile first-visual --until visitor \
  --interactive-authorization
```

If Open3D is left uninstalled, registration records an actionable optional-dependency rejection and the photo-only pipeline continues. If geometry review is omitted, navigation does not consume LiDAR. If training review is omitted, the pipeline stops before fresh training; neither case invalidates the saved SfM result.

### Monitoring and expected outputs

Pipeline state and recent run summary:

```bash
"$ENGINE" status "$OUTPUT/.engine"
"$ENGINE" logs "$OUTPUT/.engine" sfm --lines 80
"$ENGINE" logs "$OUTPUT/.engine" training --lines 80
```

Persistent workflow state is in `$OUTPUT/.engine/workflow.sqlite3`. Stage attempts, exact commands, checkpoints, and backend logs are under `$OUTPUT/backend-runs/sfm/` and `$OUTPUT/backend-runs/training/`. LiDAR reports are `$OUTPUT/lidar/inspection.json` and `$OUTPUT/lidar/alignment-report.json`. Navigation proposals and their review HTML are under `$OUTPUT/navigation/proposals/`. The source directory remains read-only.

After a successful first-visual stage, `status` reports the versioned page paths. Expected deliverables are:

- Candidate Gaussian PLY: `$OUTPUT/first-visual/revision-NNNNNN/visitor/assets/exhibition.ply` (a private copy; the original candidate/checkpoints remain under `$OUTPUT/backend-runs/training/`).
- Reviewer: `$OUTPUT/first-visual/revision-NNNNNN/reviewer/index.html`.
- Visitor: `$OUTPUT/first-visual/revision-NNNNNN/visitor/index.html`.
- Navigation proposal/review: `$OUTPUT/navigation/proposals/navigation-proposal-vNNNNNN.json` and matching `navigation-review-vNNNNNN.html`, or an explicit unverified fallback.
- LiDAR inspection/alignment: `$OUTPUT/lidar/inspection.json` and `$OUTPUT/lidar/alignment-report.json`.
- Summary: `$OUTPUT/reports/first-visual-summary.md`.

Successful completion is reported by the run as `AWAITING_VISUAL_REVIEW`; the first-visual stage details contain Reviewer/Visitor paths and local validation status. This is not approval to publish.

### Local HTTP viewer

Serve the output root (do not open the pages with `file://`):

```bash
cd "$OUTPUT"
python3 -m http.server 8000 --bind 127.0.0.1
```

Read `reviewer_html` and `visitor_html` from `"$ENGINE" status "$OUTPUT/.engine"` to get the current version number, then open each page at `http://127.0.0.1:8000/<path-relative-to-output>`. Keep the server running while inspecting the local assets.

## Readiness classification

- **Implemented and safe-tested:** single folder command, no-side-effect dry-run, photo-only/mixed PLY discovery, Gaussian PLY separation, recoverable invalid LiDAR, streamed PLY reports, full-hash workflow authorization integration, image-only source-preserving preparation, outdoor navigation proposal/fallback, versioned Reviewer/Visitor asset generation, and resume/cache wiring.
- **Implemented but not runtime-validated:** actual SfM, real global LiDAR registration, training, real Gaussian PLY rendering, runtime boundary enforcement, full project resume after a real interruption, and browser/WebGPU loading.
- **Deferred to manual local execution:** the playground dataset, backend stages, model inspection, and the first human review.
- **Not implemented:** LiDAR-assisted training initialization, SOG conversion for this preview path, browser overlay toggles for every geometry source, and automatic quality tuning. These are not blockers for photo-led first-visual generation.
