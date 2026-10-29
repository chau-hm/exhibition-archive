from __future__ import annotations

import contextlib
import fcntl
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ProjectState:
    """SQLite state with process-level exclusion for mutations to one project."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "workflow.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_runs(
          run_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
          mode TEXT NOT NULL, status TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS stage_executions(
          execution_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES workflow_runs(run_id),
          stage_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
          input_fingerprint TEXT, output_digest TEXT, command_json TEXT NOT NULL DEFAULT '[]',
          workdir TEXT, pid INTEGER, checkpoint_path TEXT, log_path TEXT, error TEXT,
          adopted INTEGER NOT NULL DEFAULT 0, details_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS stage_states(
          stage_id TEXT PRIMARY KEY, status TEXT NOT NULL, input_fingerprint TEXT,
          output_digest TEXT, last_execution_id INTEGER, updated_at TEXT NOT NULL,
          details_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS artifacts(
          artifact_id INTEGER PRIMARY KEY, stage_id TEXT NOT NULL, path TEXT NOT NULL,
          artifact_type TEXT NOT NULL, sha256 TEXT, size_bytes INTEGER, status TEXT NOT NULL,
          provenance_json TEXT NOT NULL DEFAULT '{}', UNIQUE(stage_id,path));
        CREATE TABLE IF NOT EXISTS review_gates(
          gate_id TEXT PRIMARY KEY, status TEXT NOT NULL, required INTEGER NOT NULL,
          updated_at TEXT NOT NULL, decision_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS fingerprint_cache(
          path TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
          sha256 TEXT NOT NULL, verified_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS review_history(
          review_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, gate_id TEXT NOT NULL,
          reviewer TEXT, decision TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS execution_authorizations(
          authorization_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, stage_id TEXT NOT NULL,
          plan_id TEXT NOT NULL, plan_json TEXT NOT NULL, reviewer TEXT NOT NULL,
          rationale TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL, consumed_at TEXT, execution_id INTEGER,
          stale_reason TEXT);
        """)
        self.db.commit()

    @contextlib.contextmanager
    def lock(self):
        lock_path = self.root / ".workflow.lock"
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def start_run(self, mode: str) -> int:
        cur = self.db.execute("INSERT INTO workflow_runs(started_at,mode,status) VALUES(?,?,?)", (utc_now(), mode, "RUNNING"))
        self.db.commit()
        return int(cur.lastrowid)

    def end_run(self, run_id: int, status: str, summary: dict) -> None:
        self.db.execute("UPDATE workflow_runs SET ended_at=?,status=?,summary_json=? WHERE run_id=?", (utc_now(), status, json.dumps(summary, sort_keys=True), run_id))
        self.db.commit()

    def stage(self, stage_id: str):
        row = self.db.execute("SELECT * FROM stage_states WHERE stage_id=?", (stage_id,)).fetchone()
        return dict(row) if row else None

    def stages(self):
        return {r["stage_id"]: dict(r) for r in self.db.execute("SELECT * FROM stage_states")}

    def set_stage(self, stage_id: str, status: str, fingerprint: str | None, output_digest: str | None, execution_id: int | None, details: dict):
        self.db.execute("""INSERT INTO stage_states(stage_id,status,input_fingerprint,output_digest,last_execution_id,updated_at,details_json)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(stage_id) DO UPDATE SET status=excluded.status,input_fingerprint=excluded.input_fingerprint,
          output_digest=excluded.output_digest,last_execution_id=excluded.last_execution_id,updated_at=excluded.updated_at,details_json=excluded.details_json""",
          (stage_id, status, fingerprint, output_digest, execution_id, utc_now(), json.dumps(details, sort_keys=True)))
        self.db.commit()

    def start_stage(self, run_id: int, stage_id: str, fingerprint: str, adopted: bool, details: dict) -> int:
        cur = self.db.execute("INSERT INTO stage_executions(run_id,stage_id,status,started_at,input_fingerprint,adopted,details_json) VALUES(?,?,?,?,?,?,?)",
                              (run_id, stage_id, "RUNNING", utc_now(), fingerprint, int(adopted), json.dumps(details, sort_keys=True)))
        self.db.commit()
        return int(cur.lastrowid)

    def finish_stage(self, execution_id: int, status: str, digest: str | None, details: dict, error: str | None = None):
        self.db.execute("UPDATE stage_executions SET ended_at=?,status=?,output_digest=?,details_json=?,error=? WHERE execution_id=?",
                        (utc_now(), status, digest, json.dumps(details, sort_keys=True), error, execution_id))
        self.db.commit()

    def update_stage_execution(self, execution_id: int, *, command: list[str] | None = None,
                               workdir: str | None = None, pid: int | None = None,
                               log_path: str | None = None, checkpoint_path: str | None = None,
                               details: dict | None = None):
        row = self.db.execute("SELECT details_json FROM stage_executions WHERE execution_id=?", (execution_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown stage execution {execution_id}")
        merged = json.loads(row["details_json"] or "{}")
        if details:
            merged.update(details)
        self.db.execute("""UPDATE stage_executions SET
          command_json=COALESCE(?,command_json),workdir=COALESCE(?,workdir),pid=COALESCE(?,pid),
          log_path=COALESCE(?,log_path),checkpoint_path=COALESCE(?,checkpoint_path),details_json=?
          WHERE execution_id=?""",
          (json.dumps(command) if command is not None else None, workdir, pid, log_path, checkpoint_path,
           json.dumps(merged, sort_keys=True), execution_id))
        self.db.commit()

    def execution(self, execution_id: int):
        row = self.db.execute("SELECT * FROM stage_executions WHERE execution_id=?", (execution_id,)).fetchone()
        return dict(row) if row else None

    def create_execution_authorization(self, authorization_id: str, project_id: str, stage_id: str,
                                       plan_id: str, plan: dict, reviewer: str, rationale: str,
                                       expires_at: str) -> None:
        self.db.execute("""INSERT INTO execution_authorizations(
          authorization_id,project_id,stage_id,plan_id,plan_json,reviewer,rationale,status,created_at,expires_at)
          VALUES(?,?,?,?,?,?,?,'ACTIVE',?,?)""",
          (authorization_id, project_id, stage_id, plan_id, json.dumps(plan, sort_keys=True),
           reviewer, rationale, utc_now(), expires_at))
        self.db.commit()

    def execution_authorizations(self):
        return [dict(row) for row in self.db.execute(
            "SELECT authorization_id,project_id,stage_id,plan_id,reviewer,rationale,status,created_at,expires_at,consumed_at,execution_id,stale_reason FROM execution_authorizations ORDER BY created_at DESC")]

    def execution_authorization(self, authorization_id: str):
        row = self.db.execute("SELECT * FROM execution_authorizations WHERE authorization_id=?", (authorization_id,)).fetchone()
        return dict(row) if row else None

    def consume_execution_authorization(self, authorization_id: str, plan_id: str, execution_id: int) -> bool:
        now = utc_now()
        cur = self.db.execute("""UPDATE execution_authorizations SET status='CONSUMED',consumed_at=?,execution_id=?
          WHERE authorization_id=? AND plan_id=? AND status='ACTIVE' AND expires_at>?""",
          (now, execution_id, authorization_id, plan_id, now))
        self.db.commit()
        return cur.rowcount == 1

    def mark_authorization_stale(self, authorization_id: str, reason: str) -> None:
        self.db.execute("UPDATE execution_authorizations SET status='STALE',stale_reason=? WHERE authorization_id=? AND status='ACTIVE'",
                         (reason, authorization_id))
        self.db.commit()

    def add_artifact(self, stage_id: str, path: Path, artifact_type: str, sha256: str | None, size: int | None, provenance: dict):
        self.db.execute("""INSERT INTO artifacts(stage_id,path,artifact_type,sha256,size_bytes,status,provenance_json) VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(stage_id,path) DO UPDATE SET artifact_type=excluded.artifact_type,sha256=excluded.sha256,size_bytes=excluded.size_bytes,status=excluded.status,provenance_json=excluded.provenance_json""",
          (stage_id, str(path), artifact_type, sha256, size, "VALIDATED", json.dumps(provenance, sort_keys=True)))
        self.db.commit()

    def cached_hash(self, path: Path, force: bool = False) -> str:
        import hashlib
        path = path.resolve()
        stat = path.stat()
        row = self.db.execute("SELECT * FROM fingerprint_cache WHERE path=?", (str(path),)).fetchone()
        if not force and row and row["size_bytes"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
            return str(row["sha256"])
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        value = digest.hexdigest()
        self.db.execute("INSERT INTO fingerprint_cache(path,size_bytes,mtime_ns,sha256,verified_at) VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,sha256=excluded.sha256,verified_at=excluded.verified_at", (str(path), stat.st_size, stat.st_mtime_ns, value, utc_now()))
        self.db.commit()
        return value

    def close(self):
        self.db.close()
