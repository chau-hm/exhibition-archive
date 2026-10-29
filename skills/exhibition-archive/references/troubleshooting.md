# Troubleshooting and recovery

| Engine result | Meaning | Safe next action |
|---|---|---|
| `AUTHORIZATION_REQUIRED` | A fresh SfM/training plan has no active one-use grant, or its plan changed | Run `plan --full-hash`, compare command/resources/inputs and ask for approval of the new `plan_id` |
| `BLOCKED_PACKAGE_PREREQUISITE` | Package-only command found a missing/stale prerequisite | Inspect `status` and `plan`; explicitly resume required stages, approving each fresh backend plan separately |
| `BLOCKED` / `NEEDS_REVIEW` | A dependency or human decision prevents continuation | Inspect stage details, review gate and local review UI; don't reset as a substitute for approval |
| `FAILED` | A stage execution failed | Read `status`, then `logs <project> <sfm|training>` if a fresh backend attempt exists; retain attempt evidence |
| backend unavailable | CLI/capability discovery cannot find the configured executable | Check configured executable and host installation; do not authorize an unresolved command or invent device availability |
| resource limit | Disk/memory/iteration thresholds reject the operation | Present the measured threshold and exact config change needed; a changed plan needs new approval |
| sandbox cannot access Metal/GPU | Current process lacks access to the selected host device | Stop before retry; use a supported, explicitly authorized host CLI context. Never disable sandbox controls inside the Skill |
| stale approval | Input bytes, config, backend/version, command or limits changed | Treat old grant as invalid; inspect the new full-hash plan and request renewed approval |

Workflow resume means the Engine rechecks and reuses durable completed stage outputs. It does not claim that the Spirula optimizer restored state from a checkpoint. A retry creates a new bounded attempt while preserving prior evidence.
