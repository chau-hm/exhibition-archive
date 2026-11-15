# WS-A: Training Profiles and Host Preflight

## Scope and source

This selective forward-port references `codex/phase4a-workflow-simplification` at `3ff015863bb9de355c230e486d725580bd29e124`, especially its `src/exhibition_archive/profiles.py` and `src/exhibition_archive/host_capabilities.py`. No commit from that branch was merged or cherry-picked.

The Engine adapts the old profile's training parameter values into `training_profiles.py`. It does not port its SfM/workflow defaults, resource-policy defaults, guided bootstrap, or execution orchestration. The old host helper treated a positive GPU inventory check as a gate before training approval; WS-A replaces that behavior with an optional, informational report. The old `guidance.py`, `source_organization.py`, `sfm_review.py`, `sfm_dataset.py`, and `operator_config.py` paths remain out of scope.

## Training profile behavior

`--profile first-visual` continues to select the existing end-to-end workflow. `--training-profile ID@REVISION` selects only a versioned set of training parameter defaults. A new folder-based `run` selects `indoor-exhibition-high-quality@v1` by default. An operator can select that revision explicitly or use `--legacy-compatible-training` when an older Spirula CLI lacks the newer `--view-sampling`, `--cache-images`, or `--load-masks` flags.

The legacy option selects the separate `indoor-exhibition-high-quality-legacy-compatible@v1` profile. It keeps the shared high-quality iteration, resolution, quality, device, and checkpoint settings, while leaving view sampling, image caching, and mask loading to the backend defaults. The normal `indoor-exhibition-high-quality@v1` definition and its effective settings are unchanged; the selected legacy identity is recorded, while effective model values continue to drive existing fingerprints and exact-plan authorization. Delegated backend defaults can vary by Spirula version and are reported by the existing backend-effective configuration record; the legacy profile does not claim those values match the full profile.

For example:

```bash
exhibition-archive run INPUT_DIR OUTPUT_DIR \
  --project-id PROJECT_ID \
  --profile first-visual

exhibition-archive run INPUT_DIR OUTPUT_DIR \
  --project-id PROJECT_ID \
  --legacy-compatible-training

exhibition-archive run PROJECT_WORKSPACE \
  --training-profile indoor-exhibition-high-quality@v1
```

For profile-controlled fields, precedence is explicit project configuration, then the selected profile, then the Engine's existing defaults. Presence is checked by key, so `false`, `0`, empty collections, and `null` are not mistaken for absent settings. `integrations.model` remains authoritative for effective values.

The profile ID and immutable revision, registered defaults, selection source, fields supplied by the profile, explicit overlapping fields, and effective model configuration are recorded in `training_profile`. Reapplying a recorded revision checks that its defaults have not drifted without a revision bump. Fresh training attempt records also capture this provenance beside the Engine's existing effective backend configuration. Profile metadata is not added to stage fingerprints or plan identity; effective model values already participate in training fingerprints and exact-plan authorization.

Projects loaded from disk are unchanged unless `--training-profile` or `--legacy-compatible-training` is explicitly supplied. Applying a profile fills absent profile fields and can refresh fields previously supplied by a profile when those values remain unedited. When switching to a profile that omits an unchanged field previously supplied by the old profile, that value is released back to the backend default; explicit or edited values remain. `exhibition-archive config set integrations.model.FIELD VALUE` marks a profile-controlled value as an explicit override. Effective changes use the existing configuration persistence and selective invalidation path. A metadata-only selection-source update does not invalidate stages.

The registry contains `indoor-exhibition-high-quality@v1` and `indoor-exhibition-high-quality-legacy-compatible@v1`. A behavior change within either identity requires a new revision; unsupported revisions fail clearly. These profiles do not select the execution mode, workflow stages, project ID, input paths, backend, or resource safety limits.

## Host capability preflight

Use `exhibition-archive plan PROJECT --host-preflight` to append read-only machine observations. The same `--host-preflight` option is available on `run` and `resume`; in the folder-based path it is included in existing preflight output, including JSON output. It reports platform and architecture, the existing Python/backend readiness observations, optional LiDAR dependency readiness, accelerator inventory, and free/total disk space at the output location or its nearest existing parent.

Capability statuses are `available`, `unavailable`, `unknown`, and `not-required`. Backend machine status and its diagnostic reason are carried separately; a structured `UNAVAILABLE_OR_UNSUPPORTED` result is reported as `unavailable` with the reason in its detail. macOS Metal inventory and Linux NVIDIA/Vulkan inventory can confirm a host capability. Missing, failed, unsupported, or ambiguous accelerator probes report `unknown`; the report does not infer a working accelerator from the OS, architecture, package presence, or an executable alone. Even when host inventory reports `available`, selected-backend initialization remains `unknown` until the backend executes. If the current plan does not require training, acceleration is `not-required`.

Disk measurements have no added minimum threshold and do not block execution. Missing tools or host warnings do not create review decisions, modify configuration, install packages, authorize a stage, select another backend, or change training parameters. Existing Engine dependency checks, planning, SfM review, and exact-plan authorization remain authoritative.

## Current-main integration and compatibility

| Older-branch behavior | WS-A decision |
| --- | --- |
| Versioned high-quality training defaults | Adapted as immutable `indoor-exhibition-high-quality@v1` tunable values. |
| Set-only-missing profile application | Adapted at the current folder-config and explicit project-update boundaries. |
| Profile-selected SfM mode, image path, run type, and resource limits | Rejected; current workflow and Engine resource policy remain authoritative. |
| Hard GPU verification before guided training authorization | Rejected; optional host inventory is informational and cannot issue or block authorization. |
| Guided bootstrap/source organization and alternate orchestration | Rejected; current CLI, config validation, planner, stage runner, approvals, and resume path remain in use. |

`cli.py` resolves defaults before existing configuration validation and planning. `workflow.py` continues to create the training plan, enforce accepted SfM scope, require exact-plan authorization, launch the existing training stage, and record backend-effective configuration. `cmd_config_set` and profile updates share the current stage fingerprint comparison and navigation approval invalidation path. No new workflow stage or authorization path is introduced.

## Validation and limitations

Focused profile, host-probe, CLI, and mocked legacy-backend coverage passed: 46 tests with 3 subtests. The broader Python suite passed 263 tests with 2 baseline fixture tests deselected and 13 subtests. The deselected navigation/preview tests were reproduced as failures on untouched canonical `main` at `5ef152012b8b9fc14e1082b0295097b360accbf1`: one fixture omits `source`, and the other expects `UNVERIFIED` while main returns `NOT_CONFIGURED`. The runs used `KMP_USE_SHM=0 OMP_NUM_THREADS=1` to avoid an intermittent OpenMP shared-memory abort in a publisher fixture subprocess. Project schema JSON parsing, modified-file syntax parsing, and `git diff --check` passed; `uv.lock` did not change.

The WS-A worktree has no `.venv`; tests used the canonical project's existing environment with `uv run --locked --no-sync`. Tests use temporary fixtures and mocked host commands. They do not run real SfM, initialize a GPU backend, train a model, or establish visual quality. Host inventory confirms only the evidence stated by each probe, not Spirula compatibility or successful execution.
