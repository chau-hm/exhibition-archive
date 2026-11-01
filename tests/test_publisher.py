from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from exhibition_archive.adapters import sha256_file, write_json
from exhibition_archive.publisher import build_website
from exhibition_archive.workflow import _website_reference_failures, verify_website_package_index


ROOT = Path(__file__).resolve().parents[1]


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


class GenericPublisherTests(unittest.TestCase):
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

            build_website(root, config, None, root / "deliverable" / "work")
            website = root / "deliverable" / "website"
            first_index_bytes = (website / "config" / "package-index.json").read_bytes()
            index = json.loads(first_index_bytes)
            self.assertEqual(index["schema_version"], "exhibition-archive.website-index.v2")
            self.assertEqual(index["project_id"], config["project_id"])
            self.assertEqual(index["model_sha256"], config["integrations"]["model"]["expected_sha256"])
            self.assertEqual(verify_website_package_index(website, index)[1], [])
            self.assertEqual(_website_reference_failures(website), [])

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

            build_website(root, config, None, root / "deliverable" / "work")
            self.assertEqual(first_index_bytes, (website / "config" / "package-index.json").read_bytes())
            self.assertEqual(
                sha256_file(website / "assets" / "exhibition.ply"),
                config["integrations"]["model"]["expected_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
