# HEIC dataset preparation for Spirula

Fresh Spirula SfM uses a verified JPEG view of registered HEIC/HEIF originals. The Engine adds the safe `sfm_dataset_prep` workflow stage after source inventory and capture preflight. It does not invoke SfM or training during conversion.

## Run and inspect

HEIC conversion requires the external `heif-convert` command from [libheif](https://github.com/strukturag/libheif), plus Pillow for pixel, EXIF, and ICC validation. On macOS, install libheif with [Homebrew](https://formulae.brew.sh/formula/libheif), then install the Engine dependencies and check both versions:

~~~bash
brew install libheif
python3 -m pip install -e .
heif-convert --version
python3 -c 'import PIL; print("Pillow", PIL.__version__)'
~~~

On Linux, install the distribution package that provides `heif-convert` (often `libheif-examples`) and install the Engine with `python3 -m pip install -e .`. HEIC preparation fails visibly with `CONVERTER_UNAVAILABLE` when the command or Pillow is missing. The error includes the macOS installation command. Spirula is not needed for intake or conversion.

With a configured project whose read-only source directory is registered in `source.photo_directory`, run safe intake and then inspect the current backend plan:

~~~bash
exhibition-archive run /path/to/workspace --full-hash
exhibition-archive plan /path/to/workspace --full-hash
~~~

The `run` command inventories source files, performs capture preflight, and prepares the JPEG dataset when fresh Spirula SfM is selected and HEIC/HEIF files are present. It can then stop at the normal SfM authorization boundary. Review the full-hash plan before separately authorizing a fresh SfM attempt. Training has its own plan and authorization boundary.

When replacing a dataset prepared by the retired ImageIO converter, install the updated Engine in the environment used by the exhibition session, confirm `heif-convert --version`, then regenerate and inspect the dataset:

~~~bash
python3 -m pip install -e /Users/homan/repo/exhibition-archive
heif-convert --version
exhibition-archive run /path/to/Lumo/workspace --full-hash
exhibition-archive plan /path/to/Lumo/workspace --full-hash
~~~

The `run` step is safe local preparation and stops before any fresh SfM or training execution. Confirm that the manifest reports the v2 schema, the new effective image directory, a `libheif heif-convert` identity, `pixel_content_validation.status` of `PASS` for every converted image, and the actual output ICC profile. Confirm the plan uses that same effective image directory. If an explicit `--image-gamut` conflicts with the recorded output profiles, update the project configuration to match the verified profile, then rerun `run` and `plan`. The old Lumo JPEG set and SfM plan must not be used or authorized.

The managed output is `derived/spirula-jpeg/<preparation-fingerprint>/`. The top-level `derived/sfm-dataset-preparation.json` records the effective image directory, `heif-convert` and libheif identity, settings, image mappings, hashes, metadata checks, actual JPEG ICC profile evidence, decoded-pixel comparison, and disk-space estimate. A dataset-local `.exhibition-archive-dataset.json` records the same provenance. When an archive package is built, it includes this small preparation manifest, not the generated image set. Compatible JPEG inputs are copied byte-for-byte; HEIC/HEIF inputs become JPEG at quality 95. Names retain source-relative paths where possible. Case-insensitive basename collisions receive deterministic source-path hash suffixes.

Preparation is repeatable. The Engine reuses a dataset only when its source inventory, converter and Pillow identities, settings, mapping, output membership, pixel/EXIF/ICC validation evidence, and file hashes still validate. The v2 preparation schema and validation-policy fingerprint invalidate ImageIO-era cache evidence, including black JPEGs whose old hashes still match their old manifest. Source or setting changes produce a different preparation fingerprint and invalidate fresh SfM plans. The JPEG, decoded PNG reference, and `heif-convert` EXIF sidecar are held under the operating system temporary directory; only images and the small dataset manifest enter the final SfM image directory. Failed conversion outputs are discarded and never recorded as a completed preparation. The disk guard checks estimated output size plus the configured reserve before conversion.

## Image and metadata handling

`heif-convert --with-exif -q 95` writes the JPEG and extracts an EXIF sidecar. The Engine separately decodes the HEIF source through libheif to a temporary PNG, then confirms the JPEG contains the same normalized dimensions and orientation 1. It compares a downsampled 512-pixel-maximum-side decode of the JPEG against the original HEIF decode. This catches black or substantially corrupted output by comparing it to the source, so a genuinely dark photograph is accepted when its pixels remain consistent. There is no arbitrary resize, crop, or lens correction. Source-present camera, lens, focal-length, exposure, ISO, focal-plane, date, and GPS EXIF fields are compared with the written JPEG. Camera make, camera model, and focal length (or 35 mm equivalent) must be present. Missing or changed essential data fails preparation.

The Engine inspects the actual output JPEG ICC bytes and profile description. When libheif's decoded source PNG has an ICC profile, the JPEG profile bytes must match exactly. Profiles without a recognized name remain recorded as unknown. Compatible JPEGs remain byte-for-byte copies and are not profile-inspected by this stage. The Engine does not infer DCI-P3 from Display P3. A recognized profile must match an explicit `--image-gamut` argument; unknown converted profiles and uninspected byte-copied JPEGs in a mixed dataset block an explicit gamut argument. ICC byte agreement verifies profile retention, not measured colorimetric fidelity.

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
