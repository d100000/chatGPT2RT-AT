"""Bounded concurrent extraction, with full validation before the first login."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any
import uuid
import zipfile

from .cpa import save_export
from .web_jobs import ACTIVE, Job, JobManager, _credentials, converted_result

MAX_ACCOUNTS = 1000
DEFAULT_CONCURRENCY = 2
MAX_CONCURRENCY = 3
_CONVERSION_ERROR = "批量 CPA / Sub2API 自动生成失败；单账号文件已保留，可重试下载或检查输出目录权限"
_ID = re.compile(r"[0-9a-f]{32}")
_TERMINAL_BATCH = {"completed", "cancelled"}
_TERMINAL_JOB = {"success", "error", "cancelled"}


class BatchInputError(ValueError):
    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__("导入内容有误，请修改标出的行后重试")


def validate_concurrency(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_CONCURRENCY:
        raise ValueError(f"同时处理账号数须为 1 到 {MAX_CONCURRENCY} 的整数")
    return value


def parse_accounts(payload: Any) -> list[tuple[int, dict]]:
    """Parse email----password----TOTP; never include input lines in errors."""
    if not isinstance(payload, dict) or set(payload) - {"accounts_text", "proxy", "timeout", "concurrency"}:
        raise BatchInputError([{"line": 0, "message": "请求格式无效"}])
    try:
        validate_concurrency(payload.get("concurrency", DEFAULT_CONCURRENCY))
    except ValueError as exc:
        raise BatchInputError([{"line": 0, "message": str(exc)}]) from None
    text = payload.get("accounts_text")
    if not isinstance(text, str) or not text.strip():
        raise BatchInputError([{"line": 0, "message": "请粘贴账号列表，每行一个账号"}])
    try:
        input_size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise BatchInputError([{"line": 0, "message": "输入包含无效字符，请使用 UTF-8 文本"}]) from None
    if input_size > 2 * 1024 * 1024:
        raise BatchInputError([{"line": 0, "message": "导入内容不能超过 2 MB"}])
    lines = [(number, raw) for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1) if raw.strip()]
    if not lines:
        raise BatchInputError([{"line": 0, "message": "请粘贴账号列表，每行一个账号"}])
    if len(lines) > MAX_ACCOUNTS:
        raise BatchInputError([{"line": 0, "message": f"每批最多 {MAX_ACCOUNTS} 条，请拆分后提交"}])
    errors, accounts, seen = [], [], set()
    for number, raw in lines:
        fields = raw.split("----")
        if len(fields) != 3:
            errors.append({"line": number, "message": "格式应为 邮箱----密码----2FA密钥，且只有三个字段"})
            continue
        email, password, secret = fields
        if not secret.strip():
            errors.append({"line": number, "message": "2FA 密钥不能为空"})
            continue
        try:
            credentials = _credentials({
                "email": email, "password": password, "totp_secret": secret,
                "proxy": payload.get("proxy"), "timeout": payload.get("timeout", 180),
            })
        except ValueError as exc:
            errors.append({"line": number, "message": str(exc)})
            continue
        key = credentials["email"].casefold()
        if key in seen:
            errors.append({"line": number, "message": "邮箱在本批次中重复，请移除重复行"})
            credentials.clear()
            continue
        seen.add(key)
        accounts.append((number, credentials))
    if errors:
        for _, credentials in accounts:
            credentials.clear()
        raise BatchInputError(errors)
    return accounts


@dataclass
class Batch:
    id: str
    directory: Path | None
    rows: list[Job]
    concurrency: int = DEFAULT_CONCURRENCY
    status: str = "queued"
    current: Job | None = None
    cancel_requested: bool = False
    error: str | None = None
    conversion_status: str = "not_saved"
    conversion_error: str | None = None
    inflight: set[str] = field(default_factory=set, repr=False)
    dispatch_not_before: float = field(default=0, repr=False)
    wake: threading.Event = field(default_factory=threading.Event, repr=False)
    finished_at: float | None = field(default=None, repr=False)
    credential_records: list[dict] = field(default_factory=list, repr=False)

    def snapshot(self) -> dict:
        counts = {"total": len(self.rows), "queued": 0, "running": 0, "awaiting_code": 0,
                  "success": 0, "error": 0, "cancelled": 0}
        for job in self.rows:
            counts[job.status] += 1
        active = [job for job in self.rows if job.status in {"running", "awaiting_code"}]
        status = self.status
        if status == "running" and active and all(job.status == "awaiting_code" for job in active):
            status = "awaiting_code"
        current = next((job for job in active if job.status == "awaiting_code"), next(iter(active), None))
        result = None
        if status in {"completed", "cancelled"} and counts["success"]:
            result = {
                "conversion_status": self.conversion_status,
                "conversion_error": self.conversion_error,
                "cpa_filename": f"cpa-{self.id}.zip",
                "sub2api_filename": f"sub2api-{self.id}.json",
            } if self.directory is None else {
                "directory": str(self.directory),
                "sub2api_url": f"/api/batches/{self.id}/sub2api-download",
                "cpa_url": f"/api/batches/{self.id}/cpa-download",
                "zip_url": f"/api/batches/{self.id}/download",
                "rt_url": f"/api/batches/{self.id}/rt-download",
                "conversion_status": self.conversion_status,
                "conversion_error": self.conversion_error,
                "cpa_filename": f"cpa-{self.id}.zip",
                "cpa_path": str(self.directory / f"cpa-{self.id}.zip"),
                "sub2api_filename": f"sub2api-{self.id}.json",
                "sub2api_path": str(self.directory / f"sub2api-{self.id}.json"),
            }
        return {
            "id": self.id, "status": status, "concurrency": self.concurrency, "counts": counts,
            "cooldown_seconds": max(0, math.ceil(self.dispatch_not_before - time.monotonic()))
                if counts["queued"] and status not in _TERMINAL_BATCH else 0,
            "rows": [{key: value for key, value in job.snapshot().items()
                      if key not in {"events", "challenge"}} for job in self.rows],
            "active_jobs": [job.snapshot() for job in active],
            "current": current.snapshot() if current else None,
            "result": result, "error": self.error,
        }


class BatchManager:
    def __init__(self, jobs: JobManager) -> None:
        self.jobs = jobs
        self.lock = jobs.lock
        self.batches: dict[str, Batch] = {}
        self.current_id: str | None = None
        if not jobs.ephemeral:
            self._restore_completed()

    def _restore_completed(self) -> None:
        """Recover only terminal metadata; never load credentials or resume logins."""
        candidates = []
        for directory in self.jobs.output_dir.glob("batch-*"):
            if not _ID.fullmatch(directory.name.removeprefix("batch-")):
                continue
            state_file = directory / "batch-state.json"
            try:
                if (directory.is_symlink() or not directory.is_dir()
                        or state_file.is_symlink() or not state_file.is_file()):
                    continue
                stat = state_file.stat()
                if stat.st_size > 8 * 1024 * 1024:
                    continue
                candidates.append((stat.st_mtime_ns, directory, state_file))
            except OSError:
                continue
        restored = []
        restored_ids = set()
        for _, directory, state_file in sorted(candidates, reverse=True):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                batch = self._restored_batch(directory, state)
                if any(row.id in restored_ids for row in batch.rows):
                    continue
            except (OSError, ValueError, TypeError, KeyError, RecursionError):
                continue
            restored.append(batch)
            restored_ids.update(row.id for row in batch.rows)
            if len(restored) == 3:
                break
        for batch in reversed(restored):
            self.batches[batch.id] = batch
            self.jobs.jobs.update((row.id, row) for row in batch.rows)
            self.current_id = batch.id
            self.jobs.current_id = batch.rows[-1].id

    @staticmethod
    def _restored_batch(directory: Path, state: Any) -> Batch:
        """Validate metadata and reconstruct download paths from trusted IDs."""
        if (not isinstance(state, dict) or state.get("id") != directory.name[6:]
                or state.get("status") not in _TERMINAL_BATCH
                or not isinstance(state.get("rows"), list)
                or not 1 <= len(state["rows"]) <= MAX_ACCOUNTS):
            raise ValueError("Invalid completed batch")

        def optional_text(value: Any, limit: int = 4096) -> str | None:
            if value is None or isinstance(value, str) and len(value) <= limit:
                return value
            raise ValueError("Invalid completed batch text")

        rows, seen = [], set()
        for saved in state["rows"]:
            if not isinstance(saved, dict):
                raise ValueError("Invalid completed row")
            job_id, email, line = saved.get("id"), saved.get("email"), saved.get("line")
            if (not isinstance(job_id, str) or not _ID.fullmatch(job_id) or job_id in seen
                    or not isinstance(email, str) or len(email) > 512
                    or not re.fullmatch(r"[^\s@]+@[^\s@]+", email)
                    or type(line) is not int or line < 1
                    or saved.get("status") not in _TERMINAL_JOB):
                raise ValueError("Invalid completed row")
            seen.add(job_id)
            row = Job(
                id=job_id, email=email, line=line, batch_id=state["id"],
                output_dir=directory, status=saved["status"],
                stage=optional_text(saved.get("stage")) or saved["status"],
                message=optional_text(saved.get("message")) or "已恢复已完成任务",
                error=optional_text(saved.get("error")),
                cancel_requested=saved["status"] == "cancelled",
            )
            if row.status == "success":
                saved_result = saved.get("result")
                if not isinstance(saved_result, dict):
                    raise ValueError("Missing completed result")
                output = directory / f"tokens-{job_id}.json"
                row.result = {
                    "filename": output.name, "path": str(output),
                    "download_url": f"/api/jobs/{job_id}/download",
                    "obtained_at": optional_text(saved_result.get("obtained_at"), 100) or "",
                    "expires_at": optional_text(saved_result.get("expires_at"), 100) or "",
                    **converted_result(job_id, directory),
                }
            rows.append(row)
        generated = [directory / f"cpa-{state['id']}.zip", directory / f"sub2api-{state['id']}.json"]
        saved_batch_result = state.get("result")
        conversion_status = "ready" if all(path.is_file() and not path.is_symlink() for path in generated) else (
            "error" if isinstance(saved_batch_result, dict) and saved_batch_result.get("conversion_status") == "error"
            else "not_saved"
        )
        return Batch(
            id=state["id"], directory=directory, rows=rows, status=state["status"],
            concurrency=validate_concurrency(state.get("concurrency", 1)),
            cancel_requested=state["status"] == "cancelled",
            error=optional_text(state.get("error")),
            conversion_status=conversion_status,
            conversion_error=_CONVERSION_ERROR if conversion_status == "error" else None,
        )

    @staticmethod
    def _save_completed(batch: Batch) -> None:
        """Atomically save the public snapshot without any token or input values."""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=batch.directory,
                prefix=".batch-state-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                if os.name == "posix":
                    os.fchmod(handle.fileno(), 0o600)
                json.dump(batch.snapshot(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, batch.directory / "batch-state.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def snapshot(self, batch_id: str | None = None) -> dict | None:
        with self.lock:
            batch = self.batches.get(batch_id or self.current_id)
            return batch.snapshot() if batch else None

    def start(self, accounts: list[tuple[int, dict]], *, concurrency: int = DEFAULT_CONCURRENCY) -> dict:
        concurrency = validate_concurrency(concurrency)
        with self.lock:
            if self.jobs.active_batch or any(job.status in ACTIVE for job in self.jobs.jobs.values()):
                raise ValueError("已有任务进行中，请先完成或取消当前任务")
            batch_id = uuid.uuid4().hex
            directory = None if self.jobs.ephemeral else self.jobs.output_dir / f"batch-{batch_id}"
            if directory is not None:
                directory.mkdir(mode=0o700)
            rows = [Job(id=uuid.uuid4().hex, email=data["email"], line=line,
                        batch_id=batch_id, output_dir=directory) for line, data in accounts]
            batch = Batch(id=batch_id, directory=directory, rows=rows, concurrency=concurrency,
                          credential_records=[data for _, data in accounts])
            while len(self.batches) >= 3:
                old = self.batches.pop(next(iter(self.batches)))
                for row in old.rows:
                    self.jobs.jobs.pop(row.id, None)
            self.jobs.jobs.update((row.id, row) for row in rows)
            self.jobs.active_batch = batch_id
            self.batches[batch_id] = batch
            self.current_id = batch_id
            worker = threading.Thread(target=self._run, args=(batch, accounts), daemon=True)
            try:
                worker.start()
            except BaseException:
                self.jobs.active_batch = None
                self.batches.pop(batch_id, None)
                self.current_id = next(reversed(self.batches), None)
                for row in rows:
                    self.jobs.jobs.pop(row.id, None)
                for _, credentials in accounts:
                    credentials.clear()
                accounts.clear()
                raise
            return batch.snapshot()

    def _cancel_remaining(self, batch: Batch) -> None:
        for row in batch.rows:
            if row.status in ACTIVE:
                row.status = "cancelled"
                row.cancel_requested = True
                row.challenge = None
                self.jobs._event(row, "cancelled", "批次已取消，该账号未完成")

    def _request_cancel(self, batch: Batch) -> None:
        """Called under the manager lock; block dispatch before cancelling workers."""
        if batch.cancel_requested:
            return
        batch.cancel_requested = True
        batch.status = "cancelling"
        batch.wake.set()
        for row in batch.rows:
            if row.status == "queued":
                row.cancel_requested = True
                row.status = "cancelled"
                self.jobs._event(row, "cancelled", "批次已取消，该账号未开始")
            elif row.id in batch.inflight:
                self.jobs.cancel(row.id)

    def _pause_dispatch(self, batch: Batch, seconds: int) -> None:
        with self.lock:
            if not batch.cancel_requested:
                batch.dispatch_not_before = max(batch.dispatch_not_before, time.monotonic() + seconds)

    def _consume(self, batch: Batch, pending: deque) -> None:
        while True:
            with self.lock:
                if batch.cancel_requested or not pending:
                    return
                delay = batch.dispatch_not_before - time.monotonic()
                if delay <= 0:
                    row, credentials = pending.popleft()
                    batch.inflight.add(row.id)
                    batch.current = row
                    self.jobs.current_id = row.id
                    row.status = "running"
                    self.jobs._event(row, "running", "准备登录")
            if delay > 0:
                batch.wake.wait(delay)
                continue
            try:
                # Each worker owns its event loop, HTTP session and credential dict.
                self.jobs._run(row, credentials, on_rate_limit=lambda delay: self._pause_dispatch(batch, delay))
            except Exception:
                with self.lock:
                    batch.error = "批次意外中断，已保留成功结果，请重新提交未完成账号"
                    self._request_cancel(batch)
            finally:
                with self.lock:
                    batch.inflight.discard(row.id)

    def _run(self, batch: Batch, accounts: list[tuple[int, dict]]) -> None:
        workers = []
        pending = deque((row, credentials) for row, (_, credentials) in zip(batch.rows, accounts))
        try:
            with self.lock:
                if not batch.cancel_requested:
                    batch.status = "running"
            for _ in range(min(batch.concurrency, len(batch.rows))):
                worker = threading.Thread(target=self._consume, args=(batch, pending), daemon=True)
                worker.start()
                workers.append(worker)
        except Exception:
            with self.lock:
                batch.error = "批次意外中断，已保留成功结果，请重新提交未完成账号"
                self._request_cancel(batch)
        finally:
            # asyncio.run waits for any synchronous VM executor work to finish.
            # Keep the batch guard and credentials intact until every worker exits.
            for worker in workers:
                worker.join()
            for _, credentials in accounts:
                credentials.clear()
            accounts.clear()
            batch.credential_records.clear()
            pending.clear()
            with self.lock:
                if batch.cancel_requested:
                    self._cancel_remaining(batch)
                batch.status = "cancelled" if batch.cancel_requested else "completed"
                batch.current = None
                self.jobs.active_batch = None
                batch.finished_at = self.jobs.clock()
                if self.jobs.ephemeral:
                    batch.conversion_status = "ready" if any(row.status == "success" for row in batch.rows) else "not_saved"
                else:
                    if any(row.status == "success" for row in batch.rows):
                        self._save_generated(batch)
                    try:
                        self._save_completed(batch)
                    except OSError:
                        batch.error = "批次状态保存失败；重启后请从保存目录查看已有结果"

    def _save_generated(self, batch: Batch) -> None:
        """Generate both batch formats before publishing terminal metadata."""
        generated = []
        try:
            exports = [self.export(batch.id, cpa=True), self.export(batch.id, sub2api=True)]
            for stream, filename, _mimetype in exports:
                generated.append(save_export(stream.getvalue(), batch.directory / filename))
            batch.conversion_status = "ready"
            batch.conversion_error = None
        except Exception:
            for path in generated:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            batch.conversion_status = "error"
            batch.conversion_error = _CONVERSION_ERROR
            batch.error = batch.conversion_error

    def cancel(self, batch_id: str) -> dict:
        with self.lock:
            batch = self.batches[batch_id]
            if batch.status in {"queued", "running"} and not batch.cancel_requested:
                self._request_cancel(batch)
            return batch.snapshot()

    def _export_payloads(self, batch_id: str) -> tuple[dict, list, list]:
        """Read every successful result, failing atomically for missing records."""
        if self.jobs.ephemeral:
            raise ValueError("网页结果仅在浏览器中导出")
        with self.lock:
            batch = self.batches[batch_id]
            snapshot = batch.snapshot()
            if not snapshot["result"]:
                raise ValueError("批次尚未结束或没有可下载的成功结果")
            rows = [(row.snapshot(), batch.directory / f"tokens-{row.id}.json") for row in batch.rows]
        successful = []
        for row, path in rows:
            if row["status"] != "success":
                continue
            if not path.is_file() or path.is_symlink():
                raise ValueError("成功结果文件已被移动，无法生成完整下载")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ValueError("成功结果文件无法读取") from None
            if not isinstance(payload, dict) or any(
                not isinstance(payload.get(key), str) or not payload[key].strip()
                for key in ("access_token", "refresh_token", "id_token")
            ):
                raise ValueError("成功结果文件的 Token 不完整")
            successful.append((row, path.name, payload))
        return snapshot, rows, successful

    def conversion_source(self, batch_id: str) -> dict:
        from .cpa import build_cpa_record

        _, _, successful = self._export_payloads(batch_id)
        # Start from CPA auth records rather than the legacy Sub2API download,
        # which intentionally omits expiry, refresh time, and plan metadata.
        return {"accounts": [build_cpa_record(payload) for _, _, payload in successful]}

    def export(self, batch_id: str, *, rt_only: bool = False, sub2api: bool = False,
               cpa: bool = False) -> tuple[io.BytesIO, str, str]:
        if sum((rt_only, sub2api, cpa)) > 1:
            raise ValueError("只能选择一种导出格式")
        snapshot, rows, successful = self._export_payloads(batch_id)
        if cpa:
            from .cpa import build_cpa_record

            # Convert all records before writing, so an incomplete result cannot
            # silently produce a partial archive. Job IDs keep names unique and
            # prevent account email addresses from becoming filesystem paths.
            records = [(row["id"], build_cpa_record(payload)) for row, _, payload in successful]
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for job_id, record in records:
                    archive.writestr(f"codex-{job_id}.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            output.seek(0)
            return output, f"cpa-{batch_id}.zip", "application/zip"
        if sub2api:
            from .sub2api import build_sub2api_export

            document = build_sub2api_export([payload for _, _, payload in successful])
            text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
            return io.BytesIO(text.encode("utf-8")), f"sub2api-{batch_id}.json", "application/json"
        if rt_only:
            text = "\n".join(payload["refresh_token"] for _, _, payload in successful) + "\n"
            return io.BytesIO(text.encode("utf-8")), f"rt-{batch_id}.txt", "text/plain; charset=utf-8"
        manifest = {
            "batch_id": batch_id, "status": snapshot["status"], "counts": snapshot["counts"],
            "accounts": [{"line": row["line"], "email": row["email"], "status": row["status"],
                          "error": row["error"], "filename": path.name if row["status"] == "success" else None}
                         for row, path in rows],
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for _, filename, payload in successful:
                archive.writestr(filename, json.dumps(payload, ensure_ascii=False, indent=2))
        output.seek(0)
        return output, f"tokens-{batch_id}.zip", "application/zip"
