from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from exhibition_archive.adapters import sha256_file, write_json
from exhibition_archive.publisher import _convert_sog, build_website, register_external_sog
from exhibition_archive.workflow import _website_reference_failures, verify_website_package_index


ROOT = Path(__file__).resolve().parents[1]


def write_sog_fixture(path: Path, count: int = 1, sh_n: bool = False) -> None:
    from zipfile import ZIP_DEFLATED, ZipFile

    path.parent.mkdir(parents=True, exist_ok=True)
    files = {key: f"{key}.webp" for key in ("means", "scales", "quats", "sh0")}
    if sh_n:
        files["shN"] = "shN_labels.webp"
    metadata = {"version": 2, "asset": {"generator": "splat-transform v3.10.3"}, "count": count}
    for key, name in files.items():
        metadata[key] = {"files": [name]}
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("meta.json", json.dumps(metadata))
        for name in files.values():
            archive.writestr(name, b"fixture webp bytes")


def package_index_for(website: Path) -> dict:
    files = [
        {"path": path.relative_to(website).as_posix(), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(website.rglob("*"))
        if path.is_file() and path.name != "package-index.json"
    ]
    return {
        "schema_version": "exhibition-archive.website-index.v2",
        "project_id": "fixture",
        "model_sha256": "0" * 64,
        "file_count": len(files),
        "total_bytes": sum(row["size_bytes"] for row in files),
        "files": files,
    }


class WebsitePackageIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.website = Path(self.temp.name) / "website"
        (self.website / "config").mkdir(parents=True)
        (self.website / "viewer").mkdir()
        (self.website / "index.html").write_text('<a href="./viewer/">Viewer</a>', encoding="utf-8")
        (self.website / "viewer" / "index.html").write_text("viewer", encoding="utf-8")
        self.index = package_index_for(self.website)
        write_json(self.website / "config" / "package-index.json", self.index)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_index_covers_every_payload_file_and_excludes_itself(self):
        checks, failures = verify_website_package_index(self.website, self.index)
        self.assertEqual(failures, [])
        self.assertEqual(len(checks), 2)

    def test_index_rejects_changed_missing_extra_duplicate_and_unsafe_paths(self):
        changed = copy.deepcopy(self.index)
        changed["files"][0]["sha256"] = "f" * 64
        self.assertTrue(any("checksum/path" in failure for failure in verify_website_package_index(self.website, changed)[1]))

        (self.website / "unlisted.txt").write_text("extra", encoding="utf-8")
        self.assertTrue(any("omits payload files" in failure for failure in verify_website_package_index(self.website, self.index)[1]))
        (self.website / "unlisted.txt").unlink()

        (self.website / "viewer" / "index.html").unlink()
        self.assertTrue(any("checksum/path" in failure or "omits payload files" in failure for failure in verify_website_package_index(self.website, self.index)[1]))
        (self.website / "viewer" / "index.html").write_text("viewer", encoding="utf-8")

        duplicate = copy.deepcopy(self.index)
        duplicate["files"].append(copy.deepcopy(duplicate["files"][0]))
        self.assertTrue(any("duplicate path" in failure for failure in verify_website_package_index(self.website, duplicate)[1]))

        unsafe = copy.deepcopy(self.index)
        unsafe["files"][0]["path"] = "../outside.txt"
        self.assertTrue(any("checksum/path" in failure or "lists files" in failure for failure in verify_website_package_index(self.website, unsafe)[1]))

        includes_self = copy.deepcopy(self.index)
        includes_self["files"].append({"path": "config/package-index.json", "size_bytes": 0, "sha256": "0" * 64})
        self.assertTrue(any("checksum/path" in failure for failure in verify_website_package_index(self.website, includes_self)[1]))


class WebsiteReferenceTests(unittest.TestCase):
    def test_resolves_local_html_css_and_javascript_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            website = Path(tmp)
            (website / "viewer" / "vendor").mkdir(parents=True)
            (website / "viewer" / "index.html").write_text(
                '<link href="./style.css"><script type="module" src="./app.js"></script>', encoding="utf-8"
            )
            (website / "viewer" / "style.css").write_text(".icon{background:url('./icon.png')}", encoding="utf-8")
            (website / "viewer" / "app.js").write_text("import {x} from './vendor/runtime.js'; fetch('../api/health.json');", encoding="utf-8")
            for name in ("icon.png",):
                (website / "viewer" / name).write_bytes(b"asset")
            (website / "viewer" / "vendor" / "runtime.js").write_text("export const x=1", encoding="utf-8")
            (website / "api").mkdir()
            (website / "api" / "health.json").write_text("{}", encoding="utf-8")
            self.assertEqual(_website_reference_failures(website), [])

    def test_rejects_root_absolute_and_missing_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            website = Path(tmp)
            (website / "viewer").mkdir()
            (website / "viewer" / "index.html").write_text('<script src="/assets/app.js"></script>', encoding="utf-8")
            (website / "viewer" / "app.js").write_text("import './missing.js';", encoding="utf-8")
            failures = _website_reference_failures(website)
            self.assertTrue(any("root-absolute" in failure for failure in failures))
            self.assertTrue(any("missing" in failure for failure in failures))


class SogConversionTests(unittest.TestCase):
    def test_timeout_preserves_conversion_settings_and_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted-master.ply"
            source.write_bytes(b"master fixture")
            target = root / "assets" / "exhibition.sog"
            timeout = subprocess.TimeoutExpired("splat-transform", 600, output=b"SH clustering 31%", stderr=b"peak working set 123 MiB")
            gpu = subprocess.CompletedProcess(["splat-transform", "--list-gpus"], 0, "[0] Apple M4 Pro\n", "")
            with (
                patch.dict(os.environ, {"EXHIBITION_ARCHIVE_BUILD_SOG": "1", "EXHIBITION_ARCHIVE_SOG_DEVICE": "auto"}),
                patch("exhibition_archive.publisher._splat_transform_binary", return_value="splat-transform"),
                patch("exhibition_archive.publisher._tool_version", return_value="splat-transform v3.10.3 (fixture)"),
                patch("exhibition_archive.publisher.subprocess.run", side_effect=[gpu, timeout]) as run,
                patch("exhibition_archive.publisher.time.monotonic", side_effect=[10.0, 610.0]),
            ):
                result = _convert_sog(source, target)

            self.assertEqual(result["status"], "TIMEOUT")
            self.assertEqual(result["tool"], "@playcanvas/splat-transform CLI")
            self.assertEqual(result["tool_version"], "splat-transform v3.10.3 (fixture)")
            self.assertEqual(result["settings"]["sh_band_clustering_iterations"], 10)
            self.assertEqual(result["settings"]["gpu"], 0)
            self.assertEqual(result["settings"]["workers"], 4)
            self.assertEqual(result["elapsed_time_limit_seconds"], 600)
            self.assertEqual(result["elapsed_seconds"], 600.0)
            self.assertEqual(result["stdout_tail"], "SH clustering 31%")
            self.assertEqual(result["stderr_tail"], "peak working set 123 MiB")
            self.assertEqual(result["input_size_bytes"], len(b"master fixture"))
            self.assertEqual(result["node_old_space_limit_mb"], 8192)
            self.assertFalse(target.exists())
            convert_command = run.call_args_list[1].args[0]
            self.assertEqual(convert_command[1:3], ["-g", "0"])
            self.assertIn("-i", convert_command)
            self.assertNotIn("cpu", convert_command)

    def test_success_records_duration_size_hash_and_compression_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted-master.ply"
            source.write_bytes(b"a sufficiently large accepted master fixture")
            target = root / "assets" / "exhibition.sog"
            gpu = subprocess.CompletedProcess(["splat-transform", "--list-gpus"], 0, "[0] Apple M4 Pro\n", "")

            def convert(command, **_kwargs):
                output = Path(command[command.index(str(source)) + 1])
                write_sog_fixture(output, count=1, sh_n=True)
                return subprocess.CompletedProcess(command, 0, "encoded SOG", "memory sample")

            def run(command, **kwargs):
                return gpu if "--list-gpus" in command else convert(command, **kwargs)

            with (
                patch.dict(os.environ, {"EXHIBITION_ARCHIVE_BUILD_SOG": "1"}),
                patch("exhibition_archive.publisher._splat_transform_binary", return_value="splat-transform"),
                patch("exhibition_archive.publisher._tool_version", return_value="splat-transform v3.10.3 (fixture)"),
                patch("exhibition_archive.publisher.subprocess.run", side_effect=run),
                patch("exhibition_archive.publisher._tool_info", side_effect=[
                    {"format": "sog", "gaussian": True, "numGaussians": 1, "shBands": 3},
                    {"format": "ply", "gaussian": True, "numGaussians": 1, "shBands": 3},
                ]),
                patch("exhibition_archive.publisher.time.monotonic", side_effect=[3.0, 5.5]),
            ):
                result = _convert_sog(source, target)

            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["elapsed_seconds"], 2.5)
            self.assertEqual(result["output_size_bytes"], target.stat().st_size)
            self.assertEqual(result["compression_ratio_output_to_input"], target.stat().st_size / source.stat().st_size)
            self.assertEqual(result["output_sha256"], sha256_file(target))
            self.assertEqual(result["stdout_tail"], "encoded SOG")
            self.assertEqual(result["stderr_tail"], "memory sample")
            self.assertEqual(result["input_gaussian_count"], 1)
            self.assertEqual(result["input_sh_bands"], 3)

    def test_no_visible_gpu_blocks_conversion_instead_of_falling_back_to_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted-master.ply"
            source.write_bytes(b"master fixture")
            no_gpu = subprocess.CompletedProcess(["splat-transform", "--list-gpus"], 0,
                                                 "No GPU adapters found.\n", "")
            with (
                patch.dict(os.environ, {"EXHIBITION_ARCHIVE_BUILD_SOG": "1", "EXHIBITION_ARCHIVE_SOG_DEVICE": "auto"}),
                patch("exhibition_archive.publisher._splat_transform_binary", return_value="splat-transform"),
                patch("exhibition_archive.publisher._tool_version", return_value="splat-transform v3.10.3 (fixture)"),
                patch("exhibition_archive.publisher.subprocess.run", return_value=no_gpu) as run,
            ):
                result = _convert_sog(source, root / "assets" / "exhibition.sog")

            self.assertEqual(result["status"], "GPU_UNAVAILABLE")
            self.assertIn("no CPU fallback", result["reason"])
            run.assert_called_once()

    def test_conversion_is_not_started_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted-master.ply"
            source.write_bytes(b"master fixture")
            with (
                patch.dict(os.environ, {"EXHIBITION_ARCHIVE_BUILD_SOG": ""}),
                patch("exhibition_archive.publisher._splat_transform_binary", return_value="splat-transform"),
                patch("exhibition_archive.publisher.subprocess.run") as run,
            ):
                result = _convert_sog(source, root / "assets" / "exhibition.sog")

            self.assertEqual(result["status"], "NOT_REQUESTED")
            run.assert_not_called()


