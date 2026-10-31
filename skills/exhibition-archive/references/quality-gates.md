# Quality gates and package meaning

## Technical evidence

- Source inventory records supported files, path membership, size and SHA-256. Original photographs are registered read-only.
- Fresh SfM must pass the Engine's structural COLMAP validation and configured quality thresholds. Warning/review states block dependent work unless an explicitly configured policy permits them.
- A human may use `sfm-review` to accept one structurally valid component for experimental fresh 3DGS training while preserving `SFM_NEEDS_REVIEW`, the original warnings and thresholds. This is a scoped review-gate decision, not a final SfM/archive acceptance or a global warning-policy change. It is invalidated by changed stage artifacts, any component artifact tree or membership, source inventory, or relevant configuration.
- Training from a scoped SfM acceptance receives only the selected component's registered source views through Engine-verified symlinks with COLMAP filenames preserved. A different component or any extra source/unregistered views is excluded unless separately selected and accepted.
- Fresh Spirula training is a `candidate` operation bounded by explicit iterations and resource policy. `SMOKE_TEST_ONLY` projects cannot enter the production workflow, and smoke outputs cannot become selected public models.
- Existing adopted model/SfM inputs must match configured identities and pass current validators.
- Generated navigation remains a proposal until geometry, transform, floor/ceiling and any provisional camera decisions are reviewed and approved.
- The public viewer excludes source photographs, private notes, absolute local paths, internal image IDs and unrestricted Debug Fly. Artwork identities are exported only from the confirmed catalogue.

## Package classes

- Local technical preview: assembled for local review; no public rights decision implied.
- Public Publish Package: static viewer, selected PLY, navigation, confirmed public metadata and runtime files. Technical package verification is required.
- Public release readiness: requires a current human decision for selected model, navigation, artwork metadata, image and artwork publication rights, access policy and public/private data policy. `publication-review` records that decision against the exact package hash. It does not deploy.
- Preservation Package: a separate future bundle with original photographs, SfM, selected model, artwork evidence, review decisions, provenance, known limitations, integrity manifests and viewer/source version. Current product status is `METADATA_PLUS_DEPLOYABLE_VIEWER`, not self-contained preservation.

An empty confirmed-artwork catalogue is allowed only when the reviewer explicitly accepts an empty public catalogue. Provisional identities remain private and must not be promoted automatically.
