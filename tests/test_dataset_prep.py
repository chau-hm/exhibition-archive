from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exhibition_archive import dataset_prep
from exhibition_archive.adapters import sha256_file, write_json
from exhibition_archive.backends import BackendError
from exhibition_archive.state import ProjectState
from exhibition_archive.workflow import (_validate_read_only_image_scope,
                                         expensive_execution_plan, run_workflow)


BACKEND = {
    "tool": {"executable": "/opt/test/spirula", "version": "test-1.2"},
    "sfm": {"auto_arguments": ["--max-image-size", "--image-gamut"]},
    "training": {"presets": ["3dgs"], "arguments": ["--train-resolution-divisor", "--device"]},
}

FAKE_CONVERTER = {"name": "libheif heif-convert test double", "version": "libheif test-1",
                  "pixel_validator": {"name": "Pillow", "version": "test"}}


def jpeg_header(width: int = 24, height: int = 32) -> bytes:
    """Minimal SOF-bearing JPEG header for deterministic orchestration tests."""
    return (b"\xff\xd8\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") +
            width.to_bytes(2, "big") + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00")


def config_for(root: Path) -> dict:
    photos = root / "input" / "originals"
    return {
        "schema_version": "exhibition-archive.project.v1",
        "project_id": "heic-prep-fixture",
        "source": {"photo_directory": str(photos), "read_only": True},
        "output_directory": "output",
        "integrations": {
            "sfm": {"mode": "spirula_fresh", "executable": "/opt/test/spirula",
                    "image_directory": str(photos), "quality": "low", "data_type": "individual",
                    "additional_args": ["--image-gamut", "sRGB"]},
            "model": {"mode": "spirula_fresh", "executable": "/opt/test/spirula",
                      "image_directory": str(photos), "run_type": "candidate", "preset": "3dgs",
                      "iterations": 50, "resolution_divisor": 2},
        },
        "resource_policy": {"minimum_free_disk_bytes_dataset_prep": 0, "minimum_free_disk_bytes_sfm": 0,
                            "max_training_iterations": 100, "sfm_timeout_seconds": 120,
                            "training_timeout_seconds": 120},
    }


