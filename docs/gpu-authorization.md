# Fresh SfM and training authorization

Fresh SfM and fresh training are expensive operations. Each must be approved separately for the exact current full-hash plan.

Before asking for approval:

1. Run plan WORKSPACE --full-hash.
2. Present the stage, plan ID, normalized command, backend and version, input directory and image count, output location, iteration cap, timeout, disk thresholds, reuse result, quality gates, and cancellation/recovery limits.
3. Confirm the operator understands whether this will use the configured device and resources.

After explicit approval, record the named reviewer and rationale with authorize WORKSPACE --stage sfm|training --plan-id ID --reviewer NAME --rationale TEXT. The Engine returns a one-use authorization ID with a 15-minute expiry. Use that ID only with the approved operation. If input, config, backend, or resource plan changes, approval is stale. SfM output that changes the downstream training plan requires another review and approval.

A vague request to continue or resume is not authorization. Retries and interrupted expensive stages require a new current grant. Do not issue an authorization on the user's behalf or run a backend during safe intake. Do not disable the Codex sandbox or claim that an Engine consent record proves host GPU permission. If the active host cannot use the selected device, stop and present the plan for execution in an explicitly supported operator environment.

Diagnostic smoke models are not publishable candidates. The Engine does not claim that optimizer/checkpoint state was resumed unless its evidence says so.
