from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest
from PIL import Image

from exhibition_archive.backends import BackendError
from exhibition_archive.studio_export import export_spirula_colmap_dataset, validate_studio_dataset


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


def test_export_is_portable_and_structurally_valid_for_77_registered_images(tmp_path):
    names = [f"IMG_{index:04d}.jpg" for index in range(77)]
    images, reconstruction = tmp_path / "effective-images", tmp_path / "sparse" / "0"
    _write_images(images, names)
    _write_colmap_model(reconstruction, names)
    source_hashes = {name: (images / name).read_bytes() for name in names}
    sparse_bytes = {name: (reconstruction / name).read_bytes()
                    for name in ("cameras.bin", "images.bin", "points3D.bin")}

    result = export_spirula_colmap_dataset(images, reconstruction, tmp_path / "export",
                                           project_id="playground-fixture")
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
    assert list((tmp_path / "export").iterdir()) == []
