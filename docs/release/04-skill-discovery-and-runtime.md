# 04 - Skill discovery and runtime

## Source and repository discovery scope

The single canonical Skill is `skills/exhibition-archive/SKILL.md`. The repository discovery entry is `.agents/skills/exhibition-archive`, a symlink to `../../skills/exhibition-archive`; its target resolves correctly. The repository-scoped `.agents/skills` location and symlink mechanism match current official Codex Skill guidance: [Build skills](https://learn.chatgpt.com/docs/build-skills). The documented direct invocation surface is `$exhibition-archive` (with `/skills` for discovery in a supported interactive session).

This establishes the supported repository layout and file validity. It does not establish runtime metadata discovery or activation.

## Structural checks

- `quick_validate.py`: PASS, “Skill is valid!”
- `audit_skill_references.py`: PASS, zero issues.
- `package_skill.py`: PASS; produced `/private/tmp/ea-phase3c1-validation/skill-package/exhibition-archive.skill` with `SKILL.md` and four references.
- `format_check.py`: BLOCKED because the validator environment lacks the optional `yaml` module (`ModuleNotFoundError`). No global dependency was installed.
- Canonical source hash at inventory time: `bcb07484697821a61d60f367ea9882e225fc52c1baa80b414e3dde92003f01a9`.

## Runtime diagnosis

The stages must be kept distinct:

| Stage | Result |
|---|---|
| Files exist and link resolves | `MIGRATED_AND_VALIDATED` |
| Skill structure and packaged references | `MIGRATED_AND_VALIDATED` |
| Codex metadata discovery in a fresh session | `BLOCKED`; no runtime discovery observation |
| Skill selection and body load | `BLOCKED`; no model turn ran |
| Skill-triggered CLI call and state interpretation | `BLOCKED`; no model turn ran |

The CLI prompt-input diagnostic first rejected the existing global reasoning value `max` as unsupported by installed CLI 0.137.0. Overriding reasoning effort for that command (without editing global settings) advanced the command to `Operation not permitted`. A fresh ephemeral `codex exec` with a model shown by `codex debug models --bundled` (GPT-5.5, reasoning high) failed before inference with `failed to initialize in-process app-server client: Operation not permitted`. The catalog listed GPT-5.5 as supported in API, but no API request or model turn started. A separate prior GPT-6.1-sol attempt returned HTTP 403 for unsupported account/model access. That service error is distinct from the current app-server sandbox failure and from Skill discovery.

No sandbox bypass, global settings edit, or privileged service was attempted. A missing Skill body in prompt-input output was not treated as proof metadata discovery failed.

## Smallest manual verification step

Start a **fresh Codex session rooted at** `/Users/homan/repo/exhibition-archive`. In that session, invoke `$exhibition-archive` and ask: “Use this Skill to inspect the generic synthetic exhibition project at `/Users/homan/repo/exhibition-archive/.local/validation/phase3c1/portability/run-20261029T144952Z/fixture/project-config.json`. Read-only only: report plan and current status; do not run, resume, package, change config, or authorize execution.” Verify the session shows Skill selection/body load, the actual CLI arguments, and a read-only SQLite status matching the CLI result. The project is synthetic and already has recorded state. This is the remaining runtime gate.
