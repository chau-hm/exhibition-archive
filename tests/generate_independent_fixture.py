#!/usr/bin/env python3
"""Create a tiny independent exhibition project fixture for integration checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import zlib
from pathlib import Path


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def png(width: int, height: int, color: tuple[int, int, int]):
    def chunk(name: bytes, body: bytes):
        return struct.pack(">I", len(body)) + name + body + struct.pack(">I", zlib.crc32(name + body) & 0xffffffff)
    rows = b"".join(b"\0" + bytes(color) * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def make_sfm(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "cameras.bin").write_bytes(struct.pack("<QiiQQ3d", 1, 1, 0, 800, 600, 520.0, 400.0, 300.0))
    names = ["west_view.png", "east_view.png"]
    with (path / "images.bin").open("wb") as f:
        f.write(struct.pack("<Q", len(names)))
        for index, name in enumerate(names, 1):
            f.write(struct.pack("<I4d3dI", index, 1, 0, 0, 0, 0.0, 0.0, float(index), 1))
            f.write(name.encode() + b"\0")
            f.write(struct.pack("<Q", 0))
    (path / "points3D.bin").write_bytes(struct.pack("<Q", 0))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    base = args.destination.resolve()
    if base.exists() and any(base.iterdir()):
        raise SystemExit(f"Refusing to overwrite nonempty fixture directory: {base}")
    photos, inputs, workspace = base / "source-photos", base / "inputs", base / "workspace"
    photos.mkdir(parents=True, exist_ok=True); inputs.mkdir(parents=True, exist_ok=True)
    (photos / "east-wing").mkdir(exist_ok=True)
    (photos / "west-wing").mkdir(exist_ok=True)
    (photos / "west-wing" / "west_view.png").write_bytes(png(96, 64, (172, 183, 190)))
    (photos / "east-wing" / "east_view.png").write_bytes(png(128, 80, (192, 174, 146)))
    sfm_dir = inputs / "sparse-model-alpha"
    make_sfm(sfm_dir)

    model = inputs / "gallery-model.ply"
    model.write_text("ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nproperty float f_dc_0\nproperty float f_dc_1\nproperty float f_dc_2\nproperty float opacity\nproperty float scale_0\nproperty float scale_1\nproperty float scale_2\nproperty float rot_0\nproperty float rot_1\nproperty float rot_2\nproperty float rot_3\nend_header\n0 0 0 0 0 0 0 0 0 0 1 0 0 0\n", encoding="ascii")
    model_hash = hashlib.sha256(model.read_bytes()).hexdigest()

    envelope = {
        "schemaVersion": 1, "id": "north-room-envelope-alpha", "coordinateFrame": "fixture-local-nonmetric",
        "horizontalBoundary": {"classification": "CAMERA_DERIVED", "algorithm": "CONVEX_HULL", "closed": True, "candidateId": "room-hull-alpha", "marginUnits": 0.25, "marginBasis": "fixture spacing", "cameraClearanceUnits": 0.1, "areaUnits2": 100.0, "vertexCount": 4,
            "vertices2d": [[-5,-5],[5,-5],[5,5],[-5,5]], "verticesCommonViewer": [[-5,0,-5],[5,0,-5],[5,0,5],[-5,0,5]], "cameraStationInclusion": {"insideOrOn":2,"total":2}, "planeBasis": {"pointOnPlaneCommonViewer":[0,0,0],"basisU":[1,0,0],"basisV":[0,0,1],"normalUnit":[0,1,0]}},
        "floor": {"normal":[0,1,0],"offset":0.0,"clearance":0.15,"classification":"GEOMETRY_SUPPORTED"},
        "ceiling": {"heightAlongFloorNormal":3.5,"clearance":0.15,"classification":"INFERRED"},
        "navigation": {"horizontalClearanceUnits":0.1,"interiorObjectCollision":False}, "referenceWallCandidates": [], "limitations": ["Synthetic software fixture only."]
    }
    policy = {"schemaVersion":1,"status":"ACTIVE","sourceModelSha256":model_hash,"cameraPolicy":"architectural-envelope","navigation":{"interiorObjectCollision":False,"freeXYZWithinEnvelope":True}}
    write_json(inputs / "envelope-alpha.json", envelope); write_json(inputs / "policy-alpha.json", policy)
    stations = {"schemaVersion":1,"stationCount":2,"stations":[
        {"stationId":"west-station","imageKeys":["west_view"],"centerCommonViewer":[-1.0,1.4,0.0],"planeCoordinates":[-1.0,0.0],"representativeForwardCommonViewer":[1.0,0.0,-0.2],"normalVisitorPositionCandidate":True,"confidence":0.95},
        {"stationId":"east-station","imageKeys":["east_view"],"centerCommonViewer":[1.0,1.4,0.0],"planeCoordinates":[1.0,0.0],"representativeForwardCommonViewer":[-1.0,0.0,-0.2],"normalVisitorPositionCandidate":True,"confidence":0.95}
    ]}
    centers = {"schemaVersion":1,"stationCount":2,"principalImageKeys":[],"stations":[
        {"stationId":"west-station","imageKeys":["west_view"],"centerCommonViewer":[-1.0,1.4,0.0],"planeCoordinates":[-1.0,0.0]},
        {"stationId":"east-station","imageKeys":["east_view"],"centerCommonViewer":[1.0,1.4,0.0],"planeCoordinates":[1.0,0.0]}
    ]}
    tours = {"routes":[{"id":"route-alpha","title":"Gallery loop alpha","waypoints":[{"position":[-1.0,1.4,0.0],"target":[0.0,1.4,-1.0],"fov":70.75},{"position":[1.0,1.4,0.0],"target":[0.0,1.4,-1.0],"fov":70.75}]}]}
    region = {"candidateId":"region-citrine","commonCenter":[0.3,1.1,-1.0],"commonHalfExtents":[0.4,0.3,0.04],"commonRotationRows":[[1,0,0],[0,1,0],[0,0,1]]}
    candidate_registry = {"candidates":[region]}
    footprints = {"candidates":[{"id":"room-hull-alpha","algorithm":"CONVEX_HULL","marginUnits":0.25,"areaUnits2":100.0,"trustedStationInclusion":{"insideOrOn":2,"total":2},"polygon2d":[[-5,-5],[5,-5],[5,5],[-5,5]]}],"concaveAlphaSweep":[]}
    distribution = {"threeNearestNeighborEdges":[]}
    write_json(inputs / "stations-alpha.json", stations); write_json(inputs / "centers-alpha.json", centers); write_json(inputs / "tours-alpha.json", tours)
    write_json(inputs / "candidate-registry-alpha.json", candidate_registry); write_json(inputs / "footprints-alpha.json", footprints); write_json(inputs / "distribution-alpha.json", distribution)
    write_json(inputs / "transform-alpha.json", {"transform":{"viewerSceneNode":{"translation":[0,0,0],"rotationQuaternionXYZW":[0,0,0,1],"uniformScale":1.0}}})
    write_json(inputs / "geometry-alpha.json", {"candidateId":"region-citrine","candidate":region})

    config = {
        "schema_version":"exhibition-archive.project.v1","project_id":"citrine-gallery-fixture","created_at_utc":"2026-10-28T00:00:00Z",
        "source":{"photo_directory":"../source-photos","read_only":True},"output_directory":"deliverable",
        "viewer":{"title":"Citrine Gallery Fixture","theme":"dark","public_geometry_view_ids":["region-citrine"],"default_geometry_view_id":"region-citrine"},
        "publication":{"public_site":True,"include_source_photographs":False,"include_private_review_notes":False,"artwork_reproduction_rights":"UNASSESSED"},
        "archive_policy":{"bundle_source_photographs":False,"known_limitations":["Synthetic fixture; no reconstruction-quality claim."]},"retry_policy":{"max_failed_attempts":2,"automatic_retries":0},
        "integrations":{"sfm":{"directory":"../inputs/sparse-model-alpha","format":"COLMAP_BINARY"},"model":{"path":"../inputs/gallery-model.ply","expected_sha256":model_hash,"expected_size_bytes":model.stat().st_size,"expected_gaussian_count":1,"trainer":"synthetic fixture model"},
            "navigation":{"envelope":"../inputs/envelope-alpha.json","runtime_policy":"../inputs/policy-alpha.json","camera_centers":"../inputs/centers-alpha.json","stations":"../inputs/stations-alpha.json","guided_tours":"../inputs/tours-alpha.json","candidate_registry":"../inputs/candidate-registry-alpha.json","footprints":"../inputs/footprints-alpha.json","distribution":"../inputs/distribution-alpha.json","transform":"../inputs/transform-alpha.json","hanging_text_geometry":"../inputs/geometry-alpha.json","default_station_id":"west-station","principal_image_keys":[]}}
    }
    write_json(base / "project-config.json", config)
    print(json.dumps({"fixture_root":str(base),"project_config":str(base / "project-config.json"),"workspace":str(workspace),"project_id":config["project_id"],"source_image_count":2,"synthetic_model_sha256":model_hash},indent=2))


if __name__ == "__main__":
    main()
