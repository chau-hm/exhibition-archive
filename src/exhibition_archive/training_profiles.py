"""Versioned, set-only-missing training parameter presets.

Workflow execution profiles (for example ``--profile first-visual``) live in
the CLI. These profiles only provide reproducible training parameter defaults.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


PROFILE_SCHEMA_VERSION = "exhibition-archive.training-profile.v1"
PROVENANCE_SCHEMA_VERSION = "exhibition-archive.training-profile-provenance.v1"
DEFAULT_TRAINING_PROFILE = "indoor-exhibition-high-quality@v1"

# A published revision is immutable. Behavior changes belong in a new revision.
_PROFILES: dict[tuple[str, str], dict[str, Any]] = {
    ("indoor-exhibition-high-quality", "v1"): {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "id": "indoor-exhibition-high-quality",
        "revision": "v1",
        "description": "Full-resolution, high-quality 3DGS candidate defaults for indoor exhibitions.",
        "training_defaults": {
            "quality": "high",
            "resolution_divisor": 1,
            "iterations": 30000,
            "steps_per_save": 5000,
            "save_full_checkpoint": True,
            "save_only_latest_checkpoint": True,
            "view_sampling": "uniform",
            "cache_images": "disk",
            "load_masks": False,
            "device": "auto",
        },
    },
}

_PROFILE_FIELD_TYPES = {
    "quality": str,
    "resolution_divisor": int,
    "iterations": int,
    "steps_per_save": int,
    "save_full_checkpoint": bool,
    "save_only_latest_checkpoint": bool,
    "view_sampling": str,
    "cache_images": str,
    "load_masks": bool,
    "device": str,
    "seed": int,
}


def _same_config_value(left: Any, right: Any) -> bool:
    """Keep JSON booleans distinct from integers such as 0 and 1."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (left.keys() == right.keys() and
                all(_same_config_value(left[key], right[key]) for key in left))
    if isinstance(left, list):
        return (len(left) == len(right) and
                all(_same_config_value(a, b) for a, b in zip(left, right)))
    return left == right


def available_training_profiles() -> list[dict[str, str]]:
    """Return stable profile identities without exposing mutable registry data."""
    return [
        {"id": profile["id"], "revision": profile["revision"],
         "reference": f"{profile['id']}@{profile['revision']}",
         "description": profile["description"]}
        for _, profile in sorted(_PROFILES.items())
    ]


def load_training_profile(reference: str) -> dict[str, Any]:
    """Load an explicitly versioned profile reference (``id@revision``)."""
    if not isinstance(reference, str) or reference.count("@") != 1:
        raise ValueError("Training profiles must use an explicit id@revision reference, for example indoor-exhibition-high-quality@v1")
    profile_id, revision = reference.split("@", 1)
    if not profile_id or not revision or any(char.isspace() for char in reference):
        raise ValueError(f"Invalid training profile reference: {reference!r}")
    profile = _PROFILES.get((profile_id, revision))
    if profile is None:
        known_id = any(key[0] == profile_id for key in _PROFILES)
        if known_id:
            revisions = sorted(key[1] for key in _PROFILES if key[0] == profile_id)
            raise ValueError(f"Unsupported revision {revision!r} for training profile {profile_id!r}; supported revisions: {', '.join(revisions)}")
        raise ValueError(f"Unknown training profile {profile_id!r}; available profiles: {', '.join(row['reference'] for row in available_training_profiles())}")
    return deepcopy(profile)


def _validate_defaults(profile: dict[str, Any]) -> dict[str, Any]:
    defaults = profile.get("training_defaults")
    if not isinstance(defaults, dict):
        raise ValueError("Training profile training_defaults must be an object")
    for key, value in defaults.items():
        expected_type = _PROFILE_FIELD_TYPES.get(key)
        if expected_type is None:
            raise ValueError(f"Training profile contains unsupported field {key!r}")
        if not isinstance(value, expected_type) or (expected_type is int and isinstance(value, bool)):
            raise ValueError(f"Training profile field {key!r} has an invalid value type")
    return defaults


