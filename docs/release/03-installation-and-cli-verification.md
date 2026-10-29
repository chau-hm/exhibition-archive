# 03 - Installation and CLI verification

## Environment and install

- Python: 3.13.5 (the package requires Python 3.11 or newer).
- Product: `exhibition-archive 0.1.0`.
- Declared runtime dependencies: none.
- Environment: repository-local ignored `.venv`.
- Commands executed successfully:
  ```sh
  python3 -m venv .venv
  .venv/bin/python -m pip install -e .
  ```

The editable install built and installed from the new repository. The installed import path is `/Users/homan/repo/exhibition-archive/src/exhibition_archive/__init__.py`. No Python import used the historical source tree.

## CLI portability

From `/private/tmp`, the installed command's `--help` and `--version` succeeded; version output was `exhibition-archive 0.1.0`. The repository launcher and a unit smoke test also returned the version outside the repository. Using the installed CLI from `/private/tmp` against an independent synthetic project, `plan` and `status` exited 0; status read the external SQLite workflow state (`COMPLETED`, 10 stages, publication `NEEDS_REVIEW`).

The documented command surface is exposed by `exhibition-archive --help`; it includes init, plan, status, run/resume, navigation/review, packaging/verification, logs, cancellation, and execution authorization. No real reconstruction or training command was run.

## Verification results

- Unit suite: 28 passed.
- Synthetic Level B fake backend suite: PASS, covering fresh SfM/training adapters, model/PLY validation, authorization and public-selection gates, cache reuse, errors, timeout, cancellation, interrupted-process reconciliation, and finalization failures. No real SfM, GPU training, Chuen Lung data, or photo decoding was used. Evidence: `.local/validation/phase3c1/fresh-backend/run-20261029T145044Z/fresh-backend-test-results.json`.
- Independent fixture portability, recovery, cache-only package resume, and static package verification: PASS. Evidence: `.local/validation/phase3c1/portability/run-20261029T144952Z/portability-test-results.json`.
- Second portability invocation from an unrelated current directory: PASS at `/private/tmp/ea-phase3c1-validation/portability-external-cwd/run-20261029T145447Z/portability-test-results.json`.
- Copied Phase 3C RC regression workspace: migrated Engine completed with SfM and training adopted, not rerun. Full-hash verification passed for 292 source photos (zero photo integrity failures) and the accepted model identity. Package index remained `dafd501870e7b633050abc599c51f047cbe426ba778e1495851db73db577d83d`, with 24 published files. Rights remained `NEEDS_REVIEW`; no public approval was made.

Synthetic package evidence is not browser evidence. The browser rerun limitation is documented in report 06. The generated static package is a Publish Package; a self-contained Preservation Package is not implemented.
