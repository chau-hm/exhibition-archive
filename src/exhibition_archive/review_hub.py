from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .adapters import read_json
from .navigation import render_navigation_review_html
from .navigation_review import current_navigation_review, record_navigation_decision, save_navigation_draft
from .state import ProjectState
from .state import utc_now


def _install_pillow_passthrough_if_missing():
    """The existing Phase2EV review page can serve native JPEGs without Pillow."""
    try:
        import PIL  # noqa: F401
        return
    except ImportError:
        pass

    class RawImage:
        def __init__(self, path): self.path = Path(path); self.data = self.path.read_bytes()
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def convert(self, _mode): return self
        def thumbnail(self, *_args, **_kwargs): return None
        def save(self, output, format=None, **_kwargs): output.write(self.data)

    image_mod = types.ModuleType("PIL.Image")
    image_mod.open = lambda path: RawImage(path)
    image_mod.Resampling = types.SimpleNamespace(LANCZOS=1)
    ops_mod = types.ModuleType("PIL.ImageOps")
    ops_mod.exif_transpose = lambda image: image
    package = types.ModuleType("PIL")
    package.Image, package.ImageOps = image_mod, ops_mod
    sys.modules["PIL"] = package
    sys.modules["PIL.Image"] = image_mod
    sys.modules["PIL.ImageOps"] = ops_mod


