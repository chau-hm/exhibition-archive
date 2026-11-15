from __future__ import annotations

import math
from typing import Any


DEFAULT_BOUNDARY_FEEDBACK = {
    "approach_distance_viewer_units": 0.85,
    "outside_enter_clearance_viewer_units": 0.0,
    "outside_exit_clearance_viewer_units": 0.18,
    "fade_duration_ms": 420,
    "maximum_vignette_opacity": 0.38,
}


def visitor_xr_configuration(config: dict[str, Any]) -> dict[str, Any]:
    """Return a validated, normalized optional Visitor XR publication profile."""
    publication = config.get("publication", {})
    if not isinstance(publication, dict):
        raise ValueError("publication must be an object")
    raw = publication.get("visitor_xr", {})
    if not isinstance(raw, dict):
        raise ValueError("publication.visitor_xr must be an object")
    unknown = set(raw) - {"enabled", "renderer", "recenter_spawn", "boundary_feedback", "diagnostics"}
    if unknown:
        raise ValueError("publication.visitor_xr has unsupported fields: " + ", ".join(sorted(unknown)))

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("publication.visitor_xr.enabled must be a boolean")
    renderer = raw.get("renderer", "auto")
    if renderer not in ("auto", "webgl", "webgpu"):
        raise ValueError("publication.visitor_xr.renderer must be auto, webgl, or webgpu")
    recenter_spawn = raw.get("recenter_spawn", "opening")
    if recenter_spawn != "opening":
        raise ValueError("publication.visitor_xr.recenter_spawn currently supports only 'opening'")

    feedback = raw.get("boundary_feedback", {})
    if not isinstance(feedback, dict):
        raise ValueError("publication.visitor_xr.boundary_feedback must be an object")
    unknown_feedback = set(feedback) - set(DEFAULT_BOUNDARY_FEEDBACK)
    if unknown_feedback:
        raise ValueError("XR boundary feedback has unsupported fields: " + ", ".join(sorted(unknown_feedback)))
    normalized_feedback = {**DEFAULT_BOUNDARY_FEEDBACK, **feedback}
    for key in DEFAULT_BOUNDARY_FEEDBACK:
        value = normalized_feedback[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"publication.visitor_xr.boundary_feedback.{key} must be a finite number")
        normalized_feedback[key] = float(value)

    enter = normalized_feedback["outside_enter_clearance_viewer_units"]
    release = normalized_feedback["outside_exit_clearance_viewer_units"]
    approach = normalized_feedback["approach_distance_viewer_units"]
    duration = normalized_feedback["fade_duration_ms"]
    opacity = normalized_feedback["maximum_vignette_opacity"]
    if enter < 0 or release < enter or approach <= release:
        raise ValueError("XR boundary thresholds must satisfy 0 <= outside-enter <= outside-exit < approach-distance")
    if not 120 <= duration <= 2000:
        raise ValueError("XR boundary fade_duration_ms must be between 120 and 2000")
    if not 0 <= opacity <= 0.8:
        raise ValueError("XR boundary maximum_vignette_opacity must be between 0 and 0.8")

    diagnostics = raw.get("diagnostics", {})
    if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("enabled", False), bool):
        raise ValueError("publication.visitor_xr.diagnostics.enabled must be a boolean")
    if set(diagnostics) - {"enabled"}:
        raise ValueError("publication.visitor_xr.diagnostics supports only the enabled field")

    return {
        "enabled": enabled,
        "renderer": renderer,
        "recenter_spawn": recenter_spawn,
        "boundary_feedback": normalized_feedback,
        "diagnostics": {"enabled": diagnostics.get("enabled", False)},
    }
