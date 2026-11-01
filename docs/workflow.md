# Workflow lifecycle

The project manifest defines the read-only source folder, output location, optional adapters, viewer metadata, resource policy, and publication policy. The Engine stores stage and review state in workflow.sqlite3 in the chosen workspace.

A typical flow is:

1. Initialize a workspace from a project manifest.
2. Run plan and status to see configured stages and current evidence.
3. Run safe inventory, image-only preflight, and adoption of configured existing artifacts.
4. Review reconstruction quality and candidate model evidence.
5. Generate or adopt navigation, validate regions, portals, floor, ceiling, clearances, and viewer transform, then obtain human approval for the exact candidate.
6. Review artwork identities and the public metadata catalogue.
7. Package from validated current artifacts.
8. Verify integrity and current release gates.
9. Perform browser acceptance and make a separate publication decision.

A run may stop safely at a missing input, invalid or stale artifact, quality warning, expensive-stage authorization gate, or human review gate. Read the returned Engine state before choosing the next action. Do not infer readiness from the requested action or a prior conversation.

Use `storage-report WORKSPACE` to inspect source, derivative, attempt, cache, checkpoint, validated/review-referenced artifact, audit, and final-deliverable storage before a costly stage or package review. It only reports; see [storage lifecycle](storage-lifecycle.md) for retention classes and disk controls.

## Component-scoped SfM review

An SfM result with `SFM_NEEDS_REVIEW` remains in that durable state until it is recomputed or otherwise resolved. A reviewer may record a component-scoped decision without rewriting the stage state:

```bash
export EXHIBITION_ARCHIVE_ENGINE=/absolute/path/to/exhibition-archive/.venv/bin/exhibition-archive
"$EXHIBITION_ARCHIVE_ENGINE" status <project>
"$EXHIBITION_ARCHIVE_ENGINE" sfm-review <project> --component 0 --decision accept-experimental-training \
  --reviewer "Reviewer name" --rationale "Why this component is suitable for an experiment" \
  --limitation "Unresolved coverage or quality limitation"
"$EXHIBITION_ARCHIVE_ENGINE" plan <project> --full-hash
```

The component ID is its relative COLMAP directory name (for example, `0` for `sparse/0`). The command also supports `--decision reject` and `--decision defer`. Every decision requires a reviewer and rationale. Conditional acceptance requires one or more explicit `--limitation` entries.

The durable record uses the existing `review_gates` and `review_history` tables. It records the project/workspace, SfM execution and plan/input identity, stage artifact digest, every component artifact-tree digest, the selected component's registered image names and digest, the original quality warnings and thresholds, the current revalidation findings, the decision/rationale, limitations, timestamp and an audit identity. The history is append-only; the gate exposes the latest decision. Inspection recomputes hashes and stage fingerprints. An acceptance becomes stale when the SfM execution, its artifacts, any component membership, source image inventory or relevant SfM configuration changes.

`accept-experimental-training` permits only an experimental fresh 3DGS training candidate. It does not change SfM `NEEDS_REVIEW` to `COMPLETED`, change thresholds, enable `allow_training_with_warnings`, or accept final model quality, navigation, artwork completeness, archive packaging or publication. Rejection and deferral keep downstream training blocked. An invalid `SFM_INVALID` reconstruction cannot be conditionally accepted.

When multiple components have accepted training scope, configure `integrations.model.sfm_component_id` to select one. Otherwise, the Engine uses the only current accepted component. The exact plan reports the selected component, component digest, registered-view membership digest and input count. Fresh training staging creates `dataset/images/` with verified per-image symlinks for only the selected component's registered images and links the matching COLMAP component under `dataset/sparse/0/`. It does not expose other components or unregistered source images to the backend. The original image directory remains unchanged.

Component acceptance is not a training grant. Fresh training still requires a separate `plan --full-hash` and `authorize --stage training --plan-id <exact-id>`. The Engine then rechecks the component acceptance and exact training plan before the one-use grant can be consumed. A scoped run stops after the experimental training stage; navigation, model evaluation, archive packaging and publication remain outside that review scope.

Cache reuse depends on the current inputs, configuration, backend identity, and stage evidence. Configuration changes selectively invalidate dependent stages and approvals. Package is package-only; it does not start fresh SfM or training. Workflow resume is not optimizer-state resume.
