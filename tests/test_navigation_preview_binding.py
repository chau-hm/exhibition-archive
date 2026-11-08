from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exhibition_archive.navigation_review import preview_navigation_context
from exhibition_archive.state import ProjectState
from exhibition_archive.workflow import run_first_visual_preview_only


class PreviewNavigationBindingTests(unittest.TestCase):
    fingerprint = "4b" * 32

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "engine"
        self.output = self.root / "output"
        self.config = {"project_id": "preview-binding-fixture", "output_directory": "output",
                       "integrations": {"navigation": {"mode": "generate_from_sfm"}}}
        self.state = ProjectState(self.root)
        self.proposal_path = self.output / "navigation" / "proposals" / "navigation-proposal-v0001.json"
        self.draft_path = self.output / "navigation" / "drafts" / "navigation-draft-v0001.json"
        self.approved_path = self.output / "work" / "navigation-approved.json"
        self.reviewed_path = self.output / "navigation" / "reviewed" / "navigation-reviewed-v0001.json"
        for path in (self.proposal_path, self.draft_path, self.approved_path, self.reviewed_path):
            path.parent.mkdir(parents=True, exist_ok=True)

        provenance = {"navigation_input_fingerprint": self.fingerprint,
                      "generation_execution_id": 12}
        proposal = {"id": "navigation-fixture-v1", "status": "NAVIGATION_NEEDS_REVIEW",
                    "provenance": provenance}
        self.proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        candidate = {"id": "navigation-fixture-v1", "status": "NAVIGATION_NEEDS_REVIEW",
                     "provenance": provenance, "review": {"status": "DRAFT", "approved": False}}
        self.draft_path.write_text(json.dumps(candidate), encoding="utf-8")
        self.candidate_sha = self._sha(self.draft_path)
        decision = {"decision": "APPROVE", "reviewer": "Fixture reviewer",
                    "rationale": "Reviewed fixture boundary.",
                    "reviewed_at_utc": "2026-11-08T14:32:18Z",
                    "proposal_path": str(self.proposal_path.resolve()),
                    "candidate_path": str(self.draft_path.resolve()),
                    "candidate_sha256": self.candidate_sha,
                    "navigation_input_fingerprint": self.fingerprint}
        approved = copy.deepcopy(candidate)
        approved["status"] = "NAVIGATION_APPROVED_BY_REVIEW"
        approved["provenance"] = {**provenance, "review_candidate_sha256": self.candidate_sha}
        approved["review"] = {
            "approved": True, "status": "APPROVED",
            "decisions": [{"decision": "APPROVE", "reviewer": decision["reviewer"],
                           "source_artifact_sha256": self.candidate_sha,
                           "timestamp_utc": decision["reviewed_at_utc"]}],
        }
        approved["validation"] = {"status": "PASS", "public_ready": True,
                                  "review_status": "NAVIGATION_APPROVED_BY_REVIEW"}
        approved["viewer_compatibility"] = {
            "envelope": {"id": "boundary-1", "horizontalBoundary": {"regions": [{"regionId": "r1"}]}},
            "stations": {"defaultStationId": "s1", "stations": [{"stationId": "s1"}]},
            "runtime_policy": {"freeXYZWithinEnvelope": True},
        }
        approved_bytes = json.dumps(approved, sort_keys=True).encode()
        self.approved_path.write_bytes(approved_bytes)
        self.reviewed_path.write_bytes(approved_bytes)
        self.approved_sha = self._sha(self.approved_path)

        for path in (self.proposal_path, self.draft_path, self.approved_path, self.reviewed_path):
            self.state.add_artifact("navigation", path.resolve(), "navigation_json", self._sha(path),
                                    path.stat().st_size,
                                    {"project_id": self.config["project_id"]})
        self.state.record_review_decision("navigation-approval", "APPROVED", required=True,
                                          reviewer=decision["reviewer"], decision="APPROVE",
                                          payload=decision)
        details = {"status": "NAVIGATION_NEEDS_REVIEW",
                   "proposal_path": str(self.proposal_path.resolve()),
                   "review_candidate_path": str(self.draft_path.resolve()),
                   "draft_sha256": self.candidate_sha,
                   "approved_artifact": str(self.approved_path.resolve()),
                   "reviewed_version": str(self.reviewed_path.resolve()),
                   "approval": decision}
        self.state.set_stage("navigation", "ADOPTED", self.fingerprint, "fixture-output-digest", 12, details)
        self.stage_fingerprint_patch = patch(
            "exhibition_archive.workflow.stage_fingerprint", return_value=self.fingerprint)
        self.validation_patch = patch(
            "exhibition_archive.navigation_review.validate_navigation_artifact",
            return_value={"status": "PASS", "finite_geometry": True, "errors": []})
        self.stage_fingerprint_patch.start()
        self.validation_patch.start()

    def tearDown(self):
        self.stage_fingerprint_patch.stop()
        self.validation_patch.stop()
        self.state.close()
        self.temporary.cleanup()

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_adopted_approval_wins_over_historical_generation_status(self):
        context = preview_navigation_context(self.root, self.config, self.state)
        self.assertTrue(context["approved"])
        self.assertEqual(context["stage_status"], "ADOPTED")
        self.assertEqual(context["gate_status"], "APPROVED")
        self.assertEqual(context["artifact_sha256"], self.approved_sha)
        self.assertEqual(context["candidate_sha256"], self.candidate_sha)
        self.assertEqual(context["artifact"]["status"], "NAVIGATION_APPROVED_BY_REVIEW")

    def test_preview_refresh_accepts_adopted_approval_without_creating_new_review(self):
        model = self.root / "existing-model.ply"
        model.write_bytes(b"validated fixture candidate")
        run_id = self.state.start_run("fixture-training")
        training_execution = self.state.start_stage(run_id, "training", self.fingerprint, False, {})
        self.state.finish_stage(training_execution, "COMPLETED", "training-digest",
                                {"artifacts": [str(model)]})
        self.state.set_stage("training", "COMPLETED", self.fingerprint, "training-digest",
                             training_execution, {"model_path": str(model)})
        self.state.end_run(run_id, "COMPLETED", {"status": "COMPLETED"})
        generated = self.root / "reviewer.html"
        generated.write_text("fixture", encoding="utf-8")
        history_before = self.state.db.execute(
            "SELECT COUNT(*) FROM review_history WHERE gate_id='navigation-approval'"
        ).fetchone()[0]
        with patch("exhibition_archive.preview.build_first_visual_preview",
                   return_value={"status": "COMPLETED", "artifacts": [generated], "details": {}}) as builder:
            result = run_first_visual_preview_only(self.root, self.config, self.state)
        self.assertEqual(result["status"], "AWAITING_VISUAL_REVIEW")
        self.assertTrue(builder.call_args.kwargs["navigation_context"]["approved"])
        self.assertEqual(self.state.stage("navigation")["status"], "ADOPTED")
        self.assertEqual(self.state.stage("navigation")["last_execution_id"], 12)
        self.assertEqual(self.state.db.execute(
            "SELECT COUNT(*) FROM review_history WHERE gate_id='navigation-approval'"
        ).fetchone()[0], history_before)

    def test_modified_draft_fails_its_registered_and_reviewed_identity(self):
        self.draft_path.write_text(self.draft_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "validated Engine artifact record"):
            preview_navigation_context(self.root, self.config, self.state)

    def test_corrupted_approved_artifact_fails_without_using_reviewed_copy_as_fallback(self):
        self.approved_path.write_text(self.approved_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "validated Engine artifact record"):
            preview_navigation_context(self.root, self.config, self.state)

    def test_proposal_identity_mismatch_blocks_the_current_approval(self):
        proposal = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        proposal["id"] = "different-proposal"
        self.proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        self.state.db.execute(
            "UPDATE artifacts SET sha256=?,size_bytes=? WHERE stage_id='navigation' AND path=?",
            (self._sha(self.proposal_path), self.proposal_path.stat().st_size,
             str(self.proposal_path.resolve())),
        )
        self.state.db.commit()
        with self.assertRaisesRegex(ValueError, "does not refer to the approved proposal identity"):
            preview_navigation_context(self.root, self.config, self.state)

    def test_stale_navigation_fingerprint_blocks_enforcement(self):
        with patch("exhibition_archive.workflow.stage_fingerprint", return_value="stale"):
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                preview_navigation_context(self.root, self.config, self.state)

    def test_missing_approved_copy_fails_without_falling_back_to_proposal(self):
        self.approved_path.unlink()
        with self.assertRaisesRegex(ValueError, "approved artifact is missing"):
            preview_navigation_context(self.root, self.config, self.state)

    def test_draft_sha_mismatch_blocks_approval_binding(self):
        row = self.state.db.execute(
            "SELECT decision_json FROM review_gates WHERE gate_id='navigation-approval'"
        ).fetchone()
        decision = json.loads(row["decision_json"])
        decision["candidate_sha256"] = "00" * 32
        self.state.db.execute(
            "UPDATE review_gates SET decision_json=? WHERE gate_id='navigation-approval'",
            (json.dumps(decision),),
        )
        self.state.db.commit()
        with self.assertRaisesRegex(ValueError, "does not bind the current draft"):
            preview_navigation_context(self.root, self.config, self.state)

    def test_unapproved_preview_stays_unbounded(self):
        self.state.set_stage("navigation", "NEEDS_REVIEW", self.fingerprint, "proposal-digest", 12,
                             {"status": "NAVIGATION_NEEDS_REVIEW",
                              "proposal_path": str(self.proposal_path.resolve())})
        self.state.db.execute(
            "UPDATE review_gates SET status='PENDING',decision_json=? WHERE gate_id='navigation-approval'",
            (json.dumps({"decision": "PENDING"}),),
        )
        self.state.db.commit()
        context = preview_navigation_context(self.root, self.config, self.state)
        self.assertFalse(context["approved"])
        self.assertEqual(context["gate_status"], "PENDING")
        self.assertIsNone(context["artifact"])

    def test_rejected_navigation_cannot_enable_boundary(self):
        self.state.set_stage("navigation", "NEEDS_REVIEW", self.fingerprint, "proposal-digest", 12,
                             {"status": "NAVIGATION_NEEDS_REVIEW",
                              "proposal_path": str(self.proposal_path.resolve())})
        self.state.db.execute(
            "UPDATE review_gates SET status='REJECTED',decision_json=? WHERE gate_id='navigation-approval'",
            (json.dumps({"decision": "REJECT"}),),
        )
        self.state.db.commit()
        context = preview_navigation_context(self.root, self.config, self.state)
        self.assertFalse(context["approved"])
        self.assertEqual(context["gate_status"], "REJECTED")

    def test_approved_gate_with_non_adopted_stage_is_ambiguous_and_fails_closed(self):
        self.state.set_stage("navigation", "NEEDS_REVIEW", self.fingerprint, "proposal-digest", 12,
                             {"status": "NAVIGATION_NEEDS_REVIEW",
                              "proposal_path": str(self.proposal_path.resolve())})
        with self.assertRaisesRegex(ValueError, "not ADOPTED"):
            preview_navigation_context(self.root, self.config, self.state)


if __name__ == "__main__":
    unittest.main()
