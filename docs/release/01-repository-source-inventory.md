# 01 — Repository source inventory

The destination was inspected before copying. It contained an initialized Git repository on main, no commits, and no product or user files; the configured origin remains unchanged, and origin/main is absent. The historical product was untracked in the old research repository, so the migrated baseline is anchored by the source-relative SHA-256 entries in 01-source-inventory.json rather than by an old product commit.

The inventory records exact source hashes for reusable files, the old RC/version identity, previous validation locations, and every excluded category. Runtime Python source, launcher, templates, dashboard, tests, generic manifest/schema, Skill, and lock/package metadata were selected. The historical README was hashed then replaced with generic onboarding because it contains Chuen Lung project instructions. Test harness path roots were rebased and their default outputs moved to ignored .local storage.

Excluded items include the private Chuen Lung manifest and project workspace, workflow SQLite/log/lock data, virtual environment, bytecode, obsolete future-Skill draft, large venue assets, and historical analysis outputs. Those assets and evidence remain in the old repository. No LICENSE was present or invented.
