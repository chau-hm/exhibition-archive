"""Bounded parsing and durable progress events for Spirula subprocess logs."""

from __future__ import annotations

import json
import math
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_READ_BYTES_PER_POLL = 512 * 1024
MAX_PARTIAL_LINE_BYTES = 64 * 1024


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class IncrementalLogReader:
    """Read appended log bytes with a bounded poll size and partial-line carry."""

    def __init__(self) -> None:
        self.offset = 0
        self.partial = b""
        self.recent_lines: deque[str] = deque(maxlen=8)

    def read(self, path: str | Path) -> list[str]:
        try:
            with Path(path).open("rb") as stream:
                stream.seek(self.offset)
                data = stream.read(MAX_READ_BYTES_PER_POLL)
                self.offset = stream.tell()
        except OSError:
            return []
        if not data:
            return []
        combined = self.partial + data
        chunks = combined.split(b"\n")
        self.partial = chunks.pop()
        if len(self.partial) > MAX_PARTIAL_LINE_BYTES:
            self.partial = self.partial[-MAX_PARTIAL_LINE_BYTES:]
        lines = []
        for raw in chunks:
            line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
            lines.append(line)
            self.recent_lines.append(line[-MAX_PARTIAL_LINE_BYTES:])
        return lines


class SpirulaProgressParser:
    """Recognize observed Spirula SfM lines and conservative training patterns."""

    _extract = re.compile(r"\[(?:extract|feature)\]\s*(\d+)\s*/\s*(\d+)(?:\s+(.+?))?(?:\s+Features:\s*(\d+))?\s*$", re.I)
    _registered = re.compile(r"Registered image\b.*?images in the model:\s*(\d+)", re.I)
    _matching = re.compile(r"Pairs kept:\s*(\d+)\s*/\s*(\d+).*?Inliers:\s*(\d+)\s*/\s*(\d+)", re.I)
    _training = re.compile(r"\b(?:iteration|iter|step)\s*[#:= ]*\s*(\d+)\s*(?:/|of)\s*(\d+)\b", re.I)
    _training_current = re.compile(r"\b(?:iteration|iter|step)\s*[#:= ]*\s*(\d+)\b", re.I)
    _loss = re.compile(r"\b(loss|l1|psnr)\s*[:=]\s*([-+0-9.eE]+)", re.I)

    def __init__(self, stage_id: str, context: dict[str, Any] | None = None) -> None:
        self.stage_id = stage_id
        self.context = context or {}
        self.started = time.monotonic()
        self.last_iteration: tuple[int, float] | None = None
        self.last_counts: dict[str, tuple[int, float]] = {}

    def parse(self, line: str) -> dict[str, Any] | None:
        elapsed = round(time.monotonic() - self.started, 2)
        match = self._extract.search(line)
        if match:
            current, total = int(match.group(1)), int(match.group(2))
            return {"event": "sfm_image_extraction", "stage": self.stage_id,
                    "current": current, "total": total, "unit": "images",
                    "image": (match.group(3) or "").strip(), "elapsed_seconds": elapsed,
                    "eta_seconds": self._counter_eta("extract", current, total)}
        match = self._registered.search(line)
        if match:
            total = self.context.get("target_images")
            current = int(match.group(1))
            return {"event": "sfm_mapping", "stage": self.stage_id, "phase": "registered_camera",
                    "current": current, "total": total, "unit": "images",
                    "elapsed_seconds": elapsed,
                    "eta_seconds": self._counter_eta("mapping", current, total) if total else None}
        match = self._matching.search(line)
        if match:
            return {"event": "sfm_matching_summary", "stage": self.stage_id,
                    "pairs_kept": int(match.group(1)), "pairs_total": int(match.group(2)),
                    "inliers": int(match.group(3)), "candidate_matches": int(match.group(4)),
                    "elapsed_seconds": elapsed}
        if "bundle adjustment" in line.casefold():
            return {"event": "sfm_bundle_adjustment", "stage": self.stage_id,
                    "activity": line.strip()[-300:], "elapsed_seconds": elapsed}
        if re.search(r"Reprojection error:\s*mean", line, re.I):
            return {"event": "sfm_validation_summary", "stage": self.stage_id,
                    "summary": line.strip()[-300:], "elapsed_seconds": elapsed}
        match = self._training.search(line)
        if match:
            current, total = map(int, match.groups())
            rate, eta = self._training_rate(current, total, elapsed)
            event = {"event": "training_iteration", "stage": self.stage_id,
                     "current": current, "total": total, "unit": "iterations",
                     "elapsed_seconds": elapsed, "iterations_per_second": rate,
                     "eta_seconds": eta}
            loss = self._loss.search(line)
            if loss:
                value = float(loss.group(2))
                if math.isfinite(value):
                    event["metric"] = {"name": loss.group(1).casefold(), "value": value}
            return event
        match = self._training_current.search(line)
        if match and self.stage_id == "training":
            current = int(match.group(1))
            total = self.context.get("target_iterations")
            rate, eta = self._training_rate(current, total, elapsed) if total else (None, None)
            event = {"event": "training_iteration", "stage": self.stage_id,
                     "current": current, "total": total, "unit": "iterations",
                     "elapsed_seconds": elapsed, "iterations_per_second": rate,
                     "eta_seconds": eta}
            loss = self._loss.search(line)
            if loss:
                value = float(loss.group(2))
                if math.isfinite(value):
                    event["metric"] = {"name": loss.group(1).casefold(), "value": value}
            return event
        return None

    def _counter_eta(self, key: str, current: int, total: int | None) -> float | None:
        now = time.monotonic()
        previous = self.last_counts.get(key)
        self.last_counts[key] = (current, now)
        if not total or previous is None:
            return None
        delta_count = current - previous[0]
        delta_time = now - previous[1]
        if delta_count <= 0 or delta_time <= 0:
            return None
        rate = delta_count / delta_time
        return round(max(0.0, (total - current) / rate), 1)

    def _training_rate(self, current: int, total: int, elapsed: float) -> tuple[float | None, float | None]:
        previous = self.last_iteration
        self.last_iteration = (current, time.monotonic())
        if previous is None:
            return None, None
        delta_steps = current - previous[0]
        delta_time = self.last_iteration[1] - previous[1]
        if delta_steps <= 0 or delta_time <= 0:
            return None, None
        rate = delta_steps / delta_time
        return round(rate, 3), round(max(0, total - current) / rate, 1)


