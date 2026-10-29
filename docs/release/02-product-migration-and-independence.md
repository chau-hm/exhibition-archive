# 02 - Product migration and independence

## Migration result

The canonical product tree is `/Users/homan/repo/exhibition-archive`. Preflight found an initialized but empty repository: branch `main`, no commits or user files, configured remote unchanged, and no `origin/main`. No existing content was overwritten. The historical Engine had no product commit to migrate; source identity is recorded by SHA-256 in [01-source-inventory.json](01-source-inventory.json).

The migration includes the Python package and CLI, workflow and SQLite state, adapters, model checks, navigation generator and review service, publisher/release gate, dashboard/viewer templates, vendor viewer runtime, generic schema and example, test harnesses, documentation, and the canonical Skill. The historical README was replaced with generic product onboarding. Test roots and default evidence output paths were rebased for this repository. The package remains version `0.1.0`; the prior candidate was `0.1.0-rc.1`. No new release identifier was minted.

## Exclusions

The Chuen Lung manifest and private project directory, SQLite/runtime state, venv/cache files, historical reports, obsolete future-Skill draft, data-bearing Phase 2ER review page/server, photos, COLMAP outputs, PLYs, checkpoints, and generated venue website were not copied. They remain in the historical repository. The project example is generic and does not select Chuen Lung data. No LICENSE was present; none was invented.

## Independence audit

Runtime source, Skill source, docs, examples, tests, launcher, templates, and config were searched for Chuen Lung/research paths and phase/venue names. The remaining matches are intentional: a negative test preventing Chuen Lung leakage into generated public JavaScript, source-relative exclusion/provenance entries in the migration inventory, and links to historical validation evidence in release reports. No ordinary runtime code imports from the historical tree or requires a developer-specific absolute path.

The publisher retains legacy internal template markers and converts them to generic public names as it packages a site. This is a compatibility transform; generated files are verified without the internal marker. `review_hub.py` can load an optional review server only when a project manifest explicitly configures one; the generic example does not configure it.

Python imports resolve to this repository's `src/exhibition_archive`. An installed console command successfully ran against an external synthetic workspace from an unrelated current directory. Project inputs and durable SQLite state are external and configurable. Chuen Lung is used only as an explicit, read-only regression source.

## Git hygiene

The repository has no commits; all product files remain untracked and no files are staged. `.venv/` and `.local/` are ignored. No data assets, secrets, PLYs, checkpoints, or SQLite runtime files are in the product tree. No commit, push, branch rewrite, remote change, or tag was made. A license choice and the repository's initial commit remain owner decisions.
