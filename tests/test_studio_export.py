from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

import pytest
from PIL import Image

from exhibition_archive.backends import BackendError
from exhibition_archive import studio_export, workflow
from exhibition_archive.studio_export import (build_studio_export_stage_report,
                                              export_spirula_colmap_dataset,
                                              validate_studio_dataset)


def _write_colmap_model(directory, names, *, dimensions=(100, 80), include_point=True):
    directory.mkdir(parents=True)
    width, height = dimensions
    with (directory / "cameras.bin").open("wb") as stream:
        stream.write(struct.pack("<Q", 1))
        # PINHOLE camera: fx, fy, cx, cy. All generated images share one intrinsics record.
        stream.write(struct.pack("<iiQQ4d", 1, 1, width, height, 100.0, 100.0,
                                 width / 2, height / 2))
    point_id = 7 if include_point else 0xFFFFFFFFFFFFFFFF
    poses_with_observation = min(2, len(names)) if include_point else 0
    with (directory / "images.bin").open("wb") as stream:
        stream.write(struct.pack("<Q", len(names)))
        for index, name in enumerate(names, 1):
            stream.write(struct.pack("<I7dI", index, 1.0, 0.0, 0.0, 0.0,
                                     0.0, 0.0, 0.0, 1))
            stream.write(name.encode("utf-8") + b"\0")
            if index <= poses_with_observation:
                stream.write(struct.pack("<Q2dQ", 1, width / 2, height / 2, point_id))
            else:
                stream.write(struct.pack("<Q", 0))
    with (directory / "points3D.bin").open("wb") as stream:
        stream.write(struct.pack("<Q", 1 if include_point else 0))
        if include_point:
            stream.write(struct.pack("<Q3d3BdQ", point_id, 0.0, 0.0, 4.0,
                                     120, 130, 140, 0.0, poses_with_observation))
            for image_id in range(1, poses_with_observation + 1):
                stream.write(struct.pack("<2i", image_id, 0))


def _write_images(directory, names, *, dimensions=(100, 80)):
    directory.mkdir(parents=True)
    for index, name in enumerate(names):
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", dimensions, (index % 256, 20, 30)).save(target, format="JPEG")


def test_export_is_portable_and_structurally_valid_for_77_registered_images(tmp_path, monkeypatch):
    names = [f"IMG_{index:04d}.jpg" for index in range(77)]
    images, reconstruction = tmp_path / "effective-images", tmp_path / "sparse" / "0"
    _write_images(images, names)
    _write_colmap_model(reconstruction, names)
    source_hashes = {name: (images / name).read_bytes() for name in names}
    sparse_bytes = {name: (reconstruction / name).read_bytes()
                    for name in ("cameras.bin", "images.bin", "points3D.bin")}

    result = export_spirula_colmap_dataset(images, reconstruction, tmp_path / "export",
                                           project_id="playground-fixture")
    monkeypatch.setattr(studio_export.shutil, "copy2",
                        lambda *_args, **_kwargs: pytest.fail("valid package reuse must not recopy JPEGs"))
    reused = export_spirula_colmap_dataset(images, reconstruction, tmp_path / "export",
                                           project_id="playground-fixture")
    package = tmp_path / "moved" / "spirula-project"
    package.parent.mkdir()
    shutil.move(result["package_directory"], package)
    validation = validate_studio_dataset(package)

    assert result["status"] == "EXPORTED_AND_STRUCTURALLY_VALIDATED"
    assert validation["image_count"] == 77
    assert validation["registered_image_names_resolve"] is True
    assert validation["camera_dimensions_match"] is True
    assert validation["portable_relative_layout"] is True
    assert validation["gui_reopen_status"] == "NOT_GUI_VERIFIED"
    assert validation["sparse_point_count"] == 1
    assert all((package / "sparse" / "0" / name).read_bytes() == data
               for name, data in sparse_bytes.items())
    assert all((images / name).read_bytes() == data for name, data in source_hashes.items())
    assert all((package / "images" / name).is_file() for name in names)
    manifest = (package / "manifest.json").read_text()
    assert str(images) not in manifest
    assert str(reconstruction) not in manifest

    assert reused["status"] == "REUSED_VALID_EXPORT"


