#!/usr/bin/env python3
"""Run Chrome acceptance against a generated first-visual Reviewer/Visitor."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from exhibition_archive.adapters import sha256_file
from run_visitor_browser_acceptance import StaticHandler, StaticServer, chrome_binary


def _sample_gaussian_ply(source: Path, destination: Path, maximum_vertices: int) -> int:
    """Write a deterministic, evenly sampled binary PLY for browser smoke checks."""
    if maximum_vertices <= 0:
        raise ValueError("--sample-gaussians must be a positive vertex count")
    type_sizes = {"char": 1, "int8": 1, "uchar": 1, "uint8": 1, "short": 2,
                  "int16": 2, "ushort": 2, "uint16": 2, "int": 4, "int32": 4,
                  "uint": 4, "uint32": 4, "float": 4, "float32": 4, "double": 8,
                  "float64": 8}
    header_lines: list[bytes] = []
    vertex_count = None
    record_size = 0
    format_name = None
    with source.open("rb") as reader:
        while True:
            line = reader.readline()
            if not line:
                raise ValueError("Gaussian PLY has no complete header")
            header_lines.append(line)
            text = line.decode("ascii").strip()
            parts = text.split()
            if parts[:1] == ["format"]:
                format_name = parts[1] if len(parts) > 1 else None
            elif parts[:2] == ["element", "vertex"]:
                if len(parts) != 3:
                    raise ValueError("Gaussian PLY has an invalid vertex declaration")
                vertex_count = int(parts[2])
            elif parts[:1] == ["element"] and parts[1:2] != ["vertex"]:
                raise ValueError("Gaussian PLY sampler only accepts vertex-only files")
            elif parts[:1] == ["property"]:
                if len(parts) != 3 or parts[1] == "list" or parts[1] not in type_sizes:
                    raise ValueError("Gaussian PLY has an unsupported property layout")
                record_size += type_sizes[parts[1]]
            elif text == "end_header":
                break
        header_size = sum(map(len, header_lines))
        if format_name != "binary_little_endian" or vertex_count is None or record_size <= 0:
            raise ValueError("Gaussian PLY sampler requires a binary little-endian vertex PLY")
        if source.stat().st_size != header_size + vertex_count * record_size:
            raise ValueError("Gaussian PLY size does not match its vertex declaration")
        sample_stride = max(1, math.ceil(vertex_count / maximum_vertices))
        sample_count = math.ceil(vertex_count / sample_stride)
        sampled_header = []
        for line in header_lines:
            text = line.decode("ascii")
            if text.startswith("element vertex "):
                newline = "\r\n" if text.endswith("\r\n") else "\n"
                line = f"element vertex {sample_count}{newline}".encode("ascii")
            sampled_header.append(line)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as writer:
            writer.writelines(sampled_header)
            reader.seek(header_size)
            for index in range(0, vertex_count, sample_stride):
                row = reader.read(record_size)
                if len(row) != record_size:
                    raise ValueError("Gaussian PLY ended before the declared vertex count")
                writer.write(row)
                if index + sample_stride < vertex_count:
                    reader.seek((sample_stride - 1) * record_size, 1)
    return sample_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visitor", required=True, type=Path, help="generated visitor directory")
    parser.add_argument("--reviewer", type=Path, help="generated reviewer directory (defaults beside Visitor)")
    parser.add_argument("--output", required=True, type=Path, help="directory for screenshots and report")
    parser.add_argument("--chrome", help="Chrome executable path")
    parser.add_argument("--timeout", type=int, default=600, help="maximum page-load time in seconds")
    parser.add_argument("--sample-gaussians", type=int,
                        help="serve a deterministic subset of this many model vertices for faster browser checks")
    args = parser.parse_args()

    visitor = args.visitor.resolve()
    reviewer = (args.reviewer or visitor.parent.parent / "reviewer").resolve()
    site_root = visitor.parent.resolve()
    if not (visitor / "index.html").is_file() or not (reviewer / "index.html").is_file():
        parser.error("--visitor and --reviewer must point to generated first-visual package directories")

    template_app = (ROOT / "templates" / "visitor" / "app.js").read_text(encoding="utf-8")
    marker = "__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__"
    generated_app = (visitor / "js" / "viewer.js").read_text(encoding="utf-8")
    if marker not in template_app or generated_app != template_app.replace(marker, "false"):
        parser.error("generated Visitor runtime differs from the current shared app beyond its acceptance marker")
    reviewer_html = (reviewer / "index.html").read_text(encoding="utf-8")
    visitor_link = 'src="../visitor/index.html"'
    if visitor_link not in reviewer_html:
        parser.error("Technical Reviewer does not embed the generated Visitor page")

    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    model_override = None
    model_sample_count = None
    if args.sample_gaussians is not None:
        model_override = output / "visitor-model-sample.ply"
        model_sample_count = _sample_gaussian_ply(
            visitor / "assets" / "exhibition.ply", model_override, args.sample_gaussians)

    class AcceptanceHandler(StaticHandler):
        acceptance_files: dict[str, bytes] = {}

        def _send(self, include_body: bool) -> None:
            path = unquote(urlsplit(self.path).path)
            payload = self.acceptance_files.get(path)
            if payload is None:
                return super()._send(include_body)
            mime, _ = mimetypes.guess_type(path)
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if include_body:
                self.wfile.write(payload)
            with self.request_lock:
                self.request_log.append({"path": path, "status": 200, "bytes": len(payload), "range": None})

    AcceptanceHandler.website = site_root
    AcceptanceHandler.comparison_ply = None
    AcceptanceHandler.request_log = []
    AcceptanceHandler.request_lock = threading.Lock()
    AcceptanceHandler.acceptance_files = {
        "/visitor/js/viewer.js": template_app.replace(marker, "true").encode("utf-8"),
        "/reviewer/index.html": reviewer_html.replace(
            visitor_link, 'src="../visitor/index.html?acceptance=1"').encode("utf-8"),
        "/favicon.ico": b"",
    }
    if model_override is not None:
        AcceptanceHandler.acceptance_files["/visitor/assets/exhibition.ply"] = model_override.read_bytes()

    server = StaticServer(("127.0.0.1", 0), AcceptanceHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site_url = f"http://127.0.0.1:{server.server_address[1]}"
    chrome = chrome_binary(args.chrome)
    chrome_log = output / "chrome.log"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    debug_port = sock.getsockname()[1]
    sock.close()
    driver = ROOT / "tests" / "first_visual_browser_driver.mjs"

    with tempfile.TemporaryDirectory(prefix="first-visual-chrome-") as temporary:
        profile = Path(temporary) / "profile"
        chrome_args = [
            chrome, "--headless=new", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-background-networking",
            "--disable-component-update", "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
            "--remote-allow-origins=*", "--enable-webgl", "--ignore-gpu-blocklist",
            "--enable-unsafe-swiftshader", f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={profile}", "about:blank",
        ]
        with chrome_log.open("w", encoding="utf-8") as log:
            browser = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=log)
            try:
                import time
                from urllib.request import urlopen

                debug_url = f"http://127.0.0.1:{debug_port}"
                deadline = time.time() + 30
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
                    raise RuntimeError("Chrome DevTools endpoint did not become ready")
                result = subprocess.run(
                    ["node", str(driver), "--cdp", debug_url, "--site", site_url,
                     "--visitor", str(visitor), "--reviewer", str(reviewer),
                     "--output", str(output), "--timeout-seconds", str(args.timeout)],
                    capture_output=True, text=True, timeout=args.timeout * 2 + 90, check=False,
                )
                if not result.stdout:
                    raise RuntimeError("Chrome browser driver did not return a report.\n" + result.stderr[-6000:])
                report = json.loads(result.stdout)
            finally:
                browser.terminate()
                try:
                    browser.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    browser.kill()
                    browser.wait(timeout=5)

    server.shutdown()
    server.server_close()
    report.update({"status": "PASS" if all(item["ok"] for item in report["checks"]) else "FAIL",
                   "visitor": str(visitor), "reviewer": str(reviewer), "website": site_url,
                   "chrome": subprocess.run([chrome, "--version"], capture_output=True,
                                             text=True, check=False).stdout.strip(),
                   "screenshots_directory": str(output),
                   "model_override": ({"kind": "deterministic_gaussian_vertex_sample",
                                       "source": str(visitor / "assets" / "exhibition.ply"),
                                       "sample": str(model_override),
                                       "source_sha256": sha256_file(visitor / "assets" / "exhibition.ply"),
                                       "sample_sha256": sha256_file(model_override),
                                       "vertex_count": model_sample_count}
                                      if model_override is not None else None),
                   "http_failures": [row for row in AcceptanceHandler.request_log if row["status"] >= 400],
                   "http_requests": AcceptanceHandler.request_log,
                   "http_request_count": len(AcceptanceHandler.request_log)})
    if report["http_failures"]:
        report["status"] = "FAIL"
    report_path = output / "first-visual-browser-acceptance.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path),
                      "checks": report["checks"], "screenshots": report["screenshots"]}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
