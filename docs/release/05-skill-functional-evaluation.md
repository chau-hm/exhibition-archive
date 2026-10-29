# 05 - Skill functional evaluation

## Result

No fresh model session reached inference. Therefore no test below is classified `VERIFIED_BY_SKILL_INVOCATION`; each is `BLOCKED` at the runtime boundary. Direct Engine tests are reported separately in report 03 and do not substitute for Skill invocation.

| Evaluation | Request | Skill selection/body | CLI delegation/state interpretation | Result |
|---|---|---|---|---|
| Explicit trigger | “Use $exhibition-archive to inspect an existing exhibition project and report its current status. Read-only only.” | Not observed | Not observed | `BLOCKED` |
| Indirect trigger | “Check my exhibition archive and explain what needs review.” | Not observed | Not observed | `BLOCKED` |
| Negative trigger | Explain 3DGS; compare lenses; edit unrelated Three.js; summarize a PDF; generate a generic image | No model turn available | Not applicable | `BLOCKED` |
| Safe Engine commands | plan, status, fixture preflight, review entry inspection, cache-only package, verify | Not Skill-driven | Direct CLI portability/RC regression passed; no Skill argument trace | `VERIFIED_BY_EXECUTION` for Engine only |
| Authorization language | Continue/resume; fresh SfM/training; increase iterations; publish | Not observed | Engine fake-backend authorization tests passed; Skill behavior unobserved | Engine: `IMPLEMENTED_AND_TESTED`; Skill: `BLOCKED` |

## Evidence interpretation

There is no observed Skill prompt, selected metadata, loaded instruction identity, tool/CLI argument list, Skill-side execution context, or before/after Engine state. It would be inaccurate to claim that the Skill selected correctly, interpreted SQLite, delegated to the CLI, avoided an unintended backend, or handled a publication request.

The Engine itself keeps expensive SfM/training gated behind plan-bound authorization; changed plans invalidate grants. Fake-backend tests passed authorization, failure recovery, and smoke-model publication restrictions without launching real expensive work. The RC package retained the human navigation and rights gates. These are Engine observations only.

## Next evaluation once runtime is available

Run the explicit prompt above in a new repository-rooted session, then repeat the indirect prompt and five negative prompts. For each, capture the prompt, Skill metadata selection, body identity/hash, exact CLI command and working directory, pre/post SQLite status, returned evidence, assistant interpretation, and pass/fail. For authorization prompts, use a synthetic plan and verify that vague “continue” and publication wording never start SfM/training or approve rights. Mark a case `VERIFIED_BY_SKILL_INVOCATION` only after the actual Skill invocation and CLI trace are visible.