def test_manifest_and_stage_report_serialize_nested_paths_as_portable_relative_paths(tmp_path):
    names = ["gallery/a.jpg", "gallery/b.jpg"]
    images, reconstruction = tmp_path / "effective-images", tmp_path / "sparse" / "0"
    _write_images(images, names)
    _write_colmap_model(reconstruction, names)
    provenance_file = images / "metadata" / "source-index.json"
    sparse_provenance = reconstruction / "images.bin"
    result = export_spirula_colmap_dataset(
        images, reconstruction, tmp_path / "output" / "sfm" / "spirula-studio-export",
        project_id="portable-fixture",
        source_provenance={"nested": {"image_record": provenance_file,
                                       "model_record": sparse_provenance,
                                       "relative_record": Path("metadata/derivative-map.json")}})
    package = Path(result["package_directory"])
    manifest_text = (package / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)

    result["validation"]["nested_metadata"] = {
        "paths": [package / "images" / "gallery" / "a.jpg",
                  {"manifest": package / "manifest.json"}],
    }
    stage_report = build_studio_export_stage_report(result, tmp_path / "output")
    report_text = json.dumps(stage_report, sort_keys=True)

    assert manifest["schema_version"] == "exhibition-archive.spirula-studio-colmap-export.v1"
    assert manifest["dataset_format"] == "COLMAP_BINARY"
    assert manifest["layout"] == "images/ + sparse/0/{cameras.bin,images.bin,points3D.bin}"
    assert manifest["source_provenance"]["nested"] == {
        "image_record": "images/metadata/source-index.json",
        "model_record": "sparse/0/images.bin",
        "relative_record": "metadata/derivative-map.json",
    }
    assert manifest["registered_image_names"] == names
    assert not manifest["compatibility_validation"]["gui_reopen"].startswith("PASS")
    assert str(tmp_path) not in manifest_text
    assert stage_report["schema_version"] == "exhibition-archive.spirula-export-stage.v1"
    assert stage_report["package_directory"] == package.relative_to(tmp_path / "output").as_posix()
    assert stage_report["manifest_path"] == (package / "manifest.json").relative_to(tmp_path / "output").as_posix()
    assert stage_report["validation"]["manifest_path"] == stage_report["manifest_path"]
    assert stage_report["validation"]["nested_metadata"]["paths"][0] == (
        package / "images" / "gallery" / "a.jpg").relative_to(tmp_path / "output").as_posix()
    assert str(tmp_path) not in report_text
    assert len(stage_report["artifacts"]) == 7
    assert validate_studio_dataset(package)["registered_image_names_resolve"] is True


def test_portable_report_rejects_paths_outside_authorized_output_root(tmp_path):
    result = {"status": "EXPORTED_AND_STRUCTURALLY_VALIDATED",
              "package_directory": tmp_path / "output" / "package",
              "manifest_path": tmp_path / "output" / "package" / "manifest.json",
              "validation": {"manifest_path": tmp_path / "outside" / "manifest.json"},
              "artifacts": [tmp_path / "output" / "package" / "manifest.json"]}

    with pytest.raises(BackendError, match="escapes the output root"):
        build_studio_export_stage_report(result, tmp_path / "output")


def test_export_stage_retry_writes_report_and_reuses_existing_image_package(tmp_path, monkeypatch):
    names = ["a.jpg", "b.jpg"]
    images, reconstruction = tmp_path / "images", tmp_path / "sparse" / "0"
    _write_images(images, names)
    _write_colmap_model(reconstruction, names)
    original_bytes = {name: (images / name).read_bytes() for name in names}
    output_root = tmp_path / "output"
    existing = export_spirula_colmap_dataset(images, reconstruction,
        output_root / "sfm" / "spirula-studio-export", project_id="workflow-retry-fixture")

    class State:
        @staticmethod
        def stage(stage_id):
            assert stage_id == "sfm"
            return {"details_json": json.dumps({"reconstruction_directory": str(reconstruction),
                                                "registered_image_count": len(names)})}

    monkeypatch.setattr(workflow, "effective_sfm_dataset", lambda *_args, **_kwargs: (images, None))
    monkeypatch.setattr(studio_export.shutil, "copy2",
                        lambda *_args, **_kwargs: pytest.fail("retry must reuse the complete content-addressed export"))
    stage_dir = output_root / "work" / "sfm_studio_export"
    stage_result = workflow.execute_sfm_studio_export(
        tmp_path, {"project_id": "workflow-retry-fixture", "output_directory": "output",
                  "integrations": {"sfm": {"mode": "spirula_fresh", "image_directory": str(images)}}},
        State(), stage_dir)
    report = json.loads((stage_dir / "export-report.json").read_text(encoding="utf-8"))

    assert stage_result["status"] == "COMPLETED"
    assert report["status"] == "REUSED_VALID_EXPORT"
    assert report["package_directory"] == Path(existing["package_directory"]).relative_to(output_root).as_posix()
    assert all(not Path(path).is_absolute() for path in report["artifacts"])
    assert str(tmp_path) not in json.dumps(report)
    assert all((images / name).read_bytes() == original_bytes[name] for name in names)


