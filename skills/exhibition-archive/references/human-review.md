# Human review decisions

Ask for a separate, specific decision for each consequential gate. Approval of one stage never implies approval of another.

| Gate | Evidence to show | Durable action |
|---|---|---|
| Candidate model selection | PLY hash, validation, run type, limitations and views | `review-model` for a fresh candidate |
| Navigation | exact proposal hash, transform status, each region's boundary/floor/ceiling, portals and provisional poses | navigation review UI and `navigation review` |
| Artwork identity | source evidence and confidence/uncertainty | configured Artwork Review; publish confirmed records only |
| Publication rights | exact package candidate, image/artwork rights, access and public/private policy | `publication-review`; reviewer and rationale required |

An approval generated for automated testing must say `TEST_ONLY` in the test workspace and must never be copied into a real project's state. Rights values such as `UNASSESSED_PROJECT_SPECIFIC_DECISION_REQUIRED` are blockers. Do not turn a validation fixture, adopted historical model, inferred ceiling or empty review file into curator approval.

The Engine records a local approval statement. It cannot determine whether the reviewer has legal authority to grant rights or whether a venue's legal requirements are satisfied. A public release decision remains the reviewer's responsibility.
