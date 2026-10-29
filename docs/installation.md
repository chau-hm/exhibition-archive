# Installation and CLI use

## Requirements

- Python 3.11 or newer.
- No Python runtime dependencies are declared.
- Spirula is optional and only needed for fresh SfM or training. Existing validated COLMAP and PLY inputs can be adopted without starting those backends.

## Editable local install

From a clean checkout:

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/exhibition-archive --help
.venv/bin/exhibition-archive --version
~~~

The installed console script can be run from any working directory. The repository also includes bin/exhibition-archive, a source-checkout launcher that resolves its package relative to the script location.

## Initialize an external workspace

Copy examples/fresh-run.project.example.json to a project-specific location, then edit its values. Paths in project manifests are interpreted relative to the project workspace unless they are absolute. Keep original photos read-only and store workflow SQLite state, logs, intermediate outputs, and website output in an external workspace.

~~~bash
exhibition-archive init --config /data/gallery/project.json --workspace /data/gallery/workspace
exhibition-archive plan /data/gallery/workspace --full-hash
exhibition-archive status /data/gallery/workspace
~~~

Do not put venue datasets, runtime SQLite databases, or generated packages in the product checkout.

## Useful commands

~~~text
exhibition-archive init --config CONFIG --workspace WORKSPACE
exhibition-archive plan WORKSPACE [--full-hash]
exhibition-archive status WORKSPACE
exhibition-archive run WORKSPACE [--authorization-id ID]
exhibition-archive authorize WORKSPACE --stage sfm|training --plan-id ID --reviewer NAME --rationale TEXT
exhibition-archive resume WORKSPACE [--authorization-id ID]
exhibition-archive review WORKSPACE
exhibition-archive navigation assess|generate|validate|review WORKSPACE ...
exhibition-archive package WORKSPACE
exhibition-archive verify WORKSPACE [--full-hash] [--public-release]
exhibition-archive logs WORKSPACE STAGE
exhibition-archive cancel WORKSPACE STAGE
~~~

Run the command's --help for exact options. Only the Engine can determine current stage readiness and whether existing artifacts remain valid.