def apply_training_profile_defaults(config: dict[str, Any], profile: dict[str, Any], *,
                                    selection_source: str = "operator") -> dict[str, Any]:
    """Apply a pinned profile, preserving explicit and non-missing values.

    Previously profile-provided values remain profile-owned only while their
    current value still matches the recorded value and they have not been
    explicitly edited through ``config set``. This lets a deliberate profile
    revision update its own defaults while protecting operator overrides.
    """
    if (not isinstance(profile, dict) or
            profile.get("schema_version") != PROFILE_SCHEMA_VERSION or
            not isinstance(profile.get("id"), str) or not isinstance(profile.get("revision"), str)):
        raise ValueError("Training profile is malformed or uses an unsupported schema version")
    if selection_source not in {"default", "operator"}:
        raise ValueError("Training profile selection_source must be default or operator")
    registered = _PROFILES.get((profile["id"], profile["revision"]))
    if registered != profile:
        raise ValueError("Training profile content does not match its immutable registered revision")
    defaults = _validate_defaults(profile)

    integrations = config.setdefault("integrations", {})
    if not isinstance(integrations, dict):
        raise ValueError("Project integrations must be an object before applying a training profile")
    model = integrations.setdefault("model", {})
    if not isinstance(model, dict):
        raise ValueError("Project integrations.model must be an object before applying a training profile")

    has_previous = "training_profile" in config
    previous = config.get("training_profile", {})
    previous_fields: dict[str, Any] = {}
    explicit_fields: set[str] = set()
    if has_previous:
        if (not isinstance(previous, dict) or not previous or
                previous.get("schema_version") != PROVENANCE_SCHEMA_VERSION or
                not isinstance(previous.get("profile_provided_fields", {}), dict)):
            raise ValueError("Existing training_profile provenance is malformed; inspect project.json before selecting a profile")
        previous_id, previous_revision = previous.get("id"), previous.get("revision")
        if not isinstance(previous_id, str) or not isinstance(previous_revision, str):
            raise ValueError("Existing training_profile identity is malformed; inspect project.json before selecting a profile")
        try:
            previous_definition = load_training_profile(f"{previous_id}@{previous_revision}")
        except ValueError as exc:
            raise ValueError(f"Existing training_profile references an unsupported revision: {exc}") from exc
        if previous.get("selection_source") not in {"default", "operator"}:
            raise ValueError("Existing training_profile selection_source is malformed")
        if not _same_config_value(previous.get("profile_defaults"),
                                  previous_definition["training_defaults"]):
            raise ValueError(
                "Registered training profile defaults changed without a revision bump; "
                "inspect project.json and select a new profile revision explicitly")
        previous_fields = previous["profile_provided_fields"]
        if any(not isinstance(field, str) or not field.startswith("integrations.model.")
               for field in previous_fields):
            raise ValueError("Existing training_profile provided field provenance is malformed")
        old_defaults = previous_definition["training_defaults"]
        for field, value in previous_fields.items():
            old_key = field.removeprefix("integrations.model.")
            if old_key not in old_defaults or not _same_config_value(value, old_defaults[old_key]):
                raise ValueError(f"Existing training_profile field provenance does not match its registered revision: {field}")
        raw_explicit_fields = previous.get("explicitly_configured_fields", [])
        if (not isinstance(raw_explicit_fields, list) or
                any(not isinstance(field, str) for field in raw_explicit_fields)):
            raise ValueError("Existing training_profile explicit field provenance is malformed")
        explicit_fields = set(raw_explicit_fields)

    supplied: dict[str, Any] = {}
    explicit: set[str] = set(explicit_fields)
    for key, value in defaults.items():
        path = f"integrations.model.{key}"
        old_profile_value = previous_fields.get(path, _MISSING)
        is_previous_profile_value = (
            old_profile_value is not _MISSING and path not in explicit and
            key in model and _same_config_value(model[key], old_profile_value)
        )
        if key in model and not is_previous_profile_value:
            explicit.add(path)
            continue
        model[key] = deepcopy(value)
        supplied[path] = deepcopy(value)

    metadata = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "id": profile["id"],
        "revision": profile["revision"],
        "selection_source": selection_source,
        "profile_defaults": deepcopy(defaults),
        "profile_provided_fields": supplied,
        "explicitly_configured_fields": sorted(explicit),
        "effective_training_configuration": {
            key: deepcopy(value) for key, value in model.items()
            if key != "environment_overrides"
        },
    }
    config["training_profile"] = metadata
    return deepcopy(metadata)


_MISSING = object()


def apply_training_profile(config: dict[str, Any], reference: str, *,
                           selection_source: str = "operator") -> dict[str, Any]:
    """Resolve and apply one registered immutable training profile revision."""
    return apply_training_profile_defaults(config, load_training_profile(reference),
                                           selection_source=selection_source)


def record_explicit_training_configuration(config: dict[str, Any], key_path: str) -> None:
    """Mark a profile-controlled model setting as an operator override."""
    metadata = config.get("training_profile")
    if not isinstance(metadata, dict) or metadata.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        return
    model_prefix = "integrations.model."
    if not key_path.startswith(model_prefix):
        return
    field = key_path
    explicit = set(metadata.get("explicitly_configured_fields", []))
    explicit.add(field)
    provided = metadata.get("profile_provided_fields", {})
    provided.pop(field, None)
    metadata["explicitly_configured_fields"] = sorted(explicit)
    model = config.get("integrations", {}).get("model", {})
    metadata["effective_training_configuration"] = {
        key: deepcopy(value) for key, value in model.items()
        if key != "environment_overrides"
    }


def refresh_effective_training_configuration(config: dict[str, Any]) -> None:
    """Refresh the provenance snapshot after engine defaults are resolved."""
    metadata = config.get("training_profile")
    if not isinstance(metadata, dict) or metadata.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        return
    model = config.get("integrations", {}).get("model", {})
    if isinstance(model, dict):
        metadata["effective_training_configuration"] = {
            key: deepcopy(value) for key, value in model.items()
            if key != "environment_overrides"
        }
