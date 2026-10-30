# Phase 3C.2 — Baseline and remaining gaps

## Repository baseline

- Canonical repository: /Users/homan/repo/exhibition-archive
- Branch and HEAD: main, 224eb46c93a8a55b7c985d242e96d8e4a46192cf
- Initial Phase 3C.2 working tree: clean; no unrelated user changes were present.
- Remote configuration was retained. No push, tag, release, or commit was made during this phase.
- Product version: 0.1.0; 0.1.0-rc.1 remains historical provenance, not a newly issued release.
- Python 3.13.5 was used; the product supports Python 3.11 or newer. The editable import and installed CLI resolve to this repository.

The captured baseline is [repository-baseline.json](../../../.local/validation/phase3c2/baseline/repository-baseline.json); the captured starting HEAD is [git-head.txt](../../../.local/validation/phase3c2/baseline/git-head.txt). The prior migration, install, and direct canonical Skill invocation remain accepted evidence in [installation and CLI verification](../03-installation-and-cli-verification.md) and [Skill discovery and runtime](../04-skill-discovery-and-runtime.md).

## Remaining gaps closed in this phase

- Post-migration browser acceptance now runs in Chrome with its default sandbox enabled.
- Indirect positive and negative Skill selection, safe first use, ambiguous continuation, failed-training recovery, untrusted intake, and publication-gate behavior were observed in fresh Codex sessions.
- Unit, fake-backend, independent-workspace portability, full-hash package-integrity, and six-group historical-asset checks pass.
- A reproducible Phase 3D pilot runbook and an explicit 34-question acceptance handoff are provided in this folder.

## Focused product fixes

The current review found and fixed three browser issues:

1. Camera markers no longer intercept pointer events used to drag coincident navigation boundary vertices.
2. The review dashboard reports an unconfigured artwork review instead of displaying a dead placeholder iframe.
3. Generated review pages use an inline empty favicon so a normal browser visit does not create an optional-resource 404.

The canonical Skill now explains that durable COMPLETED/ADOPTED status can coexist with current BLOCKED_NO_CONFIGURED_ARTIFACT plan readiness.

Changes are limited to [navigation.py](../../../src/exhibition_archive/navigation.py), [review_hub.py](../../../src/exhibition_archive/review_hub.py), [dashboard/index.html](../../../dashboard/index.html), and [skills/exhibition-archive/SKILL.md](../../../skills/exhibition-archive/SKILL.md). The initial commit remains unchanged; these modifications are an uncommitted reviewable working-tree diff.

## Remaining product and release limits

- No independent real exhibition has been processed in this phase. Synthetic fixtures and the historical Chuen Lung asset check do not prove cross-exhibition quality.
- Automatic architectural plane/wall inference is incomplete; generated navigation remains a proposal that needs human review.
- Model fidelity, artwork associations, image and artwork rights, access rules, and public/private policy require exhibition-specific review.
- A static Publish Package is implemented. A self-contained Preservation Package is not.
- Public-release approval is correctly NEEDS_REVIEW. This is an intentional human decision gate, not a technical verification failure.

No actual SfM, Gaussian training, public approval, deployment, push, or tag was performed.