class SogImportTests(unittest.TestCase):
    def test_external_import_binds_source_and_sog_integrity_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted-master.ply"
            source.write_bytes(b"accepted PLY fixture")
            sog = root / "external.sog"
            write_sog_fixture(sog, count=1, sh_n=True)
            config = {"project_id": "fixture", "output_directory": "output", "integrations": {"model": {
                "path": str(source), "expected_sha256": sha256_file(source), "expected_gaussian_count": 1}}}
            infos = [
                {"format": "ply", "gaussian": True, "numGaussians": 1, "shBands": 3},
                {"format": "sog", "gaussian": True, "numGaussians": 1, "shBands": 3},
            ] * 2
            with (
                patch("exhibition_archive.publisher._splat_transform_binary", return_value="splat-transform"),
                patch("exhibition_archive.publisher._tool_version", return_value="splat-transform v3.10.3 (fixture)"),
                patch("exhibition_archive.publisher._tool_info", side_effect=infos),
            ):
                registered = register_external_sog(
                    root, config, sog, converter_version="splat-transform v3.10.3 (4f90012)",
                    command="splat-transform -g 0 --verbose --memory accepted-master.ply exhibition.sog -i 10",
                    device="Apple M4 Pro", gpu_selection="0", sh_iterations=10,
                    execution_metadata={"elapsed_seconds": 17.274, "gpu_peak_memory": "122.3 MB", "cpu_peak_memory": "1.21 GB"},
                )
                repeat = register_external_sog(
                    root, config, sog, converter_version="splat-transform v3.10.3 (4f90012)",
                    command="splat-transform -g 0 --verbose --memory accepted-master.ply exhibition.sog -i 10",
                    device="Apple M4 Pro", gpu_selection="0", sh_iterations=10,
                )

            self.assertEqual(registered["status"], "VALIDATED_EXTERNAL")
            self.assertEqual(registered["input_sha256"], sha256_file(source))
            self.assertEqual(registered["output_sha256"], sha256_file(sog))
            self.assertEqual(registered["gaussian_count"], 1)
            self.assertEqual(registered["sh_bands"], 3)
            self.assertEqual(repeat["status"], "ALREADY_REGISTERED")
            provenance = json.loads(Path(registered["provenance"]).read_text(encoding="utf-8"))
            self.assertEqual(provenance["converter"]["gpu_selection"], "0")
            self.assertEqual(provenance["converter"]["execution_metadata"]["elapsed_seconds"], 17.274)
            self.assertEqual(provenance["converter"]["execution_metadata"]["gpu_peak_memory"], "122.3 MB")
            self.assertEqual(provenance["sog_format"]["format"], "sog")

    def test_external_import_rejects_wrong_converter_and_source_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted-master.ply"
            source.write_bytes(b"accepted PLY fixture")
            sog = root / "external.sog"
            write_sog_fixture(sog, count=1)
            config = {"project_id": "fixture", "output_directory": "output", "integrations": {"model": {
                "path": str(source), "expected_sha256": "0" * 64, "expected_gaussian_count": 1}}}
            with self.assertRaisesRegex(ValueError, "hash does not match"):
                register_external_sog(root, config, sog, converter_version="3.10.3", command="splat-transform ...")

            config["integrations"]["model"]["expected_sha256"] = sha256_file(source)
            infos = [
                {"format": "ply", "gaussian": True, "numGaussians": 1, "shBands": 0},
                {"format": "sog", "gaussian": True, "numGaussians": 1, "shBands": 0},
            ]
            with (
                patch("exhibition_archive.publisher._splat_transform_binary", return_value="splat-transform"),
                patch("exhibition_archive.publisher._tool_version", return_value="splat-transform v3.10.3 (fixture)"),
                patch("exhibition_archive.publisher._tool_info", side_effect=infos),
            ):
                with self.assertRaisesRegex(ValueError, "does not match the SOG generator"):
                    register_external_sog(root, config, sog, converter_version="splat-transform v3.8.0", command="splat-transform ...")


