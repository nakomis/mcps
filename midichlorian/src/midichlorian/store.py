"""SQLite persistence for requests, jobs and deploy records.

Synchronous on purpose: one connection, one lock. Calls are fast and local, so
async callers use it directly.
"""

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATES = frozenset(
    "queued waiting running succeeded failed skipped cancelled interrupted already".split()
)
_SETTABLE = frozenset({"started", "finished", "result", "detail", "log_path"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hosts TEXT NOT NULL,
    sha TEXT NOT NULL,
    tags TEXT,
    override_printer INTEGER NOT NULL,
    requester TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id),
    host TEXT NOT NULL,
    sha TEXT NOT NULL,
    tags TEXT,
    state TEXT NOT NULL,
    position INTEGER NOT NULL,
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT,
    result TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    log_path TEXT,
    requesters TEXT NOT NULL,
    override_printer INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_host ON jobs(host);
CREATE INDEX IF NOT EXISTS jobs_request ON jobs(request_id);
CREATE TABLE IF NOT EXISTS deploys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    sha TEXT NOT NULL,
    tags TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deploys_host ON deploys(host);
"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tags_out(raw: str | None) -> list[str] | None:
    return None if raw is None else json.loads(raw)


def _tags_in(tags: list[str] | None) -> str | None:
    return None if tags is None else json.dumps(list(tags))


@dataclass
class Job:
    id: int
    request_id: int
    host: str
    sha: str
    tags: list[str] | None
    state: str
    position: int
    created: str
    started: str | None = None
    finished: str | None = None
    result: str | None = None
    detail: dict = field(default_factory=dict)
    log_path: str | None = None
    requesters: list[str] = field(default_factory=list)
    override_printer: bool = False


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        request_id=row["request_id"],
        host=row["host"],
        sha=row["sha"],
        tags=_tags_out(row["tags"]),
        state=row["state"],
        position=row["position"],
        created=row["created"],
        started=row["started"],
        finished=row["finished"],
        result=row["result"],
        detail=json.loads(row["detail"]),
        log_path=row["log_path"],
        requesters=json.loads(row["requesters"]),
        override_printer=bool(row["override_printer"]),
    )


class Store:
    def __init__(self, path: str | Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def create_request(
        self,
        hosts: list[str],
        sha: str,
        tags: list[str] | None,
        override_printer: bool,
        requester: str,
    ) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO requests (hosts, sha, tags, override_printer, requester, created)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (json.dumps(list(hosts)), sha, _tags_in(tags), int(override_printer), requester, _now()),
            )
            self._db.commit()
            return cur.lastrowid

    def add_job(
        self,
        request_id: int,
        host: str,
        sha: str,
        tags: list[str] | None,
        position: int,
        override_printer: bool,
        requester: str,
    ) -> Job:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO jobs (request_id, host, sha, tags, state, position, created,"
                " requesters, override_printer) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                (request_id, host, sha, _tags_in(tags), position, _now(),
                 json.dumps([requester]), int(override_printer)),
            )
            self._db.commit()
            return self._get(cur.lastrowid)

    def _get(self, job_id: int) -> Job | None:
        row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    def get_job(self, job_id: int) -> Job | None:
        with self._lock:
            return self._get(job_id)

    def merge_requester(
        self, job_id: int, requester: str, sha: str | None, tags: list[str] | None
    ) -> None:
        """Add a requester; store the given sha if any; widen tags (None = full wins)."""
        with self._lock:
            job = self._get(job_id)
            if job is None:
                raise KeyError(job_id)
            requesters = job.requesters + ([requester] if requester not in job.requesters else [])
            merged = None if job.tags is None or tags is None else sorted(set(job.tags) | set(tags))
            self._db.execute(
                "UPDATE jobs SET requesters = ?, sha = ?, tags = ? WHERE id = ?",
                (json.dumps(requesters), sha or job.sha, _tags_in(merged), job_id),
            )
            self._db.commit()

    def jobs(
        self, host: str | None = None, states: list[str] | None = None, limit: int = 100
    ) -> list[Job]:
        clauses, args = [], []
        if host is not None:
            clauses.append("host = ?")
            args.append(host)
        if states:
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            args.extend(states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?", (*args, limit)
            ).fetchall()
        return [_job(r) for r in rows]

    def request_jobs(self, request_id: int) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE request_id = ? ORDER BY position, id", (request_id,)
            ).fetchall()
        return [_job(r) for r in rows]

    def set_state(self, job_id: int, state: str, **fields) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state: {state}")
        unknown = set(fields) - _SETTABLE
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        if "detail" in fields:
            fields["detail"] = json.dumps(fields["detail"])
        sets = ", ".join(f"{k} = ?" for k in ("state", *fields))
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (state, *fields.values(), job_id))
            self._db.commit()

    def recover_interrupted(self) -> list[int]:
        with self._lock:
            ids = [r["id"] for r in self._db.execute("SELECT id FROM jobs WHERE state = 'running'")]
            self._db.execute(
                "UPDATE jobs SET state = 'interrupted', finished = ? WHERE state = 'running'", (_now(),)
            )
            self._db.commit()
        return ids

    def record_deploy(self, host: str, sha: str, tags: list[str] | None, at: str) -> None:
        stored = None if tags is None else json.dumps(sorted(tags))
        with self._lock:
            self._db.execute(
                "INSERT INTO deploys (host, sha, tags, at) VALUES (?, ?, ?, ?)", (host, sha, stored, at)
            )
            self._db.commit()

    def deployed(self, host: str) -> dict | None:
        with self._lock:
            rows = self._db.execute(
                "SELECT sha, tags, at FROM deploys WHERE host = ? ORDER BY at, id", (host,)
            ).fetchall()
        if not rows:
            return None
        full = None
        latest: dict[str, dict] = {}
        for r in rows:  # ascending, so later rows overwrite earlier ones
            if r["tags"] is None:
                full = {"sha": r["sha"], "at": r["at"]}
            else:
                latest.pop(r["tags"], None)  # re-insert so dict order follows recency
                latest[r["tags"]] = {"sha": r["sha"], "tags": json.loads(r["tags"]), "at": r["at"]}
        return {"full": full, "tagged": list(latest.values())}
