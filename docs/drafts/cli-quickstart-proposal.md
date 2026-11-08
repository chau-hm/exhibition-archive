# CLI Quick-Start Proposal

## Prerequisites

- **Operating system:** the repository has no formal supported-OS matrix. It documents macOS host paths and a Linux package path for HEIC conversion; it does not claim Windows support. Confirm the selected backend works on the target host.
- **Python and environment:** Python 3.11 or newer; Python 3.12 is used in the installation guide. Install and manage the project environment with `uv`.
- **Required Python dependency:** Pillow is installed by the project sync.
- **Optional external tools:** Spirula Studio is required only for fresh SfM or training; existing validated COLMAP and model artifacts can be adopted. HEIC/HEIF input requires `heif-convert` from libheif. LiDAR registration uses the optional `lidar-registration` extra (Open3D).
- **GPU / Metal:** training is GPU-intensive. The Engine asks Spirula for automatic device selection and exposes no separate Metal switch; inspect the backend log to confirm the selected device before authorizing training. Intake, planning, and workflows using existing artifacts do not require a training GPU.

## Installation

From a clean checkout with `uv` available:

```bash
uv python find 3.12
uv venv .venv --python 3.12
uv sync --locked --python .venv/bin/python
.venv/bin/exhibition-archive --help
.venv/bin/exhibition-archive --version
```

## Normal CLI Flow

Prepare `/data/gallery/project.json` with the operator-supplied project ID, read-only photo directory `/data/gallery/photos`, and integrations for either existing artifacts or the intended fresh backends. Then:

```bash
ENGINE="$PWD/.venv/bin/exhibition-archive"
WORKSPACE=/data/gallery/workspace

"$ENGINE" init --config /data/gallery/project.json --workspace "$WORKSPACE"
"$ENGINE" plan "$WORKSPACE"
"$ENGINE" run "$WORKSPACE"
"$ENGINE" package "$WORKSPACE"
"$ENGINE" verify --full-hash "$WORKSPACE"
```

`init` creates the workspace but does not choose reconstruction or model artifacts; with only the source photo path configured, the plan can stop at `BLOCKED_NO_CONFIGURED_ARTIFACT`. `run` follows the current plan and stops at configured authorization or review gates. For each fresh SfM or training stage, inspect `plan --full-hash`, authorize that exact plan with `authorize --stage … --plan-id … --reviewer … --rationale …`, then continue with `resume --authorization-id …`; SfM and training require separate approvals. Use `review "$WORKSPACE"` only when that project has the local review interface configured, and follow the specific review command reported for other gates. `resume` is conditional on a stage needing continuation. `package` is package-only; `verify --full-hash` checks the completed package and its referenced source/model content. A verified local package is not publication approval.