def _load_phase2ev(config: dict):
    review_config = config["integrations"]["artwork"]["review_server"]
    server_file = Path(review_config["review_server_script"]).expanduser().resolve()
    _install_pillow_passthrough_if_missing()
    spec = importlib.util.spec_from_file_location("exhibition_archive_phase2ev_review_server", server_file)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--port", type=int, default=4188)
    args = parser.parse_args(argv)
    root = args.project.resolve()
    config = read_json(root / "project.json")
    artwork = config.get("integrations", {}).get("artwork", {}).get("review_server")
    review_server = None
    if artwork:
        server_module = _load_phase2ev(config)
        review_project = Path(artwork["project_manifest"]).expanduser().resolve()
        review_output = Path(artwork["output"]).expanduser()
        if not review_output.is_absolute(): review_output = (root / review_output).resolve()
        static = Path(artwork["static_root"]).expanduser().resolve()
        base_store = server_module.ReviewStore
        class EngineReviewStore(base_store):
            def apply(self, body):
                result = super().apply(body)
                action = str(body.get("action", "unknown"))
                target = str(body.get("association_id") or body.get("artwork_id") or body.get("observation_id") or action)
                decision = str(body.get("decision") or body.get("identity_status") or body.get("importance") or body.get("assigned_artwork_id") or "SAVED")
                state = ProjectState(root)
                try:
                    gate_id = f"phase2ev:{action}:{target}"
                    state.db.execute("INSERT INTO review_gates(gate_id,status,required,updated_at,decision_json) VALUES(?,?,?,?,?) ON CONFLICT(gate_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,decision_json=excluded.decision_json", (gate_id, decision, 0, utc_now(), json.dumps({"reviewer_id": body.get("reviewer_id"), "action": action, "target": target}, sort_keys=True)))
                    state.db.execute("INSERT INTO review_history(created_at,gate_id,reviewer,decision,payload_json) VALUES(?,?,?,?,?)", (utc_now(), gate_id, str(body.get("reviewer_id", "")), decision, json.dumps(body, sort_keys=True)))
                    state.db.commit()
                finally:
                    state.close()
                return result
        store = EngineReviewStore(review_project, review_output, static)
        server_module.Handler.store = store
        review_server = ThreadingHTTPServer(("127.0.0.1", args.port + 1), server_module.Handler)

    dashboard = (Path(__file__).resolve().parents[2] / "dashboard" / "index.html").read_text(encoding="utf-8")
    review_url = f"http://127.0.0.1:{args.port+1}/"
    artwork_link = f'<a href="{review_url}" target="_blank" rel="noreferrer">Open artwork review separately</a>' if review_server else ""
    artwork_frame = (
        f'<iframe title="Phase 2EV artwork identity and association review" src="{review_url}"></iframe>'
        if review_server else
        '<div class="review-unavailable">Artwork review is not configured for this project.</div>'
    )
    dashboard = dashboard.replace("__PROJECT_ID__", config["project_id"]).replace("__ARTWORK_REVIEW_LINK__", artwork_link)
    dashboard = dashboard.replace("__ARTWORK_REVIEW_FRAME__", artwork_frame)
    nav_cfg = config.get("integrations", {}).get("navigation", {})
    nav_link = '<a href="/navigation" target="_blank" rel="noreferrer">Review generated navigation</a>' if nav_cfg.get("mode") == "generate_from_sfm" else ""
    dashboard = dashboard.replace("__NAVIGATION_REVIEW_LINK__", nav_link)

    def navigation_page_data():
        state = ProjectState(root)
        try:
            return current_navigation_review(root, config, state)
        finally:
            state.close()

    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args): return
        def respond(self, status, body, content_type="text/html; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health": return self.respond(200, json.dumps({"ok": True, "project_id": config["project_id"]}), "application/json")
            if path == "/api/workflow":
                state = ProjectState(root)
                try:
                    stages = state.stages()
                    backend_runs = {}
                    for stage_id in ("sfm", "training"):
                        stage = stages.get(stage_id)
                        if not stage:
                            backend_runs[stage_id] = {"status": "NOT_STARTED"}
                            continue
                        try:
                            details = json.loads(stage.get("details_json") or "{}")
                        except (TypeError, json.JSONDecodeError):
                            details = {}
                        execution = None
                        if stage.get("last_execution_id") is not None:
                            row = state.db.execute("SELECT * FROM stage_executions WHERE execution_id=?", (stage["last_execution_id"],)).fetchone()
                            execution = dict(row) if row else None
                        command = []
                        execution_details = {}
                        if execution:
                            try:
                                command = json.loads(execution.get("command_json") or "[]")
                            except (TypeError, json.JSONDecodeError):
                                command = []
                            try:
                                execution_details = json.loads(execution.get("details_json") or "{}")
                            except (TypeError, json.JSONDecodeError):
                                execution_details = {}
                        log_path = (execution or {}).get("log_path") or details.get("log_path")
                        log_tail = []
                        if log_path:
                            candidate = Path(log_path).expanduser().resolve()
                            if candidate.is_relative_to(root) and candidate.is_file():
                                try:
                                    from collections import deque
                                    with candidate.open("r", encoding="utf-8", errors="replace") as stream:
                                        log_tail = [line.rstrip("\r\n") for line in deque(stream, maxlen=12)]
                                except OSError:
                                    log_tail = []
                        config_key = "sfm" if stage_id == "sfm" else "model"
                        stage_config = config.get("integrations", {}).get(config_key, {})
                        configured_input = stage_config.get("image_directory") or config.get("source", {}).get("photo_directory")
                        if configured_input:
                            configured_input_path = Path(configured_input).expanduser()
                            if not configured_input_path.is_absolute():
                                configured_input_path = root / configured_input_path
                            configured_input = str(configured_input_path.resolve())
                        backend_runs[stage_id] = {
                            "status": stage.get("status"),
                            "execution_status": (execution or {}).get("status"),
                            "execution_id": stage.get("last_execution_id"),
                            "backend_version": details.get("backend_version") or execution_details.get("backend_version"),
                            "attempt_directory": details.get("attempt_directory") or (execution or {}).get("workdir"),
                            "workdir": (execution or {}).get("workdir") or details.get("workdir"),
                            "pid": (execution or {}).get("pid") or details.get("pid"),
                            "command": command or details.get("command", []),
                            "log_path": log_path,
                            "recent_log_lines": log_tail or details.get("recent_log_lines", []),
                            "input_path": details.get("source_directory") or details.get("image_directory") or details.get("input_path") or configured_input,
                            "artifact_path": details.get("model_path") or details.get("reconstruction_directory"),
                            "validation_path": details.get("validation_path") or details.get("sfm_validation_path"),
                            "validation": details.get("model_status") or details.get("quality_gate") or details.get("execution_status"),
                            "resume_status": details.get("checkpoint_resume_execution_status") or "CHECKPOINT_RESUME_UNSUPPORTED_BY_ENGINE; optimizer-state semantics not verified",
                            "details": details,
                        }
                    result = {"project_id": config["project_id"], "stages": stages, "backend_runs": backend_runs, "gates": [dict(x) for x in state.db.execute("SELECT * FROM review_gates ORDER BY gate_id")]}
                finally: state.close()
                return self.respond(200, json.dumps(result), "application/json")
            if path in {"/", "/index.html"}: return self.respond(200, dashboard)
            if path == "/navigation":
                try:
                    current = navigation_page_data()
                    page = render_navigation_review_html(current["artifact"])
                    page = page.replace("CURRENT_ARTIFACT_SHA256", current["sha256"])
                    page = page.replace("CURRENT_ARTIFACT_STATUS", current["approval_status"])
                    return self.respond(200, page)
                except Exception as exc:
                    return self.respond(409, f"Navigation review unavailable: {exc}", "text/plain; charset=utf-8")
            if path == "/api/navigation/state":
                try:
                    current = navigation_page_data()
                    return self.respond(200, json.dumps({key: current[key] for key in ("sha256", "navigation_input_fingerprint", "stage_status", "approval_status", "path")}), "application/json")
                except Exception as exc:
                    return self.respond(409, json.dumps({"error": str(exc)}), "application/json")
            return self.respond(404, "Not found", "text/plain; charset=utf-8")
        def do_POST(self):
            path = urlparse(self.path).path
            origin = self.headers.get("Origin")
            allowed_origins = {f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}"}
            if origin and origin not in allowed_origins:
                return self.respond(403, json.dumps({"error": "Navigation review accepts same-origin local requests only"}), "application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4 * 1024 * 1024:
                    return self.respond(413, json.dumps({"error": "Invalid navigation review request size"}), "application/json")
                body = json.loads(self.rfile.read(length))
                state = ProjectState(root)
                try:
                    if path == "/api/navigation/draft":
                        result = save_navigation_draft(root, config, state, body.get("artifact", {}),
                                                       body.get("reviewer", ""), body.get("expected_sha256", ""))
                    elif path in {"/api/navigation/approve", "/api/navigation/reject"}:
                        result = record_navigation_decision(
                            root, config, state, "APPROVE" if path.endswith("approve") else "REJECT",
                            body.get("reviewer", ""), body.get("rationale", ""),
                            expected_sha256=body.get("expected_sha256"))
                    else:
                        return self.respond(404, json.dumps({"error": "Not found"}), "application/json")
                finally:
                    state.close()
                return self.respond(200, json.dumps(result), "application/json")
            except RuntimeError as exc:
                return self.respond(409, json.dumps({"error": str(exc)}), "application/json")
            except Exception as exc:
                return self.respond(400, json.dumps({"error": str(exc)}), "application/json")

    dashboard_server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    servers = [dashboard_server] + ([review_server] if review_server else [])
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads: thread.start()
    print(f"Exhibition Archive review: http://127.0.0.1:{args.port}/", flush=True)
    if review_server: print(f"Phase 2EV artwork review: http://127.0.0.1:{args.port+1}/", flush=True)
    try:
        threads[0].join()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    main()
