# 07 - Release candidate readiness (Phase 3C.1 historical)

> This is the Phase 3C.1 snapshot. Its browser and Skill-runtime blockers were closed in Phase 3C.2; use the current [Phase 3C.2 release acceptance and pilot readiness report](phase3c2/07-release-acceptance-and-pilot-readiness.md) for the present decision.

## Decision

**`REPOSITORY_MIGRATED_SKILL_RUNTIME_BLOCKED`**

The standalone source migration, editable install, CLI portability, unit and fake-backend tests, synthetic package verification, RC package regression, and historical asset SHA checks passed. The Skill is structurally valid and packaged, but actual Codex discovery, activation, and CLI delegation were not observed because the supported model session failed before inference at app-server initialization. Post-migration browser rerun is also blocked by missing runtime tooling and sandbox networking. The prior Phase 3C viewer browser run passed 11/11 and remains explicitly historical evidence.

The new repository is the canonical product tree for continued development. It remains uncommitted on `main`; there is no new RC tag or public release. Version is still `0.1.0`, with `0.1.0-rc.1` retained as prior provenance. A license is not selected. The next product milestone remains Phase 3D, an independent real-exhibition pilot; no pilot has started. A supervised direct-CLI pilot can begin, while Skill-led normal use remains unverified.

## Handoff answers

1. **`MIGRATED_AND_VALIDATED`** - The new repository was initialized on `main` with no commits or product/user files. Its remote configuration was left unchanged; `origin/main` was absent.
2. **`MIGRATED_AND_VALIDATED`** - Migrated runtime package, CLI, adapters, workflow/state, navigation/review, publisher, dashboard and viewer assets, schema/example, lock/package metadata, test harnesses, Skill, and generic product docs.
3. **`MIGRATED_AND_VALIDATED`** - Excluded venue manifest/workspace, databases/logs/locks, caches, obsolete draft, legacy evidence dashboard/server, historical reports, photos, COLMAP, PLY/checkpoints, and generated exhibition packages.
4. **`MIGRATED_AND_VALIDATED`** - No existing destination user files existed. Historical README was hashed and intentionally replaced with generic onboarding. Old working-tree files remain in place.
5. **`MIGRATED_AND_VALIDATED`** - Yes for the previously implemented reusable Engine source and its tests/docs; the separate self-contained Preservation Package is not part of the Engine.
6. **`VERIFIED_BY_EXECUTION`** - Import path resolves to the new repository package.
7. **`VERIFIED_BY_EXECUTION`** - Python 3.13.5 virtual environment and `pip install -e .` succeeded.
8. **`VERIFIED_BY_EXECUTION`** - Help/version, plan/status, launcher, imports, and portability test worked outside the source cwd.
9. **`VERIFIED_BY_EXECUTION`** - External project workspace and project manifest paths were used in fixture and RC regression.
10. **`VERIFIED_BY_EXECUTION`** - Chuen Lung manifests/assets are excluded and used only for explicit read-only regression. No ordinary runtime dependency on the old source path was found.
11. **`MIGRATED_AND_VALIDATED`** - `/Users/homan/repo/exhibition-archive/skills/exhibition-archive/SKILL.md`.
12. **`MIGRATED_AND_VALIDATED`** - Repository `.agents/skills/exhibition-archive` points to the canonical Skill. This follows the documented repository `.agents/skills` scope and supported symlink approach.
13. **`BLOCKED`** - Runtime metadata discovery was not observed in a fresh session.
14. **`BLOCKED`** - Skill body load was not observed.
15. **`BLOCKED`** - Explicit invocation did not reach a model turn.
16. **`BLOCKED`** - Indirect activation did not reach a model turn.
17. **`BLOCKED`** - Negative-trigger behavior did not reach a model turn.
18. **`BLOCKED`** - No Skill-issued Engine CLI call was observed. Direct Engine CLI tests are separately `VERIFIED_BY_EXECUTION`.
19. **`BLOCKED`** - Skill interpretation of SQLite state was not observed; direct CLI status did read durable SQLite state.
20. **`IMPLEMENTED_AND_TESTED`** - Engine plan-bound authorization, changed-plan invalidation, and expensive-operation gating passed unit/fake-backend coverage. Skill policy behavior is unverified.
21. **`IMPLEMENTED_AND_TESTED`** - Engine fake integration used no real SfM or GPU training; no Skill runtime was available to test the Skill's behavior.
22. **`BLOCKED`** - Installed CLI 0.137.0 rejected global reasoning `max`; per-command override then hit `Operation not permitted`. Fresh GPT-5.5 exec stopped before inference: `failed to initialize in-process app-server client: Operation not permitted`. Separate previous GPT-6.1-sol attempt returned HTTP 403 unsupported account/model.
23. **`MIGRATED_AND_VALIDATED`** - Yes. The reasoning config, app-server sandbox denial, prior model-service 403, Skill discovery, and package validation are tracked as separate signals.
24. **`VERIFIED_BY_EXECUTION`** - 28 unit tests, fake-backend integration, independent portability, and RC package/full-hash verification passed.
25. **`BLOCKED`** - Post-migration browser rerun unavailable; earlier Phase 3C browser evidence passed 11/11.
26. **`VERIFIED_BY_EXECUTION`** - All six historical protection groups currently match the saved SHA-256 baseline, including PLY, photos, COLMAP, navigation, site, and artwork evidence.
27. **`IMPLEMENTED_AND_TESTED`** - Navigation and rights gates remained enforced. RC package verification passed; publication rights remained `NEEDS_REVIEW`.
28. **`MIGRATED_AND_VALIDATED`** - Yes as the canonical product working tree. Initial commit, license selection, and Skill runtime verification remain pending.
29. **`BLOCKED`** - Not ready to claim normal Skill use until a fresh session shows discovery, body load, CLI delegation, and correct interpretation.
30. **`NEEDS_REVIEW`** - Ready to begin a supervised direct-CLI pilot; not ready to claim a Skill-led pilot experience. The independent dataset remains `NOT_STARTED`.
31. **`BLOCKED`** - Actual Skill invocation/body/delegation, fresh browser acceptance, and optional YAML-backed format check. Manual Skill procedure is in report 04.
32. **`NOT_IMPLEMENTED`** - Deferred: self-contained Preservation Package, architectural plane estimation where unsupported, richer navigation features such as spline touring, and future pilot-specific capture/model limits. These do not change the tested CLI Engine baseline.

## Publish and preservation boundary

A Publish Package is a static candidate with verified content; publication still requires navigation, artwork metadata, rights, and public/private policy decisions. A Preservation Package is a distinct, future output and is not implemented. No package was deployed or publicly released.

## Readiness conditions for later release

1. Complete the fresh-session Skill procedure in report 04 and capture actual selection, loaded body, CLI call, and SQLite interpretation.
2. Re-run browser acceptance in a supported local browser harness.
3. Choose and document repository licensing.
4. Review and commit the migration through the owner's Git workflow; do not push/tag until their release criteria are satisfied.
5. Begin Phase 3D with a real independent exhibition folder, explicit rights/access policy, and human review of model, artwork, navigation, and public output.
