# Publish and preservation outputs

The Publish Package is a static visitor website built from approved public inputs. It contains the viewer, selected model, approved navigation, public metadata, runtime assets, and integrity manifest. It excludes source photographs and private review records.

The Preservation Package is a separate future deliverable. It is not implemented as a self-contained package. A source index or visitor website does not replace original image preservation, SfM reconstruction, review history, provenance, reproduction instructions, or source checksums.

Before publication:

1. Select and review the exact model candidate.
2. Review navigation for the exact proposal and viewer transform.
3. Review artwork identity and public catalogue fields.
4. Decide image/artwork reproduction rights and access restrictions.
5. Build the local static candidate with package.
6. Run verify --full-hash and browser acceptance.
7. Record publication-review for the exact package candidate.
8. Run verify --public-release --full-hash and inspect all public files.

The Engine records a local human decision; it cannot establish the reviewer's legal authority or independently determine rights. A local package is not permission to publish or deploy. Do not automatically deploy.

## Build a static website

From the canonical Engine checkout, use the package-only command against a project workspace whose accepted model and navigation artifacts are already validated:

```sh
./bin/exhibition-archive package /path/to/project-workspace
./bin/exhibition-archive verify --full-hash /path/to/project-workspace
```

`package` rebuilds the visitor website, archive metadata, and verification outputs from the selected inputs. It skips SfM and Gaussian training. The website is written to the project's configured output directory at `website/`.

Before deployment, confirm that `config/package-index.json` reports `exhibition-archive.website-index.v2` and that `verify` passes. The index records size and SHA-256 for every regular website payload file except the index itself. Verification rejects omitted, extra, duplicate, unsafe, or changed paths and resolves local HTML, CSS, JavaScript, and model references within the package.

## Static hosting

Upload the *contents* of `output/website/` to the static host's document root or to a nested prefix, preserving the directory layout:

```text
index.html
viewer/
api/
config/
assets/exhibition.ply
```

Serve the website over HTTP or HTTPS. Do not open the files with `file://`. The viewer and its bundled PlayCanvas runtime use relative paths, so the same package works at `/` and at paths such as `/exhibitions/chuen-lung/`. Configure the host to serve the PLY as a binary static asset and permit its full download; the file is approximately 248 MB for the Chuen Lung case. The package needs no Engine checkout, Python installation, backend process, API service, database, or runtime asset proxy after deployment.

`README.md` is included in each website with its selected model hash. Keep `config/package-index.json` alongside the files. The browser regression command used for this reference case is:

```sh
PYTHONPATH=src .venv/bin/python tests/run_browser_acceptance.py \
  --website /path/to/project-workspace/output/website \
  --output /path/to/project-workspace/evidence/browser
```

The acceptance harness serves the unchanged static package locally at both `/` and `/nested/`, then checks a deliberately missing model response. It is a development verification tool, not part of the visitor deployment.

## Scope and release gates

The static website contains the selected PLY, bundled viewer runtime, accepted navigation, guided route poses, and public metadata. It excludes the original source photographs and private review records. It is not a self-contained Preservation Package: source materials, full preservation metadata, review history, reproduction instructions, and source checksums are not bundled with it.

Technical package acceptance and public publication approval are separate. Passing `package`, `verify`, and browser checks records only local technical readiness. Rights, access restrictions, public-release approval, and deployment remain explicit human decisions; this workflow does not change those states or publish the site.
