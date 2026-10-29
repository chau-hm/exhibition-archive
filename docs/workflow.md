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

Cache reuse depends on the current inputs, configuration, backend identity, and stage evidence. Configuration changes selectively invalidate dependent stages and approvals. Package is package-only; it does not start fresh SfM or training. Workflow resume is not optimizer-state resume.
