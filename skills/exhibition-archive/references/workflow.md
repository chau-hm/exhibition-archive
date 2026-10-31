# Operator workflow

## Install and find the CLI

The product owns `bin/exhibition-archive`; it runs the checked-out Python package directly and needs Python 3.11 or newer. No Python runtime dependencies are declared. For a shell session, either invoke the absolute path to `bin/exhibition-archive` or add the product's `bin/` directory to `PATH`. `EXHIBITION_ARCHIVE_ENGINE` may hold the executable path for a managed local installation.

The Skill itself is available from the product workspace's `.agents/skills/exhibition-archive` discovery link, which points to the version-controlled `skills/exhibition-archive` source. It does not own or copy Engine state.

## First use

```text
exhibition-archive init /path/to/original/photos --project autumn-gallery --workspace /path/to/work/autumn-gallery
exhibition-archive plan /path/to/work/autumn-gallery
exhibition-archive status /path/to/work/autumn-gallery
```

Use the Engine result to report supported image formats and source count. The originals remain read-only at their registered location. `init` does not start SfM or training. If the manifest is incomplete, explain what optional evidence is missing rather than fabricating it.

When the user asks to prepare or continue a workflow, `run <project>` without an authorization ID can finish safe local intake and then stop at an expensive-backend gate. It does not start fresh SfM/training without a grant. After this safe work, re-read status and `plan --full-hash` so any consent is for the current post-intake plan.

## Continue an existing project

```text
exhibition-archive plan <project>
exhibition-archive status <project>
```

Read SQLite state and stage artifacts. `COMPLETED` means the Engine performed the stage; `ADOPTED` means existing evidence was validated and registered. A valid cache is reused only when the current stage fingerprint and recorded output files still match.

## Fresh backend authorization

```text
exhibition-archive plan <project> --full-hash
exhibition-archive authorize <project> --stage sfm --plan-id <plan_id> --reviewer <user-supplied-name> --rationale <user-supplied-reason>
exhibition-archive resume <project> --authorization-id <returned-id>
```

For fresh training use `--stage training`; it can only be authorized after SfM is accepted. A successful fresh SfM changes the training dependency fingerprint, so a pre-existing training plan is not valid for the new state. The Engine consumes a grant once, after the full-hash plan is checked again, and records its reviewer, rationale, stage, plan and execution ID. Grants expire after 15 minutes. `run` and `resume` without a matching grant return authorization-required state without marking the stage failed. For a new workspace, let `run` finish safe local prerequisites before authorizing; then replan and authorize the exact current plan. An approval does not grant Metal access or override a sandbox.

`package <project>` is restricted to packaging and verification stages with current, valid prerequisite cache entries. It returns a package prerequisite blocker instead of computing missing SfM, training, inventory or review stages.

## Review and publication

`review <project>` starts the configured local review interfaces. `navigation status` reports generated proposal and approval state. `review-model` applies only to a fresh candidate and will not select a smoke-test artifact. Navigation and model approvals are persisted with candidate hashes.

For SfM in `NEEDS_REVIEW`, inspect `status` and current artifacts, then use the supported `sfm-review` operation to record `accept-experimental-training`, `reject`, or `defer` for one relative COLMAP component ID. Include reviewer, rationale, and explicit limitations for acceptance. The decision is artifact-bound and append-only in `review_history`; it does not change the SfM stage status. The Engine rechecks the SfM execution, stage artifacts, all component artifact trees, registered image memberships, source inventory, and configuration. Any mismatch makes the old decision stale.

An accepted component is eligible only for experimental fresh training. Other components, navigation, final model quality, artwork completeness, archive packaging, and publication remain unapproved. Multiple accepted components require explicit `integrations.model.sfm_component_id`. The training plan identifies the selected component and registered-view count; staging creates per-image symlinks only for those exact COLMAP image names plus the matching reconstruction under `sparse/0`. It does not rely on external backends to filter a larger source directory. A separate exact-plan training authorization remains required.

The static site build is local and does not deploy. Before declaring public release readiness, require `publication-review` to bind explicit reviewer decisions to the current package contents. `verify --public-release --full-hash` checks technical package integrity and the current publication gate. If rights or access remain unassessed, report `NEEDS_REVIEW` and do not mark the candidate public-release-ready.