def test_interrupted_export_resumes_verified_staging_without_recopying_complete_images(tmp_path, monkeypatch):
    names = ["a.jpg", "b.jpg", "c.jpg"]
    images, reconstruction, output = tmp_path / "images", tmp_path / "sparse" / "0", tmp_path / "export"
    _write_images(images, names)
    _write_colmap_model(reconstruction, names)
    source_image_bytes = {name: (images / name).read_bytes() for name in names}
    sparse_bytes = {name: (reconstruction / name).read_bytes() for name in studio_export.SPARSE_FILES}
    real_copy2 = shutil.copy2

    def interrupt_second_image(source, destination):
        if Path(source).name == "b.jpg":
            Path(destination).write_bytes(b"partial image copy")
            raise KeyboardInterrupt("synthetic interruption during copy")
        return real_copy2(source, destination)

    monkeypatch.setattr(studio_export.shutil, "copy2", interrupt_second_image)
    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        export_spirula_colmap_dataset(images, reconstruction, output, project_id="retry-fixture")

    staging = next(output.glob(".revision-*.staging"))
    assert (staging / "images" / "a.jpg").is_file()
    assert (staging / "images" / "b.jpg.copying").is_file()

    retried_image_copies = []

    def count_retry_image_copies(source, destination):
        if Path(source).suffix.casefold() == ".jpg":
            retried_image_copies.append(Path(source).name)
        return real_copy2(source, destination)

    monkeypatch.setattr(studio_export.shutil, "copy2", count_retry_image_copies)
    result = export_spirula_colmap_dataset(images, reconstruction, output, project_id="retry-fixture")
    package = Path(result["package_directory"])

    assert result["status"] == "EXPORTED_AND_STRUCTURALLY_VALIDATED"
    assert retried_image_copies == ["b.jpg", "c.jpg"]
    assert not staging.exists()
    assert all((images / name).read_bytes() == source_image_bytes[name] for name in names)
    assert all((reconstruction / name).read_bytes() == sparse_bytes[name] for name in studio_export.SPARSE_FILES)
    assert validate_studio_dataset(package)["image_count"] == 3


def test_export_rejects_missing_image_mapping_and_cleans_staging(tmp_path):
    names = ["a.jpg", "b.jpg", "c.jpg"]
    images, reconstruction = tmp_path / "images", tmp_path / "sparse" / "0"
    _write_images(images, names[:2])
    _write_colmap_model(reconstruction, names)

    with pytest.raises(BackendError, match="registered COLMAP images are absent"):
        export_spirula_colmap_dataset(images, reconstruction, tmp_path / "export",
                                     project_id="missing-image")
    assert not (tmp_path / "export").exists()


def test_validation_detects_changed_or_missing_exported_image(tmp_path):
    names = ["a.jpg", "b.jpg", "c.jpg"]
    images, reconstruction = tmp_path / "images", tmp_path / "sparse" / "0"
    _write_images(images, names)
    _write_colmap_model(reconstruction, names)
    result = export_spirula_colmap_dataset(images, reconstruction, tmp_path / "export",
                                           project_id="changed-image")
    package = Path(result["package_directory"])
    (package / "images" / "a.jpg").unlink()

    with pytest.raises(BackendError, match="image membership or content"):
        validate_studio_dataset(package)


def test_export_rejects_camera_image_dimension_mismatch(tmp_path):
    names = ["a.jpg", "b.jpg"]
    images, reconstruction = tmp_path / "images", tmp_path / "sparse" / "0"
    _write_images(images, names, dimensions=(96, 80))
    _write_colmap_model(reconstruction, names, dimensions=(100, 80))

    with pytest.raises(BackendError, match="dimensions differ from COLMAP"):
        export_spirula_colmap_dataset(images, reconstruction, tmp_path / "export",
                                     project_id="dimension-mismatch")
    assert list((tmp_path / "export").glob("revision-*")) == []
    assert list((tmp_path / "export").glob(".revision-*.staging"))
