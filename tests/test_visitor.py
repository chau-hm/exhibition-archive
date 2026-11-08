from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

from exhibition_archive.visitor import (
    CAMERA_COLLISION_RADIUS,
    common_to_viewer,
    enclosure_collision_glb,
    visitor_camera_settings,
    viewer_transform_node,
)


ROOT = Path(__file__).resolve().parents[1]


class VisitorCoordinateTests(unittest.TestCase):
    def test_common_frame_pose_maps_to_official_viewer_local_frame(self):
        transform = {
            "viewerSceneNode": {
                "translation": [4.0, -1.0, 0.5],
                "rotationQuaternionXYZW": [0.0, 0.0, 1.0, 0.0],
                "uniformScale": 2.0,
            }
        }
        # D * (scale * R * native + translation) for native=[1,2,3],
        # R=180 degrees about Z, and D=180 degrees about Z.
        common = [-2.0, 5.0, 6.5]
        self.assertEqual(common_to_viewer(common, viewer_transform_node(transform)), (-1.0, -2.0, 3.0))

    def test_initial_camera_uses_default_trusted_station(self):
        envelope = {"id": "fixture-envelope"}
        stations = {
            "defaultStationId": "east",
            "stations": [
                {"stationId": "west", "centerCommonViewer": [-1, 1.5, 0], "representativeForwardCommonViewer": [1, 0, 0]},
                {"stationId": "east", "centerCommonViewer": [1, 1.5, 0], "representativeForwardCommonViewer": [0, 0, -1]},
            ],
        }
        settings, record = visitor_camera_settings(envelope, stations)
        self.assertEqual(record["station_id"], "east")
        self.assertEqual(record["coordinate_frame"], "SuperSplat Y-up world frame")
        self.assertEqual(settings["cameras"][0]["initial"]["position"], [1.0, 1.5, 0.0])
        self.assertEqual(settings["cameras"][0]["initial"]["target"], [1.0, 1.5, -2.0])
        self.assertEqual(record["pose_supersplat"], settings["cameras"][0]["initial"])
        self.assertEqual(settings["cameras"][0]["initial"]["fov"], 70.75)
        self.assertEqual(settings["startMode"], "default")


class VisitorCollisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        fixture = Path(self.temp.name) / "fixture"
        import subprocess
        import sys
        subprocess.run(
            [sys.executable, str(ROOT / "tests" / "generate_independent_fixture.py"), str(fixture)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.envelope = json.loads((fixture / "inputs" / "envelope-alpha.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def parse_glb(data: bytes) -> dict:
        magic, version, total_length = struct.unpack_from("<III", data, 0)
        json_length, json_type = struct.unpack_from("<II", data, 12)
        assert magic == 0x46546C67
        assert version == 2
        assert total_length == len(data)
        assert json_type == 0x4E4F534A
        return json.loads(data[20:20 + json_length].decode("utf-8"))

    def test_glb_contains_only_offset_enclosure_shell_for_fly_collision(self):
        data, metadata = enclosure_collision_glb(self.envelope)
        document = self.parse_glb(data)
        mesh = document["meshes"][0]
        primitive = mesh["primitives"][0]
        vertex_count = document["accessors"][primitive["attributes"]["POSITION"]]["count"]
        triangle_count = document["accessors"][primitive["indices"]]["count"] // 3

        self.assertEqual(len(document["meshes"]), 1)
        self.assertEqual(mesh["name"], "Enclosure shell only")
        self.assertEqual(vertex_count, 8)  # two 4-corner loops
        self.assertEqual(triangle_count, 12)  # four side quads and triangulated caps
        self.assertFalse(metadata["contains_interior_objects"])
        self.assertEqual(metadata["camera_radius_viewer_units"], CAMERA_COLLISION_RADIUS)
        self.assertAlmostEqual(metadata["expanded_shell_outboard_common_units"], 0.1)
        self.assertAlmostEqual(metadata["floor_collision_height_common_units"], -0.05)
        self.assertAlmostEqual(metadata["ceiling_collision_height_common_units"], 3.55)

    def test_rejects_degenerate_enclosure(self):
        malformed = json.loads(json.dumps(self.envelope))
        malformed["horizontalBoundary"]["vertices2d"] = [[0, 0], [1, 0], [2, 0]]
        with self.assertRaisesRegex(ValueError, "zero area"):
            enclosure_collision_glb(malformed)

    def test_virtual_exploration_limits_create_free_xyz_collision_shell_without_claiming_a_ceiling(self):
        polygon = [[-2, -2], [2, -2], [2, 2], [-2, 2]]
        limits = {"upNormal": [0, 1, 0], "originCommonViewer": [0, 0, 0],
                  "minimumHeight": 0.25, "maximumHeight": 4.5,
                  "classification": "REVIEWED_EXPLORATION_LIMIT", "physicalCeiling": False}
        envelope = {"floor": None, "ceiling": None, "explorationLimits": limits,
                    "horizontalBoundary": {"vertices2d": polygon, "cameraClearanceUnits": 0,
                                           "planeBasis": {"normalUnit": [0, 1, 0], "offset": 0,
                                                          "pointOnPlaneCommonViewer": [0, 0, 0],
                                                          "basisU": [1, 0, 0], "basisV": [0, 0, -1]},
                                           "regions": [{"regionId": "region-01", "vertices2d": polygon,
                                                        "explorationLimits": limits}]}}
        stations = {"defaultStationId": "start", "stations": [
            {"stationId": "start", "regionId": "region-01", "centerCommonViewer": [0, 2, 0],
             "representativeForwardCommonViewer": [0, 0, -1]}]}
        settings, camera = visitor_camera_settings(envelope, stations)
        self.assertEqual(settings["cameras"][0]["initial"]["position"], [0, 2, 0])
        self.assertEqual(camera["pose_supersplat"]["position"], [0, 2, 0])
        self.assertAlmostEqual(camera["enclosure_clearance"]["minimum_exploration_margin_common_units"], 1.75)
        self.assertAlmostEqual(camera["enclosure_clearance"]["maximum_exploration_margin_common_units"], 2.5)
        glb, metadata = enclosure_collision_glb(envelope)
        document = self.parse_glb(glb)
        self.assertEqual(document["accessors"][0]["count"], 8)
        self.assertIn("virtual exploration limits", metadata["regions"][0]["vertical_kind"])
        self.assertFalse(envelope["explorationLimits"]["physicalCeiling"])

        outside = json.loads(json.dumps(stations))
        outside["stations"][0]["centerCommonViewer"] = [0, 4.6, 0]
        with self.assertRaisesRegex(ValueError, "exploration envelope"):
            visitor_camera_settings(envelope, outside)

    def test_camera_exactly_on_approved_polygon_edge_is_inclusive_and_shell_is_offset(self):
        polygon = [[-2, -2], [2, -2], [2, 2], [-2, 2]]
        limits = {"upNormal": [0, 1, 0], "originCommonViewer": [0, 0, 0],
                  "minimumHeight": 0.3, "maximumHeight": 3.5,
                  "classification": "REVIEWED_EXPLORATION_LIMIT", "physicalCeiling": False}
        envelope = {"id": "edge-start", "floor": None, "ceiling": None,
                    "explorationLimits": limits,
                    "horizontalBoundary": {
                        "vertices2d": polygon, "cameraClearanceUnits": 0,
                        "planeBasis": {"normalUnit": [0, 1, 0], "offset": 0,
                                       "pointOnPlaneCommonViewer": [0, 0, 0],
                                       "basisU": [1, 0, 0], "basisV": [0, 0, -1]},
                        "regions": [{"regionId": "region-01", "vertices2d": polygon,
                                     "explorationLimits": limits}],
                    }}
        stations = {"defaultStationId": "edge-station", "stations": [
            {"stationId": "edge-station", "regionId": "region-01",
             "centerCommonViewer": [2, 1, 0], "representativeForwardCommonViewer": [0, 0, -1]}]}
        settings, camera = visitor_camera_settings(envelope, stations)
        self.assertEqual(settings["cameras"][0]["initial"]["position"], [2.0, 1.0, 0.0])
        self.assertAlmostEqual(camera["enclosure_clearance"]["nearest_wall_distance_common_units"], 0.0)
        self.assertAlmostEqual(camera["enclosure_clearance"]["minimum_wall_margin_common_units"], 0.0)
        _glb, collision = enclosure_collision_glb(envelope)
        self.assertAlmostEqual(collision["expanded_shell_outboard_common_units"], CAMERA_COLLISION_RADIUS)
        self.assertAlmostEqual(collision["floor_collision_height_common_units"],
                               limits["minimumHeight"] - CAMERA_COLLISION_RADIUS)
        self.assertAlmostEqual(collision["ceiling_collision_height_common_units"],
                               limits["maximumHeight"] + CAMERA_COLLISION_RADIUS)

    def test_concave_approved_boundary_uses_safe_inset_and_triangulated_caps(self):
        polygon = [[0, 0], [3, 0], [3, 1], [1, 1], [1, 3], [0, 3]]
        limits = {"upNormal": [0, 1, 0], "originCommonViewer": [0, 0, 0],
                  "minimumHeight": 0, "maximumHeight": 4, "physicalCeiling": False}
        envelope = {"explorationLimits": limits,
                    "horizontalBoundary": {"vertices2d": polygon, "cameraClearanceUnits": 0,
                                           "planeBasis": {"normalUnit": [0, 1, 0], "offset": 0,
                                                          "pointOnPlaneCommonViewer": [0, 0, 0],
                                                          "basisU": [1, 0, 0], "basisV": [0, 0, -1]},
                                           "regions": [{"regionId": "l", "vertices2d": polygon,
                                                        "explorationLimits": limits}]}}
        _data, metadata = enclosure_collision_glb(envelope)
        self.assertEqual(metadata["outward_margin_fallback_regions"], ["l"])
        self.assertEqual(metadata["triangle_count"], 20)


if __name__ == "__main__":
    unittest.main()
