# Installation and CLI use

## Requirements

- uv for Python version and dependency management.
- Python 3.11 or newer; use Python 3.12 for development and validation.
- Pillow is installed from the project's declared runtime dependencies.
- pytest is included in the default development dependency group.
- Spirula is optional and only needed for fresh SfM or training. Existing validated COLMAP and PLY inputs can be adopted without starting those backends.

## uv-managed development environment

From a clean checkout:

~~~bash
uv python find 3.12
uv venv .venv --python 3.12
uv sync --locked --python .venv/bin/python
.venv/bin/exhibition-archive --help
.venv/bin/exhibition-archive --version
PYTHONPATH=src .venv/bin/python -m pytest tests -q
export EXHIBITION_ARCHIVE_ENGINE="$PWD/.venv/bin/exhibition-archive"
~~~

`uv.lock` pins the Engine and default development dependencies. Use the project-local `.venv` for CLI, test, and future workspace commands; do not use a Conda interpreter. If the lockfile is intentionally changed, regenerate it with `uv lock`, review the diff, and then use `uv sync --locked`.

The installed console script can be run from any working directory. The repository also includes bin/exhibition-archive, a source-checkout launcher that resolves its package relative to the script location; for operator work, prefer `.venv/bin/exhibition-archive` so the CLI uses the uv-managed environment.

## Initialize an external workspace

Copy examples/fresh-run.project.example.json to a project-specific location, then edit its values. Paths in project manifests are interpreted relative to the project workspace unless they are absolute. Keep original photos read-only and store workflow SQLite state, logs, intermediate outputs, and website output in an external workspace.

The example is intentionally not ready for fresh training. Before planning or authorizing training, set positive `minimum_free_disk_bytes_training` and `checkpoint_replacement_reserve_bytes` values. The replacement reserve is explicit operator-provisioned headroom or a measured value; a previous checkpoint measurement is not required. Record its source with `checkpoint_replacement_reserve_basis` (`OPERATOR_PROVISIONED` or `MEASURED`). At launch the Engine requires the runtime free-disk floor plus this reserve to allow the old and new full checkpoints to coexist temporarily. The reserve is a safety budget, not a checkpoint-size prediction.

~~~bash
"$EXHIBITION_ARCHIVE_ENGINE" init --config /data/gallery/project.json --workspace /data/gallery/workspace
"$EXHIBITION_ARCHIVE_ENGINE" plan /data/gallery/workspace --full-hash
"$EXHIBITION_ARCHIVE_ENGINE" status /data/gallery/workspace
~~~

Do not put venue datasets, runtime SQLite databases, or generated packages in the product checkout.

## Useful commands

~~~text
"$EXHIBITION_ARCHIVE_ENGINE" init --config CONFIG --workspace WORKSPACE
"$EXHIBITION_ARCHIVE_ENGINE" plan WORKSPACE [--full-hash]
"$EXHIBITION_ARCHIVE_ENGINE" status WORKSPACE
"$EXHIBITION_ARCHIVE_ENGINE" storage-report WORKSPACE
"$EXHIBITION_ARCHIVE_ENGINE" run WORKSPACE [--authorization-id ID]
"$EXHIBITION_ARCHIVE_ENGINE" authorize WORKSPACE --stage sfm|training --plan-id ID --reviewer NAME --rationale TEXT
"$EXHIBITION_ARCHIVE_ENGINE" resume WORKSPACE [--authorization-id ID]
"$EXHIBITION_ARCHIVE_ENGINE" review WORKSPACE
"$EXHIBITION_ARCHIVE_ENGINE" navigation assess|generate|validate|review WORKSPACE ...
"$EXHIBITION_ARCHIVE_ENGINE" package WORKSPACE
"$EXHIBITION_ARCHIVE_ENGINE" verify WORKSPACE [--full-hash] [--public-release]
"$EXHIBITION_ARCHIVE_ENGINE" logs WORKSPACE STAGE
"$EXHIBITION_ARCHIVE_ENGINE" cancel WORKSPACE STAGE
~~~

Run the command's --help for exact options. Only the Engine can determine current stage readiness and whether existing artifacts remain valid.
