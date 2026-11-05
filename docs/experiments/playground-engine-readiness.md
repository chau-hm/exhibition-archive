# Playground Engine readiness

**Status:** implementation and safe readiness validation complete; the actual playground reconstruction has not been run. This report describes Engine capability, not a successful reconstruction or a publication approval.

## Implementation summary

The new folder-based `run` path reuses the existing project state, stage DAG, Spirula SfM and training adapters, COLMAP validation, navigation generator, and bundled SuperSplat Visitor runtime. It adds optional input discovery and PLY inspection, global scale-aware LiDAR registration diagnostics, a private first-visual Reviewer/Visitor package, and a report/status entry for the output. Existing `run WORKSPACE` and photo-only projects retain their prior path.

The requested command stages are `photo_inventory`, optional `lidar_inspection`, `capture_preflight`, optional `sfm_dataset_prep`, `artwork_association`, `sfm`, optional `lidar_registration`, `navigation`, `training`, and `first_visual`. LiDAR inspection and registration have independent fingerprints, so changing an optional PLY does not invalidate cached photo-based SfM or training. A successful run stops with `AWAITING_VISUAL_REVIEW`; it does not launch ROI retraining, parameter sweeps, package publication, or fine-tuning.

Fresh SfM and training still require **separate, one-use approvals** for their current full-hash plans. The `--interactive-authorization` option presents each plan in the local Terminal and asks for its exact authorization phrase, reviewer, and rationale. If the SfM result is `NEEDS_REVIEW`, the Engine stops for a component-scoped SfM decision; training then requires a separate approval. These existing repository gates remain in force.

## Changed files

- `src/exhibition_archive/cli.py` — folder `run`, preflight/dry-run, configuration, explicit authorization prompts, and status output.
- `src/exhibition_archive/workflow.py` — independent LiDAR inspection/registration stages, first-visual integration, training-resolution evidence, safe navigation failure continuation, and resume/cache wiring.
- `src/exhibition_archive/lidar.py` — mixed input discovery, raw/Gaussian PLY classification, streamed ASCII/binary PLY inspection, preview generation, bounded global registration, and acceptance diagnostics.
- `src/exhibition_archive/input_prep.py` — provenance-bound, byte-identical image-only copy that leaves originals and EXIF bytes untouched.
- `src/exhibition_archive/navigation.py` — scene-type-aware outdoor coverage proposal without fabricated ceiling/walls; missing ground or vertical evidence remains unverified.
- `src/exhibition_archive/preview.py` — versioned private Reviewer/Visitor output, candidate identity validation, diagnostics, and first-visual summary.
- `src/exhibition_archive/backends.py` — explicit scene-center configuration support, protected from extra-argument overrides; captures selected preset defaults plus command overrides from installed Spirula help in preflight, authorization plans, and training records.
- `templates/visitor/app.js` — allow an unbounded technical preview when optional collision geometry is absent.
- `tests/test_end_to_end_engine.py` — synthetic/mocked discovery, PLY, dry-run, resume, navigation, and preview checks.
- `tests/test_authorization.py`, `tests/test_sfm_review.py`, and `tests/test_training_storage.py` — retained gate and training command fixtures cover the new configuration evidence and scene-coordinate option.
- `docs/installation.md` — entry point to the runbook.

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

After SfM, optional Open3D FPFH/RANSAC registration is initialized in normalized point-cloud scales, then estimates a 7-DoF similarity transform and checks bidirectional overlap and normalized residuals. The report records thresholds and their stated purpose, transform, scale, dependencies, evidence, and reason for rejection. Weak, ambiguous, or unavailable registration is rejected. Source LiDAR is never passed to COLMAP or converted to Gaussian training data.

Even an accepted transform is currently diagnostic only: there is no validated LiDAR consumer in the training or navigation adapters. Reviewer receives the raw and, if accepted, transformed preview and registration evidence; the summary explicitly records `training_consumed=false` and `navigation_consumed=false`. No LiDAR fusion is claimed.

On the validation host, `open3d` is not installed. The dry-run therefore reports registration as optional/unavailable; the real visual pipeline remains available, but an alignment attempt on that environment will be rejected with a dependency reason. This task did not install Open3D or run registration against real data.

## Navigation and publishing

Navigation remains camera-pose-first. Indoor proposals retain floor/ceiling and enclosure evidence where supplied. Outdoor proposals derive only a horizontal camera-coverage footprint and do not infer physical walls or a ceiling. The Engine will not invent a ground plane from camera heights. With missing floor/vertical evidence, the navigation result remains `NAVIGATION_INSUFFICIENT_GEOMETRY`/`NEEDS_REVIEW`; it does not constrain Visitor movement. Navigation generation errors are preserved as an explicit unverified fallback and do not discard a valid trained model.

The private first-visual Visitor uses the bundled SuperSplat runtime and the actual validated Gaussian PLY with relative local asset paths. The first-visual banner marks navigation unverified and public release unapproved. The technical Reviewer embeds that Visitor preview and links copied SfM, camera, LiDAR, navigation, training, and log diagnostics when they exist. Navigation and release remain human-reviewed gates. No browser was used in this readiness phase.

## Safe validation results

- `.venv/bin/python -m pytest tests -q`: **108 passed, 1 skipped, 3 subtests passed**. The skip is the optional NumPy similarity-fit test; NumPy/Open3D are absent from the current `.venv`.
- `.venv/bin/python -m py_compile src/exhibition_archive/*.py` and `git diff --check`: passed.
- A real CLI dry-run against two temporary 32×24 JPEG fixtures returned `READY`, discovered the local Spirula Studio CLI (`2026.10.16 (65246c5)`), captured 247 effective training settings for the `high` 3DGS preset, and confirmed divisor `1` and `--scene-center none`. It left the output directory absent and did not launch SfM or training.
- No playground input directory was searched for or processed in this implementation-only task.

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

HEIC/HEIF requires `heif-convert` from libheif and the Pillow/LittleCMS validation path. If HEIF is present and the converter is missing, dry-run reports a blocker; install libheif (for example `brew install libheif`) and rerun preflight. Open3D is optional and currently absent in the validated environment; without it, alignment is rejected while photos continue. The Engine does not require Open3D to produce the first visual model.

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
- **Not implemented:** LiDAR consumption by training/navigation, SOG conversion for this preview path, browser overlay toggles for every geometry source, and automatic quality tuning. These are not blockers for photo-led first-visual generation.
