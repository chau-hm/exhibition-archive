from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapters import read_json, sha256_file
from .workflow import json_digest, output_path


def release_candidate_identity(root: Path, config: dict) -> dict[str, Any]:
    website = output_path(root, config) / "website"
    paths = [website / "config" / "package-index.json", website / "config" / "exhibition.json",
             website / "config" / "navigation.json", website / "config" / "artworks.json",
             website / "api" / "health.json"]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        return {"status": "PACKAGE_MISSING", "missing_files": missing}
    hashes = {path.relative_to(website).as_posix(): sha256_file(path) for path in paths}
    package_index = read_json(website / "config" / "package-index.json")
    return {"status": "READY_FOR_REVIEW", "project_id": config["project_id"],
            "package_index_sha256": hashes["config/package-index.json"],
            "published_file_count": len(package_index.get("files", [])),
            "public_control_hashes": hashes,
            "publication_policy_sha256": json_digest(config.get("publication", {})),
            "candidate_id": json_digest({"project_id": config["project_id"], "files": hashes,
                                         "publication_policy": config.get("publication", {})})}


def publication_release_status(root: Path, config: dict, state) -> dict[str, Any]:
    identity = release_candidate_identity(root, config)
    row = state.db.execute("SELECT * FROM review_gates WHERE gate_id='publication-rights-review'").fetchone()
    decision = json.loads(row["decision_json"] or "{}") if row else {}
    current = identity.get("status") == "READY_FOR_REVIEW" and decision.get("candidate_id") == identity.get("candidate_id")
    status = "APPROVED" if row and row["status"] == "APPROVED" and current else (
        "STALE" if row and row["status"] == "APPROVED" and not current else "NEEDS_REVIEW")
    return {"gate_id": "publication-rights-review", "status": status,
            "public_release_ready": status == "APPROVED", "candidate": identity,
            "decision": decision if row else None,
            "required_decisions": ["model selection", "navigation approval", "artwork metadata accuracy",
                                   "image and artwork publication rights", "public/private data policy"],
            "notice": "A local package is not permission to publish or deploy. This gate records human review; it does not establish legal rights independently."}
