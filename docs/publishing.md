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
