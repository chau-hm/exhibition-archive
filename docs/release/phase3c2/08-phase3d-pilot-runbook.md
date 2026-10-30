# Phase 3D — Independent real-exhibition pilot runbook

## Purpose and boundary

Use this runbook for one supervised independent exhibition. The goal is to measure whether a new photo folder can become a high-quality 3DGS archive, reviewed navigation, and a deployable static website with minimal necessary human intervention.

Do not use Chuen Lung data as the pilot. Do not treat this Phase 3C.2 synthetic browser geometry as architectural evidence. Do not start any backend until the operator approves the current exact plan. A local package is not a public deployment; a static Publish Package is not a self-contained Preservation Package.

## Before intake

Collect and record:

- A read-only original photo folder for an independent exhibition.
- A stable exhibition/project ID and a new external workspace, outside the source folder.
- The operator/reviewer responsible for reconstruction and the curator responsible for content.
- Access policy and the intended public/private boundary.
- Available artwork inventory and source/credit/rights information.
- Optional validated existing SfM/PLY artifacts and their provenance.
- Optional known architectural constraints or survey references.
- A decision about where long-term source, database, logs, and checkpoints will be preserved; the current product does not bundle a complete Preservation Package.

Agree in advance on project-specific acceptance thresholds for registration, model fidelity, navigation geometry, browser loading, and delivery size. The Engine's synthetic acceptance results do not set quality thresholds for a real venue.

## Install and initialize

Use Python 3.11 or newer and a project-local environment. Do not require global installation.

    cd /path/to/exhibition-archive
    python3 --version
    python3 -m venv .venv
    .venv/bin/python -m pip install -e .
    .venv/bin/exhibition-archive --version

Use the canonical repository Skill at .agents/skills/exhibition-archive. Choose the ID and workspace with the operator, confirm both paths, then initialize:

    EA=/path/to/exhibition-archive/.venv/bin/exhibition-archive
    PHOTOS=/path/to/independent-exhibition/photos
    WORK=/path/to/work/independent-exhibition
    "$EA" init "$PHOTOS" --project independent-exhibition --workspace "$WORK"
    "$EA" plan "$WORK"
    "$EA" status "$WORK"
    "$EA" run "$WORK"

init registers originals as read-only and does not run reconstruction. run without an authorization ID may finish local inventory, image-only preflight, and other safe ready work; it must stop at a fresh SfM/training authorization gate. Read the resulting status and plan. Record source counts/formats, duplicate/low-resolution warnings, unknown capture or region properties, and each completed/adopted/blocked stage. Do not turn unknown measurements into positive claims.

If a validated SfM/model is supplied, review its provenance and let Engine validation decide whether it can be adopted. Adoption is not equivalent to fresh computation.

## Fresh SfM and training

For each costly stage, use a separate approval round:

1. Read plan for the workspace with --full-hash and read status after safe intake.
2. Present the operator the exact stage, plan ID, normalized command/backend/version, input folder and image count, output workspace, iteration/timeout/resource limits, cache/reuse result, quality gates, and cancellation/recovery behavior.
3. Stop unless the user explicitly approves that named stage and exact plan.
4. Record that user-supplied review and execute the one-use grant:

       "$EA" plan "$WORK" --full-hash
       "$EA" authorize "$WORK" --stage sfm --plan-id PLAN_ID --reviewer OPERATOR --rationale "Approved the displayed SfM plan and limits."
       "$EA" resume "$WORK" --authorization-id GRANT_ID

Replace placeholders only with values returned by the current plan/authorization. Grants are one use, expire after 15 minutes, and become stale when relevant inputs or configuration change. Never retry automatically. For retry, inspect status, stage logs and failure type; correct the cause, establish a new bounded retry window if required, re-plan, and obtain fresh approval.

After SfM, inspect registration and quality evidence, then review the new training plan. SfM changes downstream inputs; training requires its own current plan and separate approval. Use the same plan --full-hash, authorize --stage training, and resume --authorization-id sequence. Do not claim that workflow resume restores optimizer state.

For training, the command sequence is:

    "$EA" plan "$WORK" --full-hash
    "$EA" authorize "$WORK" --stage training --plan-id PLAN_ID --reviewer OPERATOR --rationale "Approved the displayed training plan and limits."
    "$EA" resume "$WORK" --authorization-id GRANT_ID