class GenericPublisherTests(unittest.TestCase):
    def test_registered_external_sog_builds_modular_sog_primary_package_without_ply(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "fixture"
            subprocess.run([sys.executable, str(ROOT / "tests" / "generate_independent_fixture.py"), str(fixture)],
                           check=True, stdout=subprocess.PIPE, text=True)
            config = json.loads((fixture / "project-config.json").read_text(encoding="utf-8"))
            root = fixture / "workspace"
            source = (root / config["integrations"]["model"]["path"]).resolve()
            external_sog = fixture / "gpu-generated.sog"
            write_sog_fixture(external_sog, count=1, sh_n=False)
            confirmed = root / "deliverable" / "work" / "confirmed-artworks.json"
            confirmed.parent.mkdir(parents=True)
            write_json(confirmed, {"count": 0, "artworks": []})
            with (
                patch("exhibition_archive.publisher._splat_transform_binary", return_value="splat-transform"),
                patch("exhibition_archive.publisher._tool_version", return_value="splat-transform v3.10.3 (fixture)"),
                patch("exhibition_archive.publisher._tool_info", side_effect=[
                    {"format": "ply", "gaussian": True, "numGaussians": 1, "shBands": 0},
                    {"format": "sog", "gaussian": True, "numGaussians": 1, "shBands": 0},
                ]),
            ):
                import_result = register_external_sog(
                    root, config, external_sog, converter_version="splat-transform v3.10.3 (fixture)",
                    command="splat-transform -g 0 accepted-master.ply exhibition.sog -i 10",
                    device="Test GPU", gpu_selection="0", sh_iterations=10,
                )
            self.assertEqual(import_result["status"], "VALIDATED_EXTERNAL")

            with patch.dict(os.environ, {"EXHIBITION_ARCHIVE_SPLAT_TRANSFORM": str(root / "missing-splat-transform")}):
                build_website(root, config, None, root / "deliverable" / "work")
            website = root / "deliverable" / "website-visitor"
            metadata = json.loads((website / "config" / "exhibition.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["primary_format"], "sog")
            self.assertEqual(metadata["conversion"]["status"], "VALIDATED_EXTERNAL")
            self.assertEqual(metadata["models"]["sog"]["available"], True)
            self.assertEqual(metadata["models"]["ply"]["available"], False)
            self.assertFalse((website / "assets" / "exhibition.ply").exists())
            self.assertEqual(sha256_file(website / "assets" / "exhibition.sog"), sha256_file(external_sog))
            index_bytes = (website / "config" / "package-index.json").read_bytes()
            index = json.loads(index_bytes)
            self.assertEqual(verify_website_package_index(website, index)[1], [])
            self.assertEqual(_website_reference_failures(website), [])
            self.assertEqual(metadata["master"]["sha256"], config["integrations"]["model"]["expected_sha256"])

            with patch.dict(os.environ, {"EXHIBITION_ARCHIVE_SPLAT_TRANSFORM": str(root / "missing-splat-transform")}):
                build_website(root, config, None, root / "deliverable" / "work")
            self.assertEqual(index_bytes, (website / "config" / "package-index.json").read_bytes())

    def test_build_is_metadata_driven_and_reproducible_for_independent_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "fixture"
            subprocess.run(
                [sys.executable, str(ROOT / "tests" / "generate_independent_fixture.py"), str(fixture)],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            config = json.loads((fixture / "project-config.json").read_text(encoding="utf-8"))
            config["viewer"]["title"] = "Citrine & North Gallery"
            root = fixture / "workspace"
            confirmed = root / "deliverable" / "work" / "confirmed-artworks.json"
            confirmed.parent.mkdir(parents=True)
            write_json(confirmed, {"count": 0, "artworks": []})

            with patch.dict(os.environ, {"EXHIBITION_ARCHIVE_SPLAT_TRANSFORM": str(root / "missing-splat-transform")}):
                build_website(root, config, None, root / "deliverable" / "work")
            website = root / "deliverable" / "website"
            visitor = root / "deliverable" / "website-visitor"
            first_index_bytes = (website / "config" / "package-index.json").read_bytes()
            first_visitor_index_bytes = (visitor / "config" / "package-index.json").read_bytes()
            index = json.loads(first_index_bytes)
            self.assertEqual(index["schema_version"], "exhibition-archive.website-index.v2")
            self.assertEqual(index["project_id"], config["project_id"])
            self.assertEqual(index["model_sha256"], config["integrations"]["model"]["expected_sha256"])
            self.assertEqual(verify_website_package_index(website, index)[1], [])
            self.assertEqual(_website_reference_failures(website), [])
            visitor_index = json.loads((visitor / "config" / "package-index.json").read_text(encoding="utf-8"))
            self.assertEqual(visitor_index["profile"], "visitor")
            self.assertEqual(visitor_index["model_sha256"], config["integrations"]["model"]["expected_sha256"])
            self.assertEqual(verify_website_package_index(visitor, visitor_index)[1], [])
            self.assertEqual(_website_reference_failures(visitor), [])
            visitor_metadata = json.loads((visitor / "config" / "exhibition.json").read_text(encoding="utf-8"))
            review_camera = json.loads((website / "api" / "initial-camera.json").read_text(encoding="utf-8"))
            self.assertEqual(visitor_metadata["initial_camera"]["pose_common_viewer"], review_camera["pose_common_viewer"])
            self.assertEqual(visitor_metadata["conversion"]["expected_tool_version"], "3.10.3")
            self.assertEqual(visitor_metadata["primary_format"], "ply")
            self.assertEqual(visitor_metadata["release_authorization"], "NEEDS_REVIEW")
            self.assertEqual(visitor_metadata["conversion"]["status"], "NOT_REQUESTED")
            self.assertEqual(
                sha256_file(visitor / "assets" / "exhibition.ply"),
                config["integrations"]["model"]["expected_sha256"],
            )
            self.assertFalse((visitor / "assets" / "exhibition.sog").exists())
            self.assertTrue((visitor / "assets" / "enclosure-collision.glb").is_file())
            self.assertTrue((visitor / "js" / "viewer.js").is_file())
            self.assertTrue((visitor / "css" / "viewer.css").is_file())
            self.assertTrue((visitor / "vendor" / "supersplat-viewer.js").is_file())
            self.assertTrue((visitor / "config" / "locales.json").is_file())
            visitor_html = (visitor / "index.html").read_text(encoding="utf-8").casefold()
            self.assertIn('id="resetview"', visitor_html)
            self.assertIn('id="helptoggle"', visitor_html)
            self.assertIn('id="languageselect"', visitor_html)
            self.assertIn('./js/viewer.js', visitor_html)
            self.assertIn('./css/viewer.css', visitor_html)
            self.assertNotIn("tour", visitor_html)
            self.assertNotIn("diagnostic", visitor_html)
            self.assertEqual(visitor_html.count("<select"), 1)
            locales = json.loads((visitor / "config" / "locales.json").read_text(encoding="utf-8"))
            self.assertIn("zh-Hant", locales)
            self.assertIn("en", locales)
            self.assertIn("E / Q", " ".join(locales["en"]["instructions"]))

            html = (website / "viewer" / "index.html").read_text(encoding="utf-8")
            app = (website / "viewer" / "app.js").read_text(encoding="utf-8")
            health = json.loads((website / "api" / "health.json").read_text(encoding="utf-8"))
            tours = json.loads((website / "api" / "tours.json").read_text(encoding="utf-8"))
            self.assertIn("Citrine &amp; North Gallery", html)
            self.assertIn(config["project_id"], html)
            self.assertNotIn("__EXHIBITION_TITLE__", html)
            self.assertIn("health.contentUrl", app)
            self.assertNotIn("/assets/", app)
            self.assertNotIn('value="debug"', html)
            self.assertNotIn("Debug Fly", html)
            self.assertNotIn('id="candidateSelect"', html)
            self.assertNotIn('id="cameraClearance"', html)
            self.assertNotIn("setSetupPose", app)
            self.assertNotIn("tryCameraPose", app)
            self.assertNotIn("chuen", app.casefold())
            self.assertEqual(health["modelFilename"], "exhibition.ply")
            self.assertEqual([route["id"] for route in tours["routes"]], ["guided-route-01"])
            self.assertTrue(all(set(route) == {"id", "title", "waypoints"} for route in tours["routes"]))
            self.assertNotIn("IMG_", json.dumps(tours))
            self.assertFalse((website / "source-images").exists())
            self.assertIn("visitors need no Exhibition Archive Engine or Python runtime", (website / "README.md").read_text(encoding="utf-8"))

            with patch.dict(os.environ, {"EXHIBITION_ARCHIVE_SPLAT_TRANSFORM": str(root / "missing-splat-transform")}):
                build_website(root, config, None, root / "deliverable" / "work")
            self.assertEqual(first_index_bytes, (website / "config" / "package-index.json").read_bytes())
            self.assertEqual(first_visitor_index_bytes, (visitor / "config" / "package-index.json").read_bytes())
            self.assertEqual(
                sha256_file(website / "assets" / "exhibition.ply"),
                config["integrations"]["model"]["expected_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