def inventory_for(photos: Path) -> dict:
    return {"images": [{"relative_path": path.relative_to(photos).as_posix(),
                        "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                       for path in sorted(photos.rglob("*"))
                       if path.is_file() and path.suffix.casefold() in dataset_prep.IMAGE_SUFFIXES]}


def write_inventory(root: Path, photos: Path) -> dict:
    inventory = inventory_for(photos)
    target = root / "output" / "work" / "source-index.json"
    write_json(target, {"schema_version": "exhibition-archive.source-index.v1",
                        "project_id": "heic-prep-fixture", "images": inventory["images"]})
    return inventory


def fake_batch(requests: list[dict[str, str]], _converter: dict, _timeout: int) -> dict:
    result = {}
    for request in requests:
        Path(request["output"]).write_bytes(jpeg_header())
        result[request["source"]] = {
            "width": 24, "height": 32, "source_width": 32, "source_height": 24,
            "source_orientation": 6, "output_orientation": 1, "output_tiff_orientation": 1,
            "jpeg_quality": 95,
            "metadata_validation_status": "PRESERVED_SOURCE_FIELDS",
            "source_metadata_fields": ["camera_make", "camera_model", "focal_length_35mm"],
            "retained_metadata_fields": ["camera_make", "camera_model", "focal_length_35mm"],
            "missing_source_essential_fields": [],
            "validation_policy": dataset_prep.CONVERSION_VALIDATION_POLICY,
            "pixel_content_validation": {"status": "PASS",
                                         "method": "libheif PNG decode compared with decoded JPEG pixels",
                                         "sample_max_side": 512, "mean_absolute_channel_error": 0.1,
                                         "source_luminance_p99": 220, "output_luminance_p99": 219},
            "source_color_profile": "sRGB", "output_color_profile": "sRGB",
            "source_icc_profile_byte_count": 0, "output_icc_profile_byte_count": 0,
            "source_icc_profile_sha256": None, "output_icc_profile_sha256": None,
            "icc_profile_validation_status": "SOURCE_ICC_PROFILE_ABSENT",
            "color_profile_validation_status": "OUTPUT_ICC_PROFILE_INSPECTED",
            "color_handling": "test double",
        }
    return result


class DatasetPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ea-heic-prep-")
        self.root = Path(self.tmp.name) / "project"
        self.photos = self.root / "input" / "originals"
        self.photos.mkdir(parents=True)
        self.config = config_for(self.root)
        (self.root / "project.json").write_text(json.dumps(self.config), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def prepare_with_test_converter(self, inventory: dict) -> dict:
        with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
             patch.object(dataset_prep, "_run_heif_convert_batch", side_effect=fake_batch), \
             patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
            return dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory)

    def test_heif_detection_and_jpeg_only_no_op(self):
        jpeg = self.photos / "room.JPG"
        jpeg.write_bytes(b"existing-jpeg-bytes")
        self.assertFalse(dataset_prep.contains_heif(self.photos))
        no_op = dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory_for(self.photos))
        self.assertEqual(no_op["status"], "NO_CONVERSION_REQUIRED")
        self.assertEqual(jpeg.read_bytes(), b"existing-jpeg-bytes")

        (self.photos / "nested").mkdir()
        heif = self.photos / "nested" / "new.HEIF"
        heif.write_bytes(b"registered-heif")
        self.assertTrue(dataset_prep.contains_heif(self.photos))

    def test_missing_libheif_converter_fails_clearly(self):
        with patch.object(dataset_prep.shutil, "which", return_value=None):
            with self.assertRaisesRegex(BackendError, "heif-convert.*brew install libheif"):
                dataset_prep.heif_converter_identity()

    def test_pixel_validation_rejects_black_output_but_accepts_a_dark_source(self):
        from PIL import Image, ImageDraw

        reference = self.root / "dark-source.png"
        matching_jpeg = self.root / "dark-match.jpg"
        black_jpeg = self.root / "black.jpg"
        dark = Image.new("RGB", (192, 128), (4, 5, 7))
        ImageDraw.Draw(dark).rectangle((80, 40, 120, 90), fill=(22, 19, 15))
        dark.save(reference, "PNG")
        dark.save(matching_jpeg, "JPEG", quality=95)
        Image.new("RGB", dark.size, (0, 0, 0)).save(black_jpeg, "JPEG", quality=95)
        self.assertEqual(dataset_prep._decoded_pixel_evidence(reference, matching_jpeg)["status"], "PASS")
        with self.assertRaisesRegex(BackendError, "pixel content does not match"):
            dataset_prep._decoded_pixel_evidence(reference, black_jpeg)

    def test_mixed_dataset_collision_manifest_and_cache_reuse(self):
        (self.photos / "same.jpg").write_bytes(b"jpeg-original-exact-bytes")
        (self.photos / "same.heic").write_bytes(b"heic-original-a")
        (self.photos / "nested").mkdir()
        (self.photos / "nested" / "same.heif").write_bytes(b"heic-original-b")
        originals = {path: sha256_file(path) for path in self.photos.rglob("*") if path.is_file()}
        result = self.prepare_with_test_converter(write_inventory(self.root, self.photos))
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        mappings = manifest["image_mappings"]
        outputs = [row["derived_relative_path"] for row in mappings]
        self.assertEqual(len(outputs), len(set(path.casefold() for path in outputs)))
        self.assertTrue(all("__" in Path(path).stem for path in outputs))
        self.assertEqual(manifest["jpeg_quality"], 95)
        self.assertEqual(manifest["conversion"]["jpeg_quality"], 95)
        self.assertEqual(manifest["converted_heif_count"], 2)
        self.assertEqual(manifest["copied_unchanged_count"], 1)
        for row in mappings:
            source = self.photos / row["source_relative_path"]
            self.assertEqual(sha256_file(source), originals[source])
            target = Path(result["effective_image_directory"]) / row["derived_relative_path"]
            if row["conversion"] == "COPIED_UNCHANGED":
                self.assertEqual(target.read_bytes(), source.read_bytes())
            else:
                self.assertEqual(target.suffix, ".jpg")
                self.assertEqual(row["conversion_validation"]["output_orientation"], 1)
                self.assertEqual(row["conversion_validation"]["jpeg_quality"], 95)

        with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
             patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
            self.assertTrue(dataset_prep.validate_preparation_stage_outputs(self.root, self.config))
            converted = next(Path(result["effective_image_directory"]).joinpath(row["derived_relative_path"])
                             for row in mappings if row["conversion"] == "HEIC_TO_JPEG_LIBHEIF")
            original_derivative = converted.read_bytes()
            converted.write_bytes(original_derivative + b"tamper")
            self.assertFalse(dataset_prep.validate_preparation_stage_outputs(self.root, self.config))
            converted.write_bytes(original_derivative)
            self.assertTrue(dataset_prep.validate_preparation_stage_outputs(self.root, self.config))

        first_mapping = mappings
        with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
             patch.object(dataset_prep, "_run_heif_convert_batch", side_effect=AssertionError("cache miss")), \
             patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
            reused = dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory_for(self.photos))
        self.assertTrue(reused["details"]["cache_reused"])
        second = json.loads(Path(reused["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(first_mapping, second["image_mappings"])

    def test_old_imageio_manifest_and_matching_hashes_do_not_reuse_derivatives(self):
        heic = self.photos / "frame.heic"
        heic.write_bytes(b"registered-heic-source")
        write_inventory(self.root, self.photos)
        old_id = "old-imageio-preparation"
        old_dataset = self.root / dataset_prep.PREP_DIR / old_id
        old_dataset.mkdir(parents=True)
        old_output = old_dataset / "frame.jpg"
        old_output.write_bytes(jpeg_header())
        old_manifest = {
            "schema_version": "exhibition-archive.spirula-image-dataset.v1",
            "status": "PREPARED", "project_id": self.config["project_id"],
            "source_directory": str(self.photos.resolve()),
            "effective_image_directory": str(old_dataset.resolve()),
            "preparation_fingerprint": old_id, "source_photographs_modified": False,
            "image_mappings": [{"source_relative_path": "frame.heic",
                                "derived_relative_path": "frame.jpg",
                                "derived_sha256": sha256_file(old_output),
                                "conversion": "HEIC_TO_JPEG_IMAGEIO"}],
        }
        write_json(old_dataset / dataset_prep.DATASET_MANIFEST, old_manifest)
        write_json(dataset_prep.preparation_manifest_path(self.root), old_manifest)

        with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
             patch.object(dataset_prep, "_run_heif_convert_batch", side_effect=fake_batch), \
             patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
            result = dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory_for(self.photos))
            effective, _ = dataset_prep.effective_sfm_dataset(self.root, self.config)
        self.assertFalse(result["details"]["cache_reused"])
        self.assertNotEqual(result["effective_image_directory"], old_dataset)
        self.assertEqual(effective, result["effective_image_directory"])
        self.assertEqual(sha256_file(old_output), old_manifest["image_mappings"][0]["derived_sha256"])

    def test_display_p3_is_not_accepted_as_dci_p3(self):
        manifest = {"status": "PREPARED", "image_mappings": [{
            "conversion": "HEIC_TO_JPEG_LIBHEIF",
            "conversion_validation": {"output_color_profile": "kCGColorSpaceDisplayP3"},
        }]}
        self.config["integrations"]["sfm"]["additional_args"] = ["--image-gamut", "Display-P3"]
        self.assertIsNone(dataset_prep.image_gamut_mismatch(self.config, manifest))
        self.config["integrations"]["sfm"]["additional_args"] = ["--image-gamut", "DCI-P3"]
        self.assertIn("does not match", dataset_prep.image_gamut_mismatch(self.config, manifest))
        unknown = {"status": "PREPARED", "image_mappings": [{
            "conversion": "HEIC_TO_JPEG_LIBHEIF", "conversion_validation": {"output_color_profile": None},
        }]}
        self.assertIn("cannot be verified", dataset_prep.image_gamut_mismatch(self.config, unknown))
        mixed = {"status": "PREPARED", "image_mappings": [
            {"conversion": "HEIC_TO_JPEG_LIBHEIF", "conversion_validation": {"output_color_profile": "sRGB"}},
            {"conversion": "COPIED_UNCHANGED", "derived_relative_path": "existing.jpg", "conversion_validation": None},
        ]}
        self.config["integrations"]["sfm"]["additional_args"] = ["--image-gamut", "sRGB"]
        self.assertIn("mixed dataset", dataset_prep.image_gamut_mismatch(self.config, mixed))

    def test_display_p3_icc_is_preserved_and_not_labeled_dci_p3(self):
        if not shutil.which("heif-convert") or not shutil.which("heif-enc"):
            self.skipTest("requires heif-convert and heif-enc")
        profiles = [Path("/System/Library/ColorSync/Profiles/Display P3.icc"),
                    Path("/Library/ColorSync/Profiles/Display P3.icc")]
        profile_path = next((path for path in profiles if path.is_file()), None)
        if profile_path is None:
            self.skipTest("a distributable system Display P3 ICC profile is unavailable")

        from PIL import Image, ImageDraw

        base_jpeg = self.root / "display-p3-fixture.jpg"
        heic = self.photos / "display-p3.heic"
        source = Image.new("RGB", (160, 120), (12, 20, 25))
        ImageDraw.Draw(source).rectangle((0, 0, 80, 120), fill=(160, 40, 20))
        exif = Image.Exif()
        exif[271] = "Apple"
        exif[272] = "iPhone P3 fixture"
        exif[274] = 1
        exif[34665] = {37386: (5, 1), 41989: 26}
        source.save(base_jpeg, "JPEG", quality=95, exif=exif,
                    icc_profile=profile_path.read_bytes())
        subprocess.run([shutil.which("heif-enc"), str(base_jpeg), "-o", str(heic)],
                       check=True, capture_output=True, text=True, timeout=60)

        result = dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory_for(self.photos))
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        details = manifest["image_mappings"][0]["conversion_validation"]
        self.assertEqual(details["output_color_profile"], "Display P3")
        self.assertEqual(details["source_icc_profile_sha256"], details["output_icc_profile_sha256"])
        self.config["integrations"]["sfm"]["additional_args"] = ["--image-gamut", "Display-P3"]
        self.assertIsNone(dataset_prep.image_gamut_mismatch(self.config, manifest))
        self.config["integrations"]["sfm"]["additional_args"] = ["--image-gamut", "DCI-P3"]
        self.assertIn("does not match", dataset_prep.image_gamut_mismatch(self.config, manifest))

    def test_source_change_invalidates_dataset_and_full_hash_plan(self):
        workflow_converter = patch("exhibition_archive.workflow.heif_converter_identity",
                                   return_value=FAKE_CONVERTER)
        workflow_converter.start()
        self.addCleanup(workflow_converter.stop)
        heic = self.photos / "frame.heic"
        heic.write_bytes(b"heic-original-v1")
        inventory = write_inventory(self.root, self.photos)
        state = ProjectState(self.root)
        try:
            first = self.prepare_with_test_converter(inventory)
            with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
                _validate_read_only_image_scope(self.root, self.config, first["effective_image_directory"])
                first_plan = expensive_execution_plan(self.root, self.config, state, "sfm", backend_info=BACKEND)

            heic.write_bytes(b"heic-original-v2")
            inventory = write_inventory(self.root, self.photos)
            second = self.prepare_with_test_converter(inventory)
            self.assertNotEqual(first["details"]["preparation_fingerprint"], second["details"]["preparation_fingerprint"])
            with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
                second_plan = expensive_execution_plan(self.root, self.config, state, "sfm", backend_info=BACKEND)
            self.assertNotEqual(first_plan["plan_id"], second_plan["plan_id"])
            self.assertEqual(second_plan["input_dataset"], str(second["effective_image_directory"]))
            self.assertEqual(second_plan["input_dataset_provenance"]["kind"], "ENGINE_VERIFIED_HEIC_JPEG_DERIVATIVES")
            self.assertEqual(second_plan["normalized_command"][3], str(second["effective_image_directory"]))
            with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
                effective_training = dataset_prep.effective_training_dataset(self.root, self.config)[0]
            self.assertEqual(effective_training, second["effective_image_directory"])
            self.config["integrations"]["model"]["image_directory"] = str(self.root / "different-training-images")
            with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
                with self.assertRaises(BackendError):
                    dataset_prep.effective_training_dataset(self.root, self.config)

            changed_settings = {**dataset_prep.PREP_SETTINGS, "test_conversion_revision": 2}
            with patch.object(dataset_prep, "PREP_SETTINGS", changed_settings), \
                 patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_run_heif_convert_batch", side_effect=fake_batch), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
                third = dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory)
                third_plan = expensive_execution_plan(self.root, self.config, state, "sfm", backend_info=BACKEND)
            self.assertNotEqual(second["details"]["preparation_fingerprint"], third["details"]["preparation_fingerprint"])
            self.assertNotEqual(second_plan["plan_id"], third_plan["plan_id"])
        finally:
            state.close()

    def test_failed_conversion_discards_staging_and_preserves_originals(self):
        heic = self.photos / "failed.heic"
        heic.write_bytes(b"original-remains-unchanged")
        before = sha256_file(heic)

        def interrupted(requests: list[dict[str, str]], _converter: dict, _timeout: int) -> dict:
            Path(requests[0]["output"]).write_bytes(b"partial-output")
            raise BackendError("controlled converter failure", failure_kind="CONVERSION_FAILED")

        with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
             patch.object(dataset_prep, "_run_heif_convert_batch", side_effect=interrupted):
            with self.assertRaises(BackendError):
                dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory_for(self.photos))
        self.assertEqual(sha256_file(heic), before)
        self.assertFalse(dataset_prep.preparation_manifest_path(self.root).exists())
        managed = self.root / dataset_prep.PREP_DIR
        self.assertEqual(list(managed.iterdir()), [])

    def test_workflow_prepares_then_stops_before_unauthorized_sfm_or_training(self):
        workflow_converter = patch("exhibition_archive.workflow.heif_converter_identity",
                                   return_value=FAKE_CONVERTER)
        workflow_converter.start()
        self.addCleanup(workflow_converter.stop)
        (self.photos / "one.heic").write_bytes(b"heic-one")
        (self.photos / "two.heic").write_bytes(b"heic-two")
        state = ProjectState(self.root)
        try:
            with patch.object(dataset_prep, "heif_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_run_heif_convert_batch", side_effect=fake_batch), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)), \
                 patch("exhibition_archive.workflow.expensive_execution_plan", return_value={
                     "plan_id": "fixture-current-plan", "backend": {},
                     "input_dataset": "managed-verified-data", "source_image_count": 2,
                     "resource_limits": {},
                 }), \
                 patch("exhibition_archive.workflow.execute_fresh_sfm", side_effect=AssertionError("SfM must not execute")), \
                 patch("exhibition_archive.workflow.execute_fresh_training", side_effect=AssertionError("training must not execute")):
                result = run_workflow(self.root, self.config, state)
            self.assertEqual(result["status"], "AUTHORIZATION_REQUIRED")
            self.assertEqual(state.stage("sfm_dataset_prep")["status"], "COMPLETED")
            self.assertEqual(state.stage("sfm")["status"], "BLOCKED")
            self.assertEqual(state.db.execute("SELECT COUNT(*) FROM stage_executions WHERE stage_id='sfm'").fetchone()[0], 0)
            self.assertEqual(state.db.execute("SELECT COUNT(*) FROM stage_executions WHERE stage_id='training'").fetchone()[0], 0)
        finally:
            state.close()

    @unittest.skipUnless(shutil.which("heif-convert") and shutil.which("heif-enc"),
                         "requires heif-convert and heif-enc")
    def test_real_heic_resolutions_orientation_metadata_icc_and_pixels(self):
        from PIL import Image, ImageCms, ImageDraw
        from PIL.TiffImagePlugin import IFDRational

        cases = [
            ("capture-24mp.heic", (5712, 4284), 1),
            ("capture-12mp.heic", (4032, 3024), 3),
        ]
        icc_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        for index, (name, size, orientation) in enumerate(cases):
            base_jpeg = self.root / f"fixture-input-{index}.jpg"
            heic = self.photos / name
            source = Image.new("RGB", size, (7, 9, 13))
            draw = ImageDraw.Draw(source)
            draw.rectangle((0, 0, size[0] // 2, size[1]), fill=(190, 42, 24))
            draw.rectangle((size[0] // 2, 0, size[0], size[1] // 3), fill=(24, 195, 72))
            draw.ellipse((size[0] // 3, size[1] // 4, size[0] * 2 // 3, size[1] * 3 // 4),
                         fill=(42, 76, 225))
            exif = Image.Exif()
            exif[271] = "Apple"
            exif[272] = "iPhone 16 Pro synthetic HEIF fixture"
            exif[274] = orientation
            exif[34665] = {37386: (425, 100), 41989: 24,
                            36867: "2026:10:31 12:00:00", 36868: "2026:10:31 12:00:00"}
            rational = lambda numerator, denominator: IFDRational(numerator, denominator)
            exif[34853] = {
                1: "N", 2: (rational(22, 1), rational(18, 1), rational(0, 1)),
                3: "E", 4: (rational(114, 1), rational(10, 1), rational(0, 1)),
                5: 0, 6: rational(12, 1),
                7: (rational(4, 1), rational(5, 1), rational(6, 1)),
                29: "2026:10:31",
            }
            source.save(base_jpeg, "JPEG", quality=95, exif=exif, icc_profile=icc_profile)
            subprocess.run([shutil.which("heif-enc"), "-q", "95", str(base_jpeg), "-o", str(heic)],
                           check=True, capture_output=True, text=True, timeout=180)
            base_jpeg.unlink()

        original_hashes = {path.name: sha256_file(path) for path in self.photos.iterdir()}
        result = dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory_for(self.photos))
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["conversion"]["backend"]["name"], "libheif heif-convert")
        self.assertEqual(manifest["conversion"]["jpeg_quality"], 95)
        self.assertEqual(len(manifest["image_mappings"]), 2)
        expected = {name: (size, orientation) for name, size, orientation in cases}
        dataset = Path(result["effective_image_directory"])
        for row in manifest["image_mappings"]:
            source_name = Path(row["source_relative_path"]).name
            expected_size, source_orientation = expected[source_name]
            target = dataset / row["derived_relative_path"]
            with Image.open(target) as converted:
                self.assertEqual(converted.format, "JPEG")
                self.assertEqual(converted.size, expected_size)
                tags = converted.getexif()
                self.assertEqual(tags.get(274, 1), 1)
                self.assertEqual(tags.get(271), "Apple")
                self.assertEqual(tags.get(272), "iPhone 16 Pro synthetic HEIF fixture")
                self.assertEqual(tags.get_ifd(34665).get(37386), (425, 100))
                self.assertEqual(tags.get_ifd(34665).get(41989), 24)
                gps = tags.get_ifd(34853)
                self.assertEqual(gps.get(1), "N")
                self.assertEqual(gps.get(3), "E")
                self.assertEqual(gps.get(29), "2026:10:31")
                self.assertTrue(converted.info.get("icc_profile"))
            details = row["conversion_validation"]
            self.assertEqual(row["conversion"], "HEIC_TO_JPEG_LIBHEIF")
            self.assertEqual(details["source_orientation"], source_orientation)
            self.assertEqual(details["output_orientation"], 1)
            self.assertEqual(details["jpeg_quality"], 95)
            self.assertEqual(details["pixel_content_validation"]["status"], "PASS")
            self.assertLess(details["pixel_content_validation"]["mean_absolute_channel_error"], 24)
            self.assertEqual(details["icc_profile_validation_status"], "ICC_PROFILE_BYTES_PRESERVED")
            self.assertEqual(details["source_icc_profile_sha256"], details["output_icc_profile_sha256"])
            self.assertIn("sRGB", details["output_color_profile"])
        self.assertIsNone(dataset_prep.image_gamut_mismatch(self.config, manifest))
        self.assertEqual({path.name: sha256_file(path) for path in self.photos.iterdir()}, original_hashes)
        expected_files = {row["derived_relative_path"] for row in manifest["image_mappings"]}
        expected_files.add(dataset_prep.DATASET_MANIFEST)
        actual_files = {path.relative_to(dataset).as_posix() for path in dataset.rglob("*") if path.is_file()}
        self.assertEqual(actual_files, expected_files)


if __name__ == "__main__":
    unittest.main()
