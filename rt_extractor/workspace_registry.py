"""Bounded, session-owned, memory-only authentication workspaces.

This module never creates, scans, or writes a directory. Tokens exist only while
being handed to the owning browser and are erased on acknowledgement or expiry.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import weakref

from .abuse import AbuseLimited
from .batch import BatchManager
from .web_jobs import ACTIVE, JobManager


_registries = weakref.WeakSet()
_janitor_lock = threading.Lock()
_janitor_started = False


def _start_janitor(registry):
    global _janitor_started
    with _janitor_lock:
        _registries.add(registry)
        if _janitor_started:
            return
        _janitor_started = True

    def sweep():
        while True:
            threading.Event().wait(30)
            with _janitor_lock:
                targets = list(_registries)
            for target in targets:
                target.cleanup()

    threading.Thread(target=sweep, name="browser-workspace-expiry", daemon=True).start()


@dataclass
class Workspace:
    owner: str
    jobs: JobManager
    batches: BatchManager
    last_seen: float

    def active(self):
        return bool(self.jobs.active_batch or any(job.status in ACTIVE for job in self.jobs.jobs.values()))

    def discard(self):
        with self.jobs.lock:
            for batch in list(self.batches.batches.values()):
                if batch.status not in {"completed", "cancelled"}:
                    self.batches._request_cancel(batch)
                for credentials in batch.credential_records:
                    credentials.clear()
                batch.credential_records.clear()
            for job in list(self.jobs.jobs.values()):
                if job.status in ACTIVE:
                    self.jobs.cancel(job.id)
                self.jobs.acknowledge(job)
                job.email = ""
                job.events.clear()
                job.error = None
                job.result = None
            self.jobs.jobs.clear()
            self.batches.batches.clear()
            self.jobs.current_id = self.batches.current_id = None


class WorkspaceRegistry:
    def __init__(self, extractor, *, clock=time.monotonic, max_workspaces=8,
                 worker_limit=6, result_ttl=600, idle_ttl=600):
        self.extractor, self.clock = extractor, clock
        self.max_workspaces, self.result_ttl, self.idle_ttl = max_workspaces, result_ttl, idle_ttl
        self.worker_slots = threading.BoundedSemaphore(worker_limit)
        self.worker_limit = worker_limit
        self.lock = threading.RLock()
        self.workspaces: dict[str, Workspace] = {}
        _start_janitor(self)

    def ensure(self, owner: str) -> Workspace:
        self.cleanup()
        with self.lock:
            workspace = self.workspaces.get(owner)
            if workspace is None:
                if len(self.workspaces) >= self.max_workspaces:
                    raise AbuseLimited(5, "当前处理名额已满，请稍后重试。")
                jobs = JobManager(None, self.extractor, ephemeral=True,
                                  worker_slots=self.worker_slots, clock=self.clock)
                workspace = Workspace(owner, jobs, BatchManager(jobs), self.clock())
                self.workspaces[owner] = workspace
            workspace.last_seen = self.clock()
            return workspace

    def get(self, owner: str, *, touch=True) -> Workspace | None:
        self.cleanup()
        with self.lock:
            workspace = self.workspaces.get(owner)
            if workspace is not None and touch:
                workspace.last_seen = self.clock()
            return workspace

    def remove(self, owner: str, *, expected: Workspace | None = None) -> bool:
        with self.lock:
            workspace = self.workspaces.get(owner)
            if workspace is None or expected is not None and workspace is not expected:
                return False
            self.workspaces.pop(owner)
        workspace.discard()
        return True

    def release_empty(self, owner: str, *, expected: Workspace | None = None):
        with self.lock:
            workspace = self.workspaces.get(owner)
            if workspace is not None and (expected is None or workspace is expected):
                with workspace.jobs.lock:
                    if not workspace.jobs.jobs and not workspace.batches.batches:
                        self.workspaces.pop(owner)

    def cleanup(self):
        now = self.clock()
        expired = []
        with self.lock:
            for owner, workspace in list(self.workspaces.items()):
                with workspace.jobs.lock:
                    active = workspace.active()
                    for job in workspace.jobs.jobs.values():
                        if (job.payload is not None and job.finished_at is not None
                                and now - job.finished_at >= self.result_ttl):
                            workspace.jobs.acknowledge(job)
                            if job.result:
                                job.result["delivery_status"] = "expired"
                    # Later tasks must not extend earlier terminal metadata's
                    # lifetime. Active batches retain row progress until the
                    # batch ends, while each row's Token TTL remains independent.
                    removed = False
                    for batch_id, batch in list(workspace.batches.batches.items()):
                        if (batch.finished_at is not None
                                and now - batch.finished_at >= self.result_ttl):
                            workspace.batches.batches.pop(batch_id)
                            for job in batch.rows:
                                workspace.jobs.jobs.pop(job.id, None)
                                workspace.jobs.acknowledge(job)
                                job.email = ""
                                job.events.clear()
                                job.error = job.result = None
                            removed = True
                    for job_id, job in list(workspace.jobs.jobs.items()):
                        if (not job.batch_id and job.finished_at is not None
                                and now - job.finished_at >= self.result_ttl):
                            workspace.jobs.jobs.pop(job_id)
                            workspace.jobs.acknowledge(job)
                            job.email = ""
                            job.events.clear()
                            job.error = job.result = None
                            removed = True
                    if workspace.jobs.current_id not in workspace.jobs.jobs:
                        workspace.jobs.current_id = None
                    if workspace.batches.current_id not in workspace.batches.batches:
                        workspace.batches.current_id = None
                    terminal_expired = (removed and not active and not workspace.jobs.jobs
                                        and not workspace.batches.batches)
                    if terminal_expired or now - workspace.last_seen >= self.idle_ttl:
                        self.workspaces.pop(owner)
                        expired.append(workspace)
        for workspace in expired:
            workspace.discard()
