# Exhibition Archive Engine

A local-first Python CLI and Codex Skill for preparing reviewable exhibition archives from photo collections and existing reconstruction/model artifacts. The Engine owns durable workflow state, provenance, validation, and publication gates; exhibition source data and project workspaces stay outside this repository.

## What it does

- Initializes an exhibition project and SQLite workflow state in a workspace you choose.
- Inventories source photographs and runs safe capture preflight.
- Adopts existing COLMAP, Gaussian-splat PLY, artwork, and navigation inputs, or plans fresh Spirula SfM/training work.
- Binds fresh expensive backend runs to a one-use approval for the exact current plan.
- Generates and validates navigation proposals and provides local review interfaces.
- Builds a static visitor package and checks its integrity and publication review state.

The Engine does not determine legal rights, approve curatorial decisions, certify inferred architecture as surveyed, or create a self-contained Preservation Package. A real Lumo pilot exposed a black-JPEG HEIC conversion defect; the reusable correction and its limits are documented below.

## Install

Requires uv and Python 3.11 or later; use Python 3.12 for development and validation. Pillow is installed from the project's declared runtime dependencies.

~~~bash
uv python find 3.12
uv venv .venv --python 3.12
uv sync --locked --python .venv/bin/python
.venv/bin/exhibition-archive --help
export EXHIBITION_ARCHIVE_ENGINE="$PWD/.venv/bin/exhibition-archive"
~~~

For a Codex session opened at this repository, repository-scoped Skill discovery uses the checked-in .agents/skills/exhibition-archive link. The one authoritative Skill source is skills/exhibition-archive. Type /skills to inspect available Skills or explicitly invoke $exhibition-archive.

## Spirula and HEIC datasets

Spirula Studio is an external, separately installed dependency for fresh reconstruction or Gaussian training. Its GUI application includes the CLI executable, so a separate CLI installer is normally unnecessary. On macOS, the executable can be at `/Applications/Spirula Studio.app/Contents/MacOS/spirula`; configure `integrations.sfm.executable` and `integrations.model.executable` to the installed executable or a compatible `spirula` on `PATH`.

The Engine can run safe source inventory and capture preflight without Spirula installed. When fresh Spirula SfM selects a source folder containing HEIC/HEIF, `run --full-hash` prepares a verified JPEG dataset and records source-to-output hashes in `derived/sfm-dataset-preparation.json`. Existing JPEGs are copied byte-for-byte. HEIC conversion uses the external `heif-convert` program from libheif at JPEG quality 95, normalizes orientation, carries and checks camera/focal/GPS metadata, and compares decoded JPEG pixels with a temporary libheif PNG decode of the original. Conversion fails if output is black or otherwise differs substantially from its source, or if essential camera make/model/focal data cannot be verified. The original photo folder remains read-only.

Install libheif on macOS with `brew install libheif`; confirm it with `heif-convert --version`. Installing the Python package also installs Pillow, which the Engine uses with LittleCMS to decode pixels and inspect or transform ICC profiles. By default, the Engine preserves the source ICC profile and does not relabel Display P3 as DCI-P3. When fresh SfM is configured with its supported `--image-gamut Rec.709` option (or `sRGB` where supported by that backend), HEIC preparation performs an ICC-managed transform to sRGB and writes a separate versioned dataset under `derived/spirula-jpeg-srgb-v1/<preparation-hash>/`. It verifies the actual output profile and transformed pixels before accepting the JPEG. A configured gamut is blocked when converted profiles are unknown or conflict with the requested setting. Review [the dataset preparation guide](docs/heic-dataset-preparation.md) for installation, policy, verification, cache, and Lumo handoff instructions.

Fresh reconstruction and training still require a configured compatible backend and separate explicit authorization for each costly stage's current full-hash plan. Safe conversion only prepares inputs; it does not start reconstruction or training.

## First project

Keep the source photographs read-only and create a separate project workspace. Copy the generic example and edit project ID, source path, output path, publication policy, and optional integrations for the exhibition:

~~~bash
mkdir -p /path/to/new-project
cp examples/fresh-run.project.example.json /path/to/new-project/project.json
~~~

Then initialize and inspect the durable plan and status:

~~~bash
"$EXHIBITION_ARCHIVE_ENGINE" init --config /path/to/new-project/project.json --workspace /path/to/new-project/workspace
"$EXHIBITION_ARCHIVE_ENGINE" plan /path/to/new-project/workspace --full-hash
"$EXHIBITION_ARCHIVE_ENGINE" status /path/to/new-project/workspace
~~~

The example config is a template; it may describe fresh backends, which remain gated. Do not authorize a backend until its exact plan and limits have been reviewed.

## Safe operation

- Use plan, status, verify, and logs for inspection.
- Use run for safe local intake and configured adopted artifacts. A new SfM or training attempt stops at its authorization gate.
- Authorize fresh SfM or training only for the current full-hash plan, after reviewing the named stage, input, command, limits, resources, and expected output.
- Use review and navigation review for human decisions. Review approvals bind to exact current candidates.
- Use package for package-only work based on current validated artifacts. It does not launch SfM or training.
- Use verify --full-hash for integrity. verify --public-release additionally requires current human publication and rights approval.
- Do not deploy automatically. A local package is not authorization to publish.

See docs/installation.md, docs/workflow.md, docs/heic-dataset-preparation.md, docs/gpu-authorization.md, docs/navigation-review.md, docs/publishing.md, and docs/pilot-onboarding.md.

## Development

~~~bash
PYTHONPATH=src .venv/bin/python -m pytest tests -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python tests/run_portability_test.py
PYTHONPATH=src .venv/bin/python tests/run_fresh_backend_tests.py
~~~

Generated test workspaces and evidence are written under the ignored .local/ directory. Browser acceptance uses a real browser harness and is recorded separately from geometry/unit and synthetic backend tests. See docs/release for migration and release evidence.

## Release and licensing

The current package version is 0.1.0. Its Phase 3C release-candidate provenance is 0.1.0-rc.1. No new release tag has been assigned. No license has been selected; this repository does not invent one.