def make_event(stage_id: str, event: str, **data: Any) -> dict[str, Any]:
    return {"schema_version": "exhibition-archive.progress-event.v1",
            "timestamp_utc": _timestamp(), "stage": stage_id, "event": event, **data}


def append_event(path: str | Path, event: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
        stream.flush()


def format_event(event: dict[str, Any]) -> str:
    name = event.get("event")
    if name in {"sfm_image_extraction", "sfm_mapping", "training_iteration"}:
        current, total = event.get("current"), event.get("total")
        progress = f"{current}/{total}" if total else f"{current} (total unknown)"
        line = f"[{event.get('stage')}] {name.replace('_', ' ')}: {progress} {event.get('unit', '')}".rstrip()
        if name == "training_iteration":
            rate = event.get("iterations_per_second")
            eta = event.get("eta_seconds")
            line += f" · {rate:.2f} it/s" if isinstance(rate, (int, float)) else " · rate: calculating"
            line += f" · ETA {int(eta)}s" if isinstance(eta, (int, float)) else " · ETA: calculating"
            if event.get("metric"):
                metric = event["metric"]
                line += f" · {metric['name']} {metric['value']:g}"
        elif name in {"sfm_image_extraction", "sfm_mapping"}:
            eta = event.get("eta_seconds")
            line += f" · ETA {int(eta)}s" if isinstance(eta, (int, float)) else " · ETA: calculating"
        elapsed = event.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)):
            line += f" · elapsed {_duration(elapsed)}"
        return line
    if name == "sfm_matching_summary":
        return f"[sfm] matching summary: {event['pairs_kept']}/{event['pairs_total']} pairs kept, {event['inliers']}/{event['candidate_matches']} inliers"
    if name == "sfm_bundle_adjustment":
        return f"[sfm] {event.get('activity', 'bundle adjustment active')}"
    if name == "sfm_validation_summary":
        return f"[sfm] {event.get('summary', 'validation summary available')}"
    if name == "stage_process_started":
        log = event.get("output_log")
        return f"[{event.get('stage')}] backend started" + (f" · log {log}" if log else "")
    if name == "stage_process_heartbeat":
        line = f"[{event.get('stage')}] running · elapsed {_duration(event.get('elapsed_seconds', 0))}"
        progress = event.get("progress")
        if isinstance(progress, dict):
            rendered = format_event({**progress, "stage": event.get("stage")})
            if rendered:
                line += f" · {rendered.split('] ', 1)[-1]}"
        else:
            line += " · backend progress: unavailable"
        checkpoint_count = event.get("checkpoint_count")
        if isinstance(checkpoint_count, int):
            line += f" · checkpoints {checkpoint_count}"
        return line
    if name == "checkpoint_state":
        candidates = event.get("candidates") or []
        latest = candidates[-1].get("path") if candidates and isinstance(candidates[-1], dict) else None
        line = f"[{event.get('stage')}] checkpoints: {len(candidates)} detected"
        return line + (f" · latest {latest}" if latest else "")
    if name in {"pipeline_stage_started", "pipeline_stage_completed", "pipeline_stage_cached",
                "pipeline_stage_needs_review"}:
        number, total = event.get("stage_number"), event.get("stage_total")
        prefix = f"[{number}/{total}] " if number and total else ""
        action = {"pipeline_stage_started": "started", "pipeline_stage_completed": "completed",
                  "pipeline_stage_cached": "reused validated cache",
                  "pipeline_stage_needs_review": "needs human review"}[name]
        line = f"{prefix}{event.get('stage', 'pipeline')}: {action} · {event.get('status', 'UNKNOWN')}"
        if event.get("warning"):
            line += f" · warning: {event['warning']}"
        return line
    return json.dumps(event, sort_keys=True, ensure_ascii=False)


def _duration(seconds: float | int) -> str:
    seconds = max(0, int(seconds))
    minutes, remaining = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{remaining:02d}" if hours else f"{minutes:02d}:{remaining:02d}"
