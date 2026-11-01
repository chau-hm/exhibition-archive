# HEIC dataset preparation for Spirula

Fresh Spirula SfM uses a verified JPEG view of registered HEIC/HEIF originals. The Engine adds the safe `sfm_dataset_prep` workflow stage after source inventory and capture preflight. It does not invoke SfM or training during conversion.

## Run and inspect

HEIC conversion requires the external `heif-convert` command from [libheif](https://github.com/strukturag/libheif), plus Pillow for pixel, EXIF, and ICC validation. On macOS, install libheif with [Homebrew](https://formulae.brew.sh/formula/libheif), then install the Engine dependencies and check both versions:

~~~bash
brew install libheif
uv venv .venv --python 3.12
uv sync --locked --python .venv/bin/python
heif-convert --version
.venv/bin/python -c 'import PIL; from PIL import ImageCms; print("Pillow", PIL.__version__, "LittleCMS", ImageCms.core.littlecms_version)'
~~~

From the Engine checkout, first create `.venv` with `uv venv .venv --python 3.12` if needed, then run `uv sync --locked --python .venv/bin/python`. On Linux, install the distribution package that provides `heif-convert` (often `libheif-examples`). HEIC preparation fails visibly with `CONVERTER_UNAVAILABLE` when the command or Pillow is missing. The error includes the macOS installation command. Spirula is not needed for intake or conversion.

With a configured project whose read-only source directory is registered in `source.photo_directory`, run safe intake and then inspect the current backend plan:

~~~bash
export EXHIBITION_ARCHIVE_ENGINE="/absolute/path/to/exhibition-archive/.venv/bin/exhibition-archive"
"$EXHIBITION_ARCHIVE_ENGINE" run /path/to/workspace --full-hash
"$EXHIBITION_ARCHIVE_ENGINE" plan /path/to/workspace --full-hash
~~~

The `run` command inventories source files, performs capture preflight, and prepares the JPEG dataset when fresh Spirula SfM is selected and HEIC/HEIF files are present. It can then stop at the normal SfM authorization boundary. Review the full-hash plan before separately authorizing a fresh SfM attempt. Training has its own plan and authorization boundary.

### Lumo Display P3 to sRGB regeneration

Spirula Studio `2026.10.16` accepts `--image-gamut Rec.709` but does not expose Display P3 as a supported explicit gamut. Set the fresh SfM `integrations.sfm.additional_args` in the Lumo `project.json` to include:

~~~json
"additional_args": ["--image-gamut", "Rec.709"]
~~~

`Rec.709` selects ICC-managed sRGB output for HEIC/HEIF sources in Spirula Studio `2026.10.16`; an `sRGB` gamut selector requests the same output where another backend version supports that label. The Engine decodes the original through libheif to a temporary lossless PNG, transforms its embedded Display P3 profile to a generated sRGB profile with Pillow ImageCms/LittleCMS, then writes the final JPEG once at quality 95. It verifies the final JPEG contains the exact sRGB ICC bytes and compares decoded JPEG pixels to the transformed PNG. Unknown or absent source ICC profiles fail conversion. Display P3 is never relabeled as sRGB or DCI-P3.

Before running, confirm `integrations.sfm.image_directory` points to the registered original HEIC folder (or a registered HEIC-containing subfolder), not the previous 332-JPEG derivative set. `integrations.model.image_directory` must resolve to that same registered source folder so training later uses the exact same prepared images as SfM.

The transformed dataset is versioned separately at `derived/spirula-jpeg-srgb-v1/<preparation-fingerprint>/`; the existing `derived/spirula-jpeg/<preparation-fingerprint>/` Display P3 dataset is left untouched. The output retains image dimensions, camera/focal/GPS metadata, and normalized orientation 1. Source photos, temporary decoded PNGs, and EXIF sidecars remain outside the prepared image directory. SfM and training continue to use the same Engine-managed dataset and their existing independent authorization gates.

Install the updated Engine in the environment used by the exhibition session, confirm libheif and Pillow/LittleCMS versions, and regenerate safely:

~~~bash
cd /Users/homan/repo/exhibition-archive
uv sync --locked --python .venv/bin/python
heif-convert --version
.venv/bin/python -c 'import PIL; from PIL import ImageCms; print("Pillow", PIL.__version__, "LittleCMS", ImageCms.core.littlecms_version)'
export EXHIBITION_ARCHIVE_ENGINE="$PWD/.venv/bin/exhibition-archive"
"$EXHIBITION_ARCHIVE_ENGINE" run /path/to/Lumo/workspace --full-hash
"$EXHIBITION_ARCHIVE_ENGINE" plan /path/to/Lumo/workspace --full-hash
~~~

The `run` step is safe local preparation and stops before any fresh SfM or training execution. Confirm that the manifest reports schema v3, the `spirula-jpeg-srgb-v1` effective image directory, the selected `--image-gamut Rec.709`, and one mapping per registered original. For every converted HEIC, check `pixel_content_validation.status` and `color_conversion_validation.status` are `PASS`, `source_color_profile` is Display P3, `output_color_profile` is sRGB, `icc_profile_validation_status` is `TARGET_SRGB_ICC_PROFILE_BYTES_VERIFIED`, `output_orientation` is 1, and camera/focal metadata is retained. Confirm the plan uses this same effective image directory and its normalized command contains `--image-gamut Rec.709`. Stop if any image is missing, black, lacks metadata, or has a profile mismatch. The previous Lumo JPEG dataset and SfM plan are invalid; do not use or authorize them. Review the new full-hash SfM plan separately before any fresh SfM approval. Training still requires its own later plan and approval.

The default source-profile-preserving output is `derived/spirula-jpeg/<preparation-fingerprint>/`. For `--image-gamut Rec.709` or `sRGB`, the separate sRGB output is `derived/spirula-jpeg-srgb-v1/<preparation-fingerprint>/`. The top-level `derived/sfm-dataset-preparation.json` records the effective image directory, `heif-convert`/libheif, Pillow/LittleCMS and target-profile identities, settings, image mappings, source/output hashes, metadata checks, actual JPEG ICC profile evidence, decoded-pixel comparison, color-transform details, and disk-space estimate. A dataset-local `.exhibition-archive-dataset.json` records the same provenance. When an archive package is built, it includes this small preparation manifest, not the generated image set. Compatible JPEG inputs are copied byte-for-byte; HEIC/HEIF inputs become JPEG at quality 95. Names retain source-relative paths where possible. Case-insensitive basename collisions receive deterministic source-path hash suffixes.

Preparation is repeatable. The Engine reuses a dataset only when its source inventory, converter and Pillow/LittleCMS identities, settings, target sRGB profile hash, mapping, output membership, pixel/EXIF/ICC validation evidence, and file hashes still validate. The separate sRGB dataset uses schema v3 and a new color-validation policy, so it cannot reuse Display P3 cache evidence; existing valid v2 profile-preserving datasets remain unchanged and reusable. The earlier ImageIO schema remains invalid. Source, gamut, converter, or settings changes produce a different preparation fingerprint and invalidate fresh SfM plans. PNG references and EXIF sidecars stay under the operating system temporary directory; only final images and the small dataset manifest enter the SfM image directory. Failed conversion outputs are discarded and never recorded as a completed preparation. The disk guard checks estimated output size plus the configured reserve before conversion.

## Image and metadata handling

In the default profile-preserving path, `heif-convert --with-exif -q 95` writes the JPEG and extracts an EXIF sidecar. In the sRGB path, `heif-convert --with-exif` decodes only to a temporary lossless PNG and sidecar; Pillow/LittleCMS applies the ICC transform and performs the single final JPEG encode at quality 95. Both paths confirm the JPEG has the same normalized dimensions and orientation 1. Pixel validation compares a downsampled 512-pixel-maximum-side decode of the JPEG with its source reference (the ICC-transformed PNG for the sRGB path). This catches black or substantially corrupted output while accepting a genuinely dark photograph when its pixels remain consistent. There is no arbitrary resize, crop, or lens correction. Source-present camera, lens, focal-length, exposure, ISO, focal-plane, date, and GPS EXIF fields are compared with the written JPEG. Camera make, camera model, and focal length (or 35 mm equivalent) must be present. Missing or changed essential data fails preparation.

The Engine inspects the actual output JPEG ICC bytes and profile description. In the profile-preserving path, when the decoded source has an ICC profile, the JPEG profile bytes must match exactly. In the sRGB path, the decoded source must have a recognized Display P3 or sRGB ICC profile; the target profile is generated by LittleCMS, has a stable normalized creation timestamp, and is embedded byte-for-byte in the transformed PNG and final JPEG. Provenance records the source/target profile hashes, LittleCMS version, rendering intent, and ICC-managed transform method. Profiles without recognized names fail sRGB conversion. Compatible JPEG inputs remain byte-for-byte copies and are not profile-inspected by this stage, so a mixed dataset containing copied JPEGs blocks an explicit gamut unless its gamut can be verified. The Engine does not infer DCI-P3 from Display P3. `Rec.709` is accepted by Spirula only alongside the verified sRGB output requested for that dataset.

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

The reusable interface is part of the Engine CLI and does not require changes to a specific exhibition's originals. A separate exhibition session can install this repository version in its Engine environment, then run safe intake against its own registered workspace. It should use `run WORKSPACE --full-hash`, inspect `plan WORKSPACE --full-hash`, and confirm the plan's `input_dataset` and `input_dataset_provenance` before any separately authorized backend work. Depending on the explicit gamut setting, the automatic dataset is created inside that workspace under `derived/spirula-jpeg/` or `derived/spirula-jpeg-srgb-v1/`; no derivative should be copied into the product repository.

If that workspace already contains independently generated JPEGs, verify each original-to-JPEG SHA mapping, EXIF orientation and camera fields, dimensions, and color-profile handling before using the external manifest path above. Do not treat same-named JPEGs as equivalent to Engine-generated derivatives. Changing the effective dataset or its configuration requires regenerating the full-hash SfM plan and obtaining a new approval for that exact plan.