## Model and navigation review

Review model identity and fidelity before selecting a candidate. Record the exact artifact hash, reviewer, rationale, visible defects, and any exclusion. A smoke-test model must never be selected for a public candidate. For a fresh eligible candidate, record its decision against the hash:

    "$EA" review-model "$WORK" --candidate-sha256 CANDIDATE_SHA256 --decision approve --reviewer CURATOR --rationale "Reviewed fidelity and selected this candidate."

Generate navigation from validated evidence. Treat regions, camera poses, floors, ceilings, boundaries, and portals as a proposal. Review the map and browser experience with a curator or venue representative:

- Check that rooms and connections correspond to the real venue.
- Inspect concave boundaries, floor/ceiling limits, camera freedom, and portal direction/policy.
- Correct proposed geometry and revalidate; approval must bind to the exact artifact hash.
- Record unresolved architectural uncertainty. Automatic wall/floor/ceiling inference is incomplete and no manually constructed camera trajectory is required by this pilot.

The local navigation workflow is:

    "$EA" navigation generate "$WORK"
    "$EA" navigation status "$WORK"
    "$EA" review "$WORK"
    "$EA" navigation validate "$WORK"

After any correction, validate the current proposal again. Record navigation approval only after an authorized reviewer accepts the exact artifact:

    "$EA" navigation review "$WORK" --decision approve --reviewer CURATOR --rationale "Reviewed the current navigation artifact and portal policy."

Review artwork identity/association and metadata against the supplied inventory. Keep uncertain matches unconfirmed. Decide explicitly whether an empty catalogue is acceptable; do not imply that technical package success confirms an artwork catalogue.

## Rights, package, and browser acceptance

Before a public candidate is approved, record separate human decisions for model selection, navigation, metadata, image rights, artwork rights, access policy, and public/private data policy. Technical hash validity does not establish legal rights. Only after each named reviewer has made those decisions may the authorized reviewer record the exact candidate's publication review:

    "$EA" publication-review "$WORK" --decision approve --reviewer PUBLISHER --rationale "Approved the reviewed package and listed rights/access decisions." --model-selection accepted --navigation-approval approved --artwork-metadata confirmed-only --image-rights approved --artwork-rights approved --access-policy approved --public-private-policy approved

Build and verify locally:

    "$EA" package "$WORK" --full-hash
    "$EA" verify "$WORK" --full-hash
    "$EA" verify "$WORK" --public-release --full-hash

The last command should remain NEEDS_REVIEW until an authorized reviewer approves the exact current package. Inspect the package index, PLY identity, public/private files, credits, path policy, and absence of Debug Fly controls. Run the site under a nested/base-path URL in a supported browser. Record browser/version, startup/load timings, PLY hash/size, console/page/network errors, movement and constraints, guided tour, geometry, and room transitions. Resolve defects and repeat the relevant suite.

If publication is approved for a specific candidate, verify again after the decision. Deployment is a separate external action and requires explicit authorization for the destination; this Engine creates the static package but does not deploy it.

## Pilot measurement sheet

Save raw timings/counts and link each observation to its evidence. Report N/A where the Engine does not provide a measure.

| Area | Measurements |
| --- | --- |
| Automation | Stages completed automatically; stages needing input; adopted/cached stages; failed and retried stages; safe-intake elapsed time |
| Human review | Number and duration of decisions; geometry corrections; artwork corrections; capture insufficiency decisions; unresolved questions |
| Reconstruction | Registered/total images and quality indicators; model completeness/fidelity; artwork detail; white-wall/floor artifacts; ghosting; view-dependent instability |
| Navigation | Region/portal accuracy; floor/ceiling corrections; boundary corrections; constrained/free movement; browser failures |
| Delivery | Package file count/bytes; PLY hash/size; load time; full-hash result; public/private review result; preservation materials retained separately |

Record failures, retries, and operator effort rather than reporting only the successful final run. Compare measurements with thresholds agreed before processing. A single pilot supports a case-specific conclusion, not cross-exhibition generalization.

## Closeout

Deliver the operator a status summary, evidence index, model/navigation/artwork decisions, known limitations, package identity, rights/access decisions, browser results, and a recommendation to proceed, correct, or stop. Preserve originals unchanged. Keep logs, state database, checkpoints, and source media under the agreed preservation plan. Do not describe a Publish Package as a Preservation Package.
