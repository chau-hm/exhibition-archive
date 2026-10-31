from __future__ import annotations

import json
import platform
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

FAKE_CONVERTER = {"name": "ImageIO test double", "version": "test-1", "helper_sha256": "test-helper"}


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
            "source_color_profile": "kCGColorSpaceSRGB", "output_color_profile": "kCGColorSpaceSRGB",
            "color_profile_validation_status": "PROFILE_NAME_MATCHED",
            "color_handling": "synthetic test double",
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
        with patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
             patch.object(dataset_prep, "_run_imageio_batch", side_effect=fake_batch), \
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

        with patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
             patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
            self.assertTrue(dataset_prep.validate_preparation_stage_outputs(self.root, self.config))
            converted = next(Path(result["effective_image_directory"]).joinpath(row["derived_relative_path"])
                             for row in mappings if row["conversion"] == "HEIC_TO_JPEG_IMAGEIO")
            original_derivative = converted.read_bytes()
            converted.write_bytes(original_derivative + b"tamper")
            self.assertFalse(dataset_prep.validate_preparation_stage_outputs(self.root, self.config))
            converted.write_bytes(original_derivative)
            self.assertTrue(dataset_prep.validate_preparation_stage_outputs(self.root, self.config))

        first_mapping = mappings
        with patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
             patch.object(dataset_prep, "_run_imageio_batch", side_effect=AssertionError("cache miss")), \
             patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
            reused = dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory_for(self.photos))
        self.assertTrue(reused["details"]["cache_reused"])
        second = json.loads(Path(reused["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(first_mapping, second["image_mappings"])

    def test_display_p3_is_not_accepted_as_dci_p3(self):
        manifest = {"status": "PREPARED", "image_mappings": [{
            "conversion": "HEIC_TO_JPEG_IMAGEIO",
            "conversion_validation": {"output_color_profile": "kCGColorSpaceDisplayP3"},
        }]}
        self.config["integrations"]["sfm"]["additional_args"] = ["--image-gamut", "Display-P3"]
        self.assertIsNone(dataset_prep.image_gamut_mismatch(self.config, manifest))
        self.config["integrations"]["sfm"]["additional_args"] = ["--image-gamut", "DCI-P3"]
        self.assertIn("does not match", dataset_prep.image_gamut_mismatch(self.config, manifest))
        unknown = {"status": "PREPARED", "image_mappings": [{
            "conversion": "HEIC_TO_JPEG_IMAGEIO", "conversion_validation": {"output_color_profile": None},
        }]}
        self.assertIn("cannot be verified", dataset_prep.image_gamut_mismatch(self.config, unknown))
        mixed = {"status": "PREPARED", "image_mappings": [
            {"conversion": "HEIC_TO_JPEG_IMAGEIO", "conversion_validation": {"output_color_profile": "kCGColorSpaceSRGB"}},
            {"conversion": "COPIED_UNCHANGED", "derived_relative_path": "existing.jpg", "conversion_validation": None},
        ]}
        self.config["integrations"]["sfm"]["additional_args"] = ["--image-gamut", "sRGB"]
        self.assertIn("mixed dataset", dataset_prep.image_gamut_mismatch(self.config, mixed))

    def test_source_change_invalidates_dataset_and_full_hash_plan(self):
        heic = self.photos / "frame.heic"
        heic.write_bytes(b"heic-original-v1")
        inventory = write_inventory(self.root, self.photos)
        state = ProjectState(self.root)
        try:
            first = self.prepare_with_test_converter(inventory)
            with patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
                _validate_read_only_image_scope(self.root, self.config, first["effective_image_directory"])
                first_plan = expensive_execution_plan(self.root, self.config, state, "sfm", backend_info=BACKEND)

            heic.write_bytes(b"heic-original-v2")
            inventory = write_inventory(self.root, self.photos)
            second = self.prepare_with_test_converter(inventory)
            self.assertNotEqual(first["details"]["preparation_fingerprint"], second["details"]["preparation_fingerprint"])
            with patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
                second_plan = expensive_execution_plan(self.root, self.config, state, "sfm", backend_info=BACKEND)
            self.assertNotEqual(first_plan["plan_id"], second_plan["plan_id"])
            self.assertEqual(second_plan["input_dataset"], str(second["effective_image_directory"]))
            self.assertEqual(second_plan["input_dataset_provenance"]["kind"], "ENGINE_VERIFIED_HEIC_JPEG_DERIVATIVES")
            self.assertEqual(second_plan["normalized_command"][3], str(second["effective_image_directory"]))
            with patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
                effective_training = dataset_prep.effective_training_dataset(self.root, self.config)[0]
            self.assertEqual(effective_training, second["effective_image_directory"])
            self.config["integrations"]["model"]["image_directory"] = str(self.root / "different-training-images")
            with patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_jpeg_dimensions", return_value=(24, 32)):
                with self.assertRaises(BackendError):
                    dataset_prep.effective_training_dataset(self.root, self.config)

            changed_settings = {**dataset_prep.PREP_SETTINGS, "test_conversion_revision": 2}
            with patch.object(dataset_prep, "PREP_SETTINGS", changed_settings), \
                 patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_run_imageio_batch", side_effect=fake_batch), \
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

        with patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
             patch.object(dataset_prep, "_run_imageio_batch", side_effect=interrupted):
            with self.assertRaises(BackendError):
                dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory_for(self.photos))
        self.assertEqual(sha256_file(heic), before)
        self.assertFalse(dataset_prep.preparation_manifest_path(self.root).exists())
        managed = self.root / dataset_prep.PREP_DIR
        self.assertEqual(list(managed.iterdir()), [])

    def test_workflow_prepares_then_stops_before_unauthorized_sfm_or_training(self):
        (self.photos / "one.heic").write_bytes(b"heic-one")
        (self.photos / "two.heic").write_bytes(b"heic-two")
        state = ProjectState(self.root)
        try:
            with patch.object(dataset_prep, "imageio_converter_identity", return_value=FAKE_CONVERTER), \
                 patch.object(dataset_prep, "_run_imageio_batch", side_effect=fake_batch), \
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

    @unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swift") and
                         shutil.which("heif-enc"), "requires macOS ImageIO, Swift, and heif-enc")
    def test_real_heic_imageio_decode_exif_orientation_and_readable_jpeg(self):
        try:
            from PIL import Image
            from PIL import ImageCms
        except ImportError:
            self.skipTest("Pillow is needed only to create and inspect the synthetic real-HEIC fixture")

        base_jpeg = self.root / "fixture-input.jpg"
        heic = self.photos / "iphone-frame.heic"
        source = Image.new("RGB", (32, 24), (180, 40, 20))
        exif = Image.Exif()
        exif[271] = "Apple"
        exif[272] = "iPhone Synthetic"
        exif[274] = 6
        exif[34665] = {37386: (5, 1), 41989: 26}
        icc_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        source.save(base_jpeg, "JPEG", quality=95, exif=exif, icc_profile=icc_profile)
        subprocess.run([shutil.which("heif-enc"), str(base_jpeg), "-o", str(heic)],
                       check=True, capture_output=True, text=True, timeout=60)
        base_jpeg.unlink()
        original_hash = sha256_file(heic)

        result = dataset_prep.prepare_spirula_dataset(self.root, self.config, inventory_for(self.photos))
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        row = manifest["image_mappings"][0]
        target = Path(result["effective_image_directory"]) / row["derived_relative_path"]
        with Image.open(target) as converted:
            converted.verify()
        with Image.open(target) as converted:
            self.assertEqual(converted.size, (24, 32))
            tags = converted.getexif()
            self.assertEqual(tags.get(274), 1)
            self.assertEqual(tags.get(271), "Apple")
            self.assertEqual(tags.get(272), "iPhone Synthetic")
            self.assertEqual(tags.get_ifd(34665).get(41989), 26)
        self.assertEqual(sha256_file(heic), original_hash)
        self.assertEqual(row["conversion_validation"]["source_orientation"], 6)
        self.assertEqual(row["conversion_validation"]["output_orientation"], 1)
        self.assertEqual(row["conversion_validation"]["jpeg_quality"], 95)
        self.assertEqual(row["conversion_validation"]["icc_profile_validation_status"], "ICC_PROFILE_BYTES_PRESERVED")
        self.assertEqual(row["conversion_validation"]["source_icc_profile_byte_count"],
                         row["conversion_validation"]["output_icc_profile_byte_count"])
        self.assertIsNone(dataset_prep.image_gamut_mismatch(self.config, manifest))
        self.assertEqual(row["conversion_validation"]["retained_metadata_fields"],
                         ["camera_make", "camera_model", "focal_length_35mm"])


if __name__ == "__main__":
    unittest.main()
