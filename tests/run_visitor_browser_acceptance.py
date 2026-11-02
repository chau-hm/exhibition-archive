#!/usr/bin/env python3
"""Run real-Chrome visitor acceptance at root and nested static URL paths."""
from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

from exhibition_archive.adapters import read_json, sha256_file
from exhibition_archive.visitor import VIEWER_VERSION, common_to_supersplat, viewer_transform_node
from exhibition_archive.workflow import _website_reference_failures, verify_website_package_index


ROOT = Path(__file__).resolve().parents[1]
PHASE4C_ACCEPTED_HASH = "bebbec74f033deed6bf4df8811fbd6537947f9e3ba07d684622a79a4f62d9d3e"


class StaticServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class StaticHandler(BaseHTTPRequestHandler):
    website: Path
    request_log: list[dict]
    request_lock: threading.Lock

    def log_message(self, _format: str, *args) -> None:
        return

    def _send(self, include_body: bool) -> None:
        started = time.perf_counter()
        parsed = urlsplit(self.path)
        decoded = unquote(parsed.path)
        request_path = decoded
        if decoded == "/nested":
            decoded = "/"
        elif decoded.startswith("/nested/"):
            decoded = decoded[len("/nested"):]
        relative = decoded.lstrip("/")
        candidate = (self.website / relative).resolve()
        try:
            candidate.relative_to(self.website.resolve())
        except ValueError:
            candidate = self.website / "__unsafe__"
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            payload = b"404 Not Found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if include_body:
                self.wfile.write(payload)
            with self.request_lock:
                self.request_log.append({"path": request_path, "status": 404, "bytes": len(payload),
                                         "range": self.headers.get("Range"),
                                         "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
            return

        size = candidate.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if match:
                start = int(match.group(1))
                end = min(size - 1, int(match.group(2)) if match.group(2) else size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    with self.request_lock:
                        self.request_log.append({"path": request_path, "status": 416, "bytes": 0,
                                                 "range": range_header,
                                                 "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
                    return
                status = 206
        response_size = end - start + 1
        mime, _ = mimetypes.guess_type(str(candidate))
        if candidate.suffix.casefold() in {".ply", ".sog", ".glb"}:
            mime = "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(response_size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=3600")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        sent = 0
        if include_body:
            with candidate.open("rb") as handle:
                handle.seek(start)
                remaining = response_size
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
        else:
            sent = response_size
        with self.request_lock:
            self.request_log.append({"path": request_path, "status": status, "bytes": sent,
                                     "range": range_header,
                                     "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})

    def do_GET(self) -> None:
        self._send(True)

    def do_HEAD(self) -> None:
        self._send(False)


def chrome_binary(value: str | None) -> str:
    candidate = value or os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(candidate).is_file():
        raise SystemExit(f"Chrome executable not found: {candidate}")
    return str(Path(candidate).resolve())


def _viewpoints(review_website: Path, visitor_metadata: dict, transform_node: dict) -> list[dict]:
    default = visitor_metadata["initial_camera"]["pose_supersplat"]
    up_alignment = visitor_metadata["initial_camera"]["common_to_supersplat_rotation_quaternion_xyzw"]
    result = [{"id": "opening", "label": "accepted opening camera", "pose": default,
               "source": "Phase 4C trusted default station"}]
    tours_path = review_website / "api" / "tours.json"
    tours = read_json(tours_path)
    for route_id, label in (("guided-route-02", "close photographic-wall view"),
                            ("guided-route-03", "headphone-installation view")):
        route = next((item for item in tours.get("routes", []) if item.get("id") == route_id), None)
        if not route or not route.get("waypoints"):
            continue
        waypoint = route["waypoints"][-1]
        result.append({
            "id": route_id,
            "label": label,
            "source": f"Review-only {route_id} final camera, used here as a fixed comparison viewpoint",
            "pose": {
                "position": list(common_to_supersplat(waypoint["position"], up_alignment)),
                "target": list(common_to_supersplat(waypoint["target"], up_alignment)),
                "fov": waypoint.get("fov", default.get("fov", 70.75)),
            },
        })
    if len(result) != 3:
        raise ValueError("The Review profile does not contain the expected photographic-wall and installation camera records")

    envelope = read_json(review_website / "api" / "boundary.json")
    boundary = envelope["horizontalBoundary"]
    polygon = boundary["vertices2d"]
    lengths = [math.hypot(polygon[(index + 1) % len(polygon)][0] - point[0],
                          polygon[(index + 1) % len(polygon)][1] - point[1])
               for index, point in enumerate(polygon)]
    edge_index = max(range(len(lengths)), key=lengths.__getitem__)
    a, b = polygon[edge_index], polygon[(edge_index + 1) % len(polygon)]
    dx, dy = b[0] - a[0], b[1] - a[1]
    edge_length = math.hypot(dx, dy)
    area = sum(polygon[index][0] * polygon[(index + 1) % len(polygon)][1] -
               polygon[(index + 1) % len(polygon)][0] * polygon[index][1]
               for index in range(len(polygon)))
    outward_uv = (dy / edge_length, -dx / edge_length) if area >= 0 else (-dy / edge_length, dx / edge_length)
    midpoint = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    start_offset = boundary["cameraClearanceUnits"] + 2.0
    start_uv = (midpoint[0] - outward_uv[0] * start_offset,
                midpoint[1] - outward_uv[1] * start_offset)
    normal = envelope["floor"]["normal"]
    basis = boundary["planeBasis"]
    def common_at(uv, height):
        point = [basis["pointOnPlaneCommonViewer"][axis] + basis["basisU"][axis] * uv[0] + basis["basisV"][axis] * uv[1]
                 for axis in range(3)]
        current_height = sum(point[axis] * normal[axis] for axis in range(3)) + envelope["floor"]["offset"]
        return [point[axis] + normal[axis] * (height - current_height) for axis in range(3)]
    start_common = common_at(start_uv, envelope["floor"]["clearance"] + 1.4)
    outward_common = [basis["basisU"][axis] * outward_uv[0] + basis["basisV"][axis] * outward_uv[1]
                      for axis in range(3)]
    target_common = [start_common[axis] + outward_common[axis] * 3.0 for axis in range(3)]
    result.append({
        "id": "wall-boundary",
        "label": "longest accepted enclosure wall",
        "source": "test-only camera directed at the longest accepted enclosure edge",
        "pose": {"position": list(common_to_supersplat(start_common, up_alignment)),
                 "target": list(common_to_supersplat(target_common, up_alignment)), "fov": 70.75},
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--website", required=True, type=Path)
    parser.add_argument("--review-website", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--chrome")
    parser.add_argument("--root-only", action="store_true", help="Run the full root-path browser matrix without the separate nested-path smoke run")
    parser.add_argument("--motion-smoke", action="store_true", help="Load once and compare public Fly movement at the authored opening pose")
    parser.add_argument("--collision-disabled", action="store_true", help="Run the motion smoke test without the enclosure collision asset")
    parser.add_argument("--input-smoke", action="store_true", help="Test native keyboard and mouse Fly controls immediately after a fresh load")
    parser.add_argument("--headed", action="store_true", help="Use visible Chrome instead of headless Chrome for browser acceptance")
    args = parser.parse_args()

    website = args.website.resolve()
    review_website = args.review_website.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    screenshots = output / "screenshots"
    screenshots.mkdir(exist_ok=True)
    metadata = read_json(website / "config" / "exhibition.json")
    review_health = read_json(review_website / "api" / "health.json")
    review_routes = read_json(review_website / "api" / "tours.json").get("routes", [])
    if len(review_routes) != 5:
        raise SystemExit(f"Expected the accepted Review profile to retain all five guided routes; found {len(review_routes)}")
    if (website / "api" / "tours.json").exists():
        raise SystemExit("Visitor package must not contain guided route data")
    envelope = read_json(review_website / "api" / "boundary.json")
    transform_doc = read_json(review_website / "api" / "transform.json")
    transform_node = viewer_transform_node(transform_doc)
    index = read_json(website / "config" / "package-index.json")
    index_checks, index_failures = verify_website_package_index(website, index)
    ref_failures = _website_reference_failures(website)
    visitor_hash = sha256_file(website / "assets" / "exhibition.ply")
    if visitor_hash != metadata["master"]["sha256"] or visitor_hash != review_health["sha256"]:
        raise SystemExit("Visitor and Review PLY copies do not match the Phase 4C accepted master")
    if visitor_hash != PHASE4C_ACCEPTED_HASH:
        raise SystemExit("The accepted Phase 4C PLY SHA-256 changed; refusing browser acceptance")
    if index_failures or ref_failures:
        raise SystemExit(json.dumps({"package_index_failures": index_failures, "reference_failures": ref_failures}, indent=2))

    case_data = {
        "primary_format": metadata["primary_format"],
        "models": metadata["models"],
        "master_sha256": visitor_hash,
        "envelope": envelope,
        "transform_node": transform_node,
        "up_alignment": metadata["initial_camera"]["common_to_supersplat_rotation_quaternion_xyzw"],
        "viewpoints": _viewpoints(review_website, metadata, transform_node),
        "expected_ui_buttons": ["resetView", "helpToggle"],
        "profile": metadata["profile"],
        "release_authorization": metadata["release_authorization"],
    }
    case_file = output / "acceptance-cases.json"
    case_file.write_text(json.dumps(case_data, indent=2) + "\n", encoding="utf-8")

    server = StaticServer(("127.0.0.1", 0), StaticHandler)
    StaticHandler.website = website
    StaticHandler.request_log = []
    StaticHandler.request_lock = threading.Lock()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    server_url = f"http://127.0.0.1:{server.server_address[1]}"

    chrome = chrome_binary(args.chrome)
    chrome_version = subprocess.run([chrome, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="exhibition-archive-visitor-chrome-") as temp_dir:
        temp = Path(temp_dir)
        driver = ROOT / "tests" / "visitor_browser_driver.mjs"

        def run_browser(name: str, extra_args: list[str]) -> dict:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            debug_port = sock.getsockname()[1]
            sock.close()
            chrome_log = output / f"chrome-{name}.log"
            chrome_args = [
                chrome, *([] if args.headed else ["--headless=new"]), "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-background-networking", "--disable-component-update", "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", "--remote-allow-origins=*",
                f"--remote-debugging-port={debug_port}", f"--user-data-dir={temp / ('profile-' + name)}", "about:blank",
            ]
            with chrome_log.open("w", encoding="utf-8") as log:
                browser = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=log)
                try:
                    deadline = time.time() + 30
                    debug_url = f"http://127.0.0.1:{debug_port}"
                    while time.time() < deadline:
                        if browser.poll() is not None:
                            raise RuntimeError(f"Chrome exited early with code {browser.returncode}; see {chrome_log}")
                        try:
                            with urlopen(debug_url + "/json/version", timeout=1) as response:
                                response.read()
                            break
                        except Exception:
                            time.sleep(0.2)
                    else:
                        raise RuntimeError(f"Chrome DevTools endpoint did not become ready for {name}")
                    command = ["node", str(driver), "--cdp", debug_url, "--site", server_url,
                               "--cases", str(case_file), "--screenshots", str(screenshots), *extra_args]
                    browser_result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
                    if browser_result.returncode:
                        raise RuntimeError(f"Visitor browser driver ({name}) failed.\nSTDOUT:\n" + browser_result.stdout[-8000:] + "\nSTDERR:\n" + browser_result.stderr[-8000:])
                    return json.loads(browser_result.stdout)
                finally:
                    browser.terminate()
                    try:
                        browser.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        browser.kill()
                        browser.wait(timeout=5)

        driver_args = []
        if args.motion_smoke:
            driver_args.extend(["--motion-smoke", "1"])
        if args.collision_disabled:
            driver_args.extend(["--no-collision", "1"])
        if args.input_smoke:
            driver_args.extend(["--input-smoke", "1"])
        root_report = run_browser("root", driver_args)
        nested_report = None if args.root_only or args.motion_smoke or args.input_smoke else run_browser("nested", ["--nested-smoke", "1"])
        browser_reports = [root_report] + ([nested_report] if nested_report else [])
        browser_report = {
            "status": "PASS" if all(item["status"] == "PASS" for item in browser_reports) else "FAIL",
            "checks": [check for item in browser_reports for check in item["checks"]],
            "browser_errors": [error for item in browser_reports for error in item["browser_errors"]],
            "console_errors": [error for item in browser_reports for error in item["console_errors"]],
            "network_errors": [error for item in browser_reports for error in item["network_errors"]],
            "performance": [row for item in browser_reports for row in item["performance"]],
            "screenshots": [capture for item in browser_reports for capture in item["screenshots"]],
        }

    server.shutdown()
    server.server_close()
    with StaticHandler.request_lock:
        requests = list(StaticHandler.request_log)
    (output / "http-requests.json").write_text(json.dumps(requests, indent=2) + "\n", encoding="utf-8")

    report = {
        "schema_version": "exhibition-archive.visitor-browser-acceptance.v1",
        "status": browser_report["status"],
        "website": str(website),
        "review_website": str(review_website),
        "browser": chrome_version,
        "viewer_version": metadata["viewer_runtime"]["viewer"]["version"],
        "playcanvas_version": metadata["viewer_runtime"]["engine"]["version"],
        "primary_format": metadata["primary_format"],
        "conversion": metadata["conversion"],
        "master_sha256": visitor_hash,
        "master_size_bytes": (website / "assets" / "exhibition.ply").stat().st_size,
        "sog_size_bytes": (website / "assets" / "exhibition.sog").stat().st_size if (website / "assets" / "exhibition.sog").is_file() else None,
        "collision_sha256": metadata["collision"]["sha256"],
        "collision_size_bytes": metadata["collision"]["size_bytes"],
        "review_guided_route_count": len(review_routes),
        "visitor_index_file_count": len(index_checks),
        "package_index_failures": index_failures,
        "reference_failures": ref_failures,
        "checks": browser_report["checks"],
        "browser_errors": browser_report["browser_errors"],
        "console_errors": browser_report["console_errors"],
        "network_errors": browser_report["network_errors"],
        "performance": browser_report["performance"],
        "image_comparison": [],
        "http_request_count": len(requests),
        "served_model_bytes": sum(item["bytes"] for item in requests if item["path"].endswith((".sog", ".ply"))),
        "http_failures": [item for item in requests if item["status"] >= 400],
        "screenshots": browser_report["screenshots"],
        "evidence_directory": str(output),
    }

    for viewpoint in ("opening", "guided-route-02", "guided-route-03"):
        sog = screenshots / f"{viewpoint}-sog.png"
        ply = screenshots / f"{viewpoint}-ply.png"
        if sog.is_file() and ply.is_file():
            comparison = compare_pngs(sog, ply)
            comparison["viewpoint"] = viewpoint
            comparison["label"] = next((item["label"] for item in case_data["viewpoints"] if item["id"] == viewpoint), viewpoint)
            report["image_comparison"].append(comparison)

    if report["http_failures"]:
        report["status"] = "FAIL"
    if browser_report["status"] != "PASS":
        report["status"] = "FAIL"
    json_path = output / "visitor-browser-acceptance.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (output / "visitor-browser-acceptance.md").write_text(render_report(report), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


def _decode_png(path: Path) -> tuple[int, int, bytes]:
    import struct
    import zlib

    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a PNG image: {path}")
    width = height = color_type = bit_depth = interlace = None
    compressed = bytearray()
    offset = 8
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            compressed.extend(body)
        elif kind == b"IEND":
            break
    if bit_depth != 8 or interlace != 0 or color_type not in {2, 6}:
        raise ValueError(f"Unsupported PNG encoding in {path}: bit depth {bit_depth}, color type {color_type}, interlace {interlace}")
    source_bpp = 4 if color_type == 6 else 3
    raw = zlib.decompress(compressed)
    row_bytes = width * source_bpp
    previous = bytearray(row_bytes)
    rgb = bytearray(width * height * 3)
    cursor = 0
    for row in range(height):
        filter_type = raw[cursor]
        cursor += 1
        encoded = raw[cursor:cursor + row_bytes]
        cursor += row_bytes
        decoded = bytearray(row_bytes)
        for index, value in enumerate(encoded):
            left = decoded[index - source_bpp] if index >= source_bpp else 0
            above = previous[index]
            upper_left = previous[index - source_bpp] if index >= source_bpp else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                left_distance, above_distance, upper_left_distance = abs(estimate - left), abs(estimate - above), abs(estimate - upper_left)
                predictor = left if left_distance <= above_distance and left_distance <= upper_left_distance else (above if above_distance <= upper_left_distance else upper_left)
            else:
                raise ValueError(f"Unsupported PNG filter {filter_type}")
            decoded[index] = (value + predictor) & 255
        for column in range(width):
            src = column * source_bpp
            dst = (row * width + column) * 3
            rgb[dst:dst + 3] = decoded[src:src + 3]
        previous = decoded
    return width, height, bytes(rgb)


def compare_pngs(first: Path, second: Path) -> dict:
    w1, h1, rgb1 = _decode_png(first)
    w2, h2, rgb2 = _decode_png(second)
    if (w1, h1) != (w2, h2):
        raise ValueError(f"Screenshot dimensions differ: {first} {(w1, h1)} vs {second} {(w2, h2)}")
    sums = [0, 0, 0]
    changed_8 = changed_32 = max_diff_hist = 0
    histogram = [0] * 256
    changed = 0
    for index in range(0, len(rgb1), 3):
        difference = max(abs(rgb1[index + channel] - rgb2[index + channel]) for channel in range(3))
        histogram[difference] += 1
        if difference:
            changed += 1
        if difference > 8:
            changed_8 += 1
        if difference > 32:
            changed_32 += 1
        for channel in range(3):
            sums[channel] += abs(rgb1[index + channel] - rgb2[index + channel])
    pixel_count = w1 * h1
    rank = int(pixel_count * 0.95)
    cumulative = 0
    p95 = 0
    for difference, count in enumerate(histogram):
        cumulative += count
        if cumulative >= rank:
            p95 = difference
            break
    mean_abs = sum(sums) / (pixel_count * 3)
    return {
        "dimensions": [w1, h1],
        "mean_absolute_rgb_error": round(mean_abs, 4),
        "max_channel_difference_p95": p95,
        "changed_pixel_percent": round(100 * changed / pixel_count, 3),
        "pixels_over_8_difference_percent": round(100 * changed_8 / pixel_count, 3),
        "pixels_over_32_difference_percent": round(100 * changed_32 / pixel_count, 3),
        "sog_screenshot": str(first),
        "ply_screenshot": str(second),
    }


def render_report(report: dict) -> str:
    lines = [
        "# Visitor Browser Acceptance",
        "",
        f"Status: **{report['status']}**",
        "",
        f"Browser: {report['browser']}",
        f"SuperSplat Viewer: {report['viewer_version']} (PlayCanvas {report['playcanvas_version']})",
        f"Visitor primary format: {report['primary_format']}",
        f"Accepted PLY: {report['master_size_bytes']:,} bytes, SHA-256 `{report['master_sha256']}`",
        f"SOG derivative: {report['sog_size_bytes'] if report['sog_size_bytes'] is not None else 'not produced'} bytes",
        f"Review routes retained: {report['review_guided_route_count']}",
        "",
        "## Checks",
        "",
    ]
    for check in report["checks"]:
        lines.append(f"- {'PASS' if check['ok'] else 'FAIL'} — {check['name']}")
    lines.extend(["", "## Matched PLY/SOG captures", ""])
    for comparison in report["image_comparison"]:
        lines.append(f"- {comparison['label']} (`{comparison['viewpoint']}`): MAE {comparison['mean_absolute_rgb_error']}; p95 channel delta {comparison['max_channel_difference_p95']}; pixels with delta >8: {comparison['pixels_over_8_difference_percent']}%. ")
    lines.extend(["", "## Browser errors", "", f"- Console errors: {len(report['console_errors'])}",
                  f"- Runtime errors: {len(report['browser_errors'])}", f"- Network errors: {len(report['network_errors'])}",
                  f"- HTTP failures: {len(report['http_failures'])}", "", "Public release authorization remains `NEEDS_REVIEW`.", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
