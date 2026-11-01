# Fresh SfM and training authorization

Fresh SfM and fresh training are expensive operations. Each must be approved separately for the exact current full-hash plan.

Before asking for approval:

1. Run plan WORKSPACE --full-hash.
2. Present the stage, plan ID, normalized command, backend and version, input directory and image count, output location, iteration cap, timeout, disk thresholds, reuse result, quality gates, and cancellation/recovery limits.
3. Confirm the operator understands whether this will use the configured device and resources.

After explicit approval, record the named reviewer and rationale with authorize WORKSPACE --stage sfm|training --plan-id ID --reviewer NAME --rationale TEXT. The Engine returns a one-use authorization ID with a 15-minute expiry. Use that ID only with the approved operation. If input, config, backend, or resource plan changes, approval is stale. SfM output that changes the downstream training plan requires another review and approval.

A vague request to continue or resume is not authorization. Retries and interrupted expensive stages require a new current grant. Do not issue an authorization on the user's behalf or run a backend during safe intake. Do not disable the Codex sandbox or claim that an Engine consent record proves host GPU permission. If the active host cannot use the selected device, stop and present the plan for execution in an explicitly supported operator environment.

Fresh training requires explicit positive values for the iteration cap, timeout, training free-disk floor, and checkpoint replacement reserve. A reserve can be explicitly operator-provisioned before the first run or based on measurements; a prior measurement is not required. Set `checkpoint_replacement_reserve_basis` to `OPERATOR_PROVISIONED` or `MEASURED` to record that choice. For Lumo, the proposed reserve is 8 GiB (`8589934592` bytes), operator-provisioned and not measured. At launch, free space must cover the 40 GiB runtime floor plus the configured 8 GiB reserve (48 GiB total). This reserve is an explicit safety budget that accounts for temporary coexistence of old and new full checkpoints; it is not a size forecast or guarantee about backend output size.

During training, the Engine checks the output filesystem at each poll and requests a process-group SIGTERM if free space falls below the floor; its bounded shutdown may escalate to SIGKILL if the process does not exit. It never deletes a checkpoint. After a disk-triggered stop, the attempt record lists candidates observed before and after shutdown and states whether a new or changed candidate appeared. Detection uses path, size, and modification time only: checkpoint validity and resumability are not verified. If SIGKILL was required, candidate validity is explicitly unknown. Keep the attempt and inspect backend evidence before any recovery attempt.

Spirula’s installed help documents `--save-full-checkpoint`, latest-only retention, and a `--resume` option. The Engine can resume only when `model.resume_from` explicitly names an existing run output directory inside this project’s `output/backend-runs/training` tree. The full-hash plan includes the discovered checkpoint candidate hashes and the command targets that same output run with `--resume`. The Engine refuses in-place resume when that run contains a model PLY, preserving candidate and reviewed model files. Engine cancellation and retries never resume automatically. Spirula decides which checkpoint to load; its optimizer-state restoration and checkpoint replacement behavior remain untested until a separately approved backend execution. If a process is force-killed during checkpoint writing, checkpoint validity is not guaranteed; preserve the attempt and inspect backend evidence before any recovery attempt.

Latest-only pruning is delegated to Spirula. The Engine does not prune older or current checkpoint files, and cleanup candidates in `storage-report` are informational only. Before authorization, verify the plan’s disk thresholds against the host’s current filesystem and the configured operator-provisioned or measured reserve. Diagnostic smoke models are not publishable candidates.
