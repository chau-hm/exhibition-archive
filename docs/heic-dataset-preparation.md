# HEIC dataset preparation for Spirula

Fresh Spirula SfM uses a verified JPEG view of registered HEIC/HEIF originals. The Engine adds the safe `sfm_dataset_prep` workflow stage after source inventory and capture preflight. It does not invoke SfM or training during conversion.

## Run and inspect

On macOS, Swift and Apple ImageIO are used through the packaged `imageio_heic.swift` helper. Xcode Command Line Tools provide Swift on a typical developer/operator Mac. Spirula itself is not needed for intake or conversion. On hosts without macOS ImageIO and Swift, HEIC preparation fails visibly; the Engine does not fall back to a metadata-stripping FFmpeg conversion.

With a configured project whose read-only source directory is registered in `source.photo_directory`, run safe intake and then inspect the current backend plan:

~~~bash
exhibition-archive run /path/to/workspace --full-hash
exhibition-archive plan /path/to/workspace --full-hash
~~~

The `run` command inventories source files, performs capture preflight, and prepares the JPEG dataset when fresh Spirula SfM is selected and HEIC/HEIF files are present. It can then stop at the normal SfM authorization boundary. Review the full-hash plan before separately authorizing a fresh SfM attempt. Training has its own plan and authorization boundary.

The managed output is `derived/spirula-jpeg/<preparation-fingerprint>/`. The top-level `derived/sfm-dataset-preparation.json` records the effective image directory, converter identity, settings, image mappings, hashes, metadata checks, color-profile evidence, and disk-space estimate. A dataset-local `.exhibition-archive-dataset.json` records the same provenance. When an archive package is built, it includes this small preparation manifest, not the generated image set. Compatible JPEG inputs are copied byte-for-byte; HEIC/HEIF inputs become JPEG at quality 95. Names retain source-relative paths where possible. Case-insensitive basename collisions receive deterministic source-path hash suffixes.

Preparation is repeatable. The Engine reuses a dataset only when its source inventory, converter identity, settings, mapping, output membership, JPEG readability, and file hashes still validate. Source or setting changes produce a different preparation fingerprint and invalidate fresh SfM plans. Failed conversion outputs remain in a temporary staging directory and are discarded; they are never recorded as a completed preparation. The disk guard checks estimated output size plus the configured reserve before conversion.

## Image and metadata handling

ImageIO decodes the HEIF source, applies EXIF orientation to the pixels, writes EXIF orientation 1, and verifies normalized dimensions. There is no arbitrary resize, crop, or lens correction. Source-present camera, lens, focal-length, exposure, ISO, focal-plane, date, and GPS EXIF fields are compared with the written JPEG. Camera make, camera model, and focal length (or 35 mm equivalent) must be present. Missing or changed essential data fails preparation.

ImageIO copies source metadata and compares source/output ICC profile bytes for converted HEIC when they are present; otherwise it compares decoded color-profile names. Compatible JPEGs remain byte-for-byte copies and are not profile-inspected by this stage. The Engine does not apply a separate gamut conversion or infer DCI-P3 from Display P3. Recognized derivative profile names are checked against an explicit `--image-gamut` argument. An explicit gamut argument is blocked if a converted HEIC profile is unknown or a byte-copied JPEG in the mixed dataset has no recorded profile evidence. Byte or profile-name agreement is not a measurement of colorimetric fidelity.

If fresh training is configured after fresh SfM preparation, its `model.image_directory` must resolve to the same source or prepared dataset. The Engine supplies the prepared dataset to the training dataset builder, retaining the exact filenames and orientation used for SfM. It does not create a second training-only conversion.

## Existing external JPEG derivatives

The existing `integrations.sfm.derived_image_manifest` contract can validate project-local external image datasets. To use one, register the derivative directory in `source.derived_image_directories`, point both fresh SfM and training `image_directory` settings at that exact directory, and set `integrations.sfm.derived_image_manifest` to a project-local JSON file containing `image_mappings`. Each mapping must provide:

~~~json
{
  "source_relative_path": "IMG_0001.HEIC",
  "source_sha256": "<full SHA-256 of the registered original>",
  "derived_relative_path": "IMG_0001.jpg",
  "derived_sha256": "<full SHA-256 of the JPEG>"
}
~~~

The Engine checks source and output paths, exact image-set membership, and current SHA-256 values. This existing external-manifest validator does not independently decode the JPEG or verify its EXIF orientation, camera metadata, or ICC profile. Before adopting external derivatives, verify those properties with a trusted image tool and retain that evidence alongside the manifest. Matching filenames alone is not sufficient. If any property cannot be verified, use the Engine-managed preparation from the registered originals instead.

## Parallel exhibition workspaces

The reusable interface is part of the Engine CLI and does not require changes to a specific exhibition's originals. A separate exhibition session can install this repository version in its Engine environment, then run safe intake against its own registered workspace. It should use `run WORKSPACE --full-hash`, inspect `plan WORKSPACE --full-hash`, and confirm the plan's `input_dataset` and `input_dataset_provenance` before any separately authorized backend work. The automatic dataset is created inside that workspace under `derived/spirula-jpeg/`; no derivative should be copied into the product repository.

If that workspace already contains independently generated JPEGs, verify each original-to-JPEG SHA mapping, EXIF orientation and camera fields, dimensions, and color-profile handling before using the external manifest path above. Do not treat same-named JPEGs as equivalent to Engine-generated derivatives. Changing the effective dataset or its configuration requires regenerating the full-hash SfM plan and obtaining a new approval for that exact plan.
