"""Shared local web job execution; credentials are held only by the worker."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable
import uuid

from .api import ExtractionError, _validate_proxy
from .cpa import build_cpa_record, save_export
from .storage import save_tokens
from .sub2api import build_sub2api_export
from ._protocol.login import ProtocolError, RateLimitedError
from ._protocol.totp import normalize_totp_secret

ACTIVE = {"queued", "running", "awaiting_code"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def converted_result(job_id: str, directory: Path) -> dict[str, str]:
    """Build paths and URLs solely from manager-owned identifiers."""
    cpa = directory / f"codex-{job_id}.json"
    sub2api = directory / f"sub2api-{job_id}.json"
    ready = all(path.is_file() and not path.is_symlink() for path in (cpa, sub2api))
    return {
        "conversion_status": "ready" if ready else "not_saved",
        "cpa_filename": cpa.name, "cpa_path": str(cpa),
        "cpa_url": f"/api/jobs/{job_id}/cpa-download",
        "sub2api_filename": sub2api.name, "sub2api_path": str(sub2api),
        "sub2api_url": f"/api/jobs/{job_id}/sub2api-download",
    }


@dataclass
class Job:
    id: str
    email: str
    batch_id: str | None = None
    line: int | None = None
    output_dir: Path | None = field(default=None, repr=False)
    status: str = "queued"
    stage: str = "queued"
    message: str = "准备登录"
    events: list[dict[str, str]] = field(default_factory=list)
    challenge: dict[str, str] | None = None
    error: str | None = None
    result: dict[str, str] | None = None
    loop: Any = field(default=None, repr=False)
    task: Any = field(default=None, repr=False)
    code_future: Any = field(default=None, repr=False)
    cancel_requested: bool = False
    retry_after: int | None = field(default=None, repr=False)
    payload: dict | None = field(default=None, repr=False)
    payload_bytes: int = field(default=0, repr=False)
    finished_at: float | None = field(default=None, repr=False)

    def snapshot(self) -> dict:
        return {
            "id": self.id, "line": self.line, "email": self.email, "status": self.status,
            "stage": self.stage, "message": self.message,
            "events": [dict(event) for event in self.events],
            "challenge": dict(self.challenge) if self.challenge else None,
            "error": self.error, "result": dict(self.result) if self.result else None,
        }


class JobManager:
    def __init__(self, output_dir: Path | None, extractor: Callable, *, ephemeral: bool = False,
                 worker_slots=None, clock=time.monotonic) -> None:
        self.output_dir = output_dir
        self.extractor = extractor
        self.ephemeral = ephemeral
        self.worker_slots = worker_slots
        self.clock = clock
        self.retained_bytes = 0
        self.max_result_bytes = 16 * 1024 * 1024
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.current_id: str | None = None
        self.active_batch: str | None = None

    def _event(self, job: Job, stage: str, message: str) -> None:
        job.stage, job.message = stage, message
        job.events.append({"time": _now(), "message": message})
        job.events = job.events[-60:]

    def snapshot(self, job_id: str | None = None) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id or self.current_id)
            return job.snapshot() if job else None

    def start(self, credentials: dict) -> dict:
        with self.lock:
            if self.active_batch or any(job.status in ACTIVE for job in self.jobs.values()):
                raise ValueError("已有任务进行中，请先完成或取消当前任务")
            standalone_ids = [key for key, value in self.jobs.items() if not value.batch_id]
            for key in standalone_ids[:-11]:
                self.jobs.pop(key)
            job = Job(id=uuid.uuid4().hex, email=credentials["email"])
            self._event(job, "queued", "准备登录")
            self.jobs[job.id] = job
            self.current_id = job.id
            try:
                threading.Thread(target=self._run, args=(job, credentials), daemon=True).start()
            except BaseException:
                self.jobs.pop(job.id, None)
                self.current_id = next(reversed(self.jobs), None)
                credentials.clear()
                raise
            return job.snapshot()

    async def _ask_code(self, job: Job, kind: str) -> str:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self.lock:
            if job.cancel_requested:
                raise asyncio.CancelledError
            job.status = "awaiting_code"
            job.challenge = {"id": secrets.token_urlsafe(16), "kind": kind}
            job.code_future = future
            message = "请输入 Authenticator 当前动态码" if kind == "totp" else "账号还需要邮箱验证码，请查看邮箱后输入"
            self._event(job, "awaiting_code", message)
        try:
            return await future
        finally:
            with self.lock:
                job.challenge = None
                job.code_future = None
                if job.status == "awaiting_code":
                    job.status = "running"

    def submit_code(self, job_id: str, challenge_id: str, code: str) -> dict:
        with self.lock:
            job = self.jobs[job_id]
            if (job.cancel_requested or job.status != "awaiting_code" or not job.challenge
                    or not hmac.compare_digest(job.challenge["id"].encode(), challenge_id.encode())
                    or job.code_future is None):
                raise ValueError("当前验证码请求已失效，请等待新的验证提示")
            future = job.code_future
            job.challenge = None  # Consume once, before scheduling the async delivery.
            job.status = "running"
            self._event(job, "verifying_code", "正在验证动态码")

            def deliver() -> None:
                if not future.done():
                    future.set_result(code)

            job.loop.call_soon_threadsafe(deliver)
            return job.snapshot()

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs[job_id]
            if job.status in ACTIVE:
                job.cancel_requested = True
                self._event(job, "cancelling", "正在取消任务")
                if job.loop is not None and not job.loop.is_closed() and job.task is not None:
                    try:
                        job.loop.call_soon_threadsafe(job.task.cancel)
                    except RuntimeError:
                        # asyncio.run may close its loop just before its worker
                        # reacquires this lock to clear the loop/task references.
                        pass
            return job.snapshot()

    def _run(self, job: Job, credentials: dict, *, on_rate_limit: Callable[[int], None] | None = None) -> None:
        async def execute() -> None:
            with self.lock:
                job.loop = asyncio.get_running_loop()
                job.task = asyncio.current_task()
                if job.cancel_requested:
                    raise asyncio.CancelledError
                job.status = "running"

            def progress(stage: str, message: str) -> None:
                with self.lock:
                    if not job.cancel_requested:
                        self._event(job, stage, message)

            async def totp() -> str:
                return await self._ask_code(job, "totp")

            async def email_code(_email: str) -> str:
                return await self._ask_code(job, "email")

            kwargs = {**credentials, "otp_fetcher": email_code, "on_step": progress}
            if not credentials.get("totp_secret"):
                kwargs["totp_fetcher"] = totp
            result = await self.extractor(**kwargs)
            with self.lock:
                if job.cancel_requested:
                    raise asyncio.CancelledError
                if self.ephemeral:
                    raw = result.to_dict()
                    record = {"raw": raw, "cpa": build_cpa_record(raw),
                              "sub2api": build_sub2api_export([raw])}
                    size = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
                    if size > 512 * 1024 or self.retained_bytes + size > self.max_result_bytes:
                        raise ExtractionError("待交接结果超出内存额度，请先同步或下载已有结果后重试")
                    self.retained_bytes += size
                    job.payload, job.payload_bytes = record, size
                    job.result = {
                        "filename": f"tokens-{job.id}.json", "cpa_filename": f"codex-{job.id}.json",
                        "sub2api_filename": f"sub2api-{job.id}.json", "obtained_at": result.obtained_at,
                        "expires_at": result.expires_at, "conversion_status": "ready",
                        "delivery_status": "pending",
                    }
                    job.status = "success"
                    self._event(job, "success", "Token 已获取，等待交接到本浏览器缓存")
                    return
                output = (job.output_dir or self.output_dir) / f"tokens-{job.id}.json"
                save_tokens(result, output)
                self._event(job, "converting", "Token 已获取，正在自动生成 CPA 和 Sub2API 文件")
                generated = []
                try:
                    payload = result.to_dict()
                    documents = (
                        (output.parent / f"codex-{job.id}.json", build_cpa_record(payload)),
                        (output.parent / f"sub2api-{job.id}.json", build_sub2api_export([payload])),
                    )
                    for target, document in documents:
                        data = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                        generated.append(save_export(data, target))
                except Exception:
                    for path in generated:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            # A permission change must not hide the conversion
                            # failure or prevent publishing its safe error state.
                            pass
                    raise ExtractionError("Token 已获取，但 CPA / Sub2API 自动生成失败；原始结果已保存在输出目录，请检查目录权限") from None
                job.result = {
                    "filename": output.name, "path": str(output),
                    "download_url": f"/api/jobs/{job.id}/download",
                    "obtained_at": result.obtained_at, "expires_at": result.expires_at,
                    **converted_result(job.id, output.parent),
                }
                job.status = "success"
                self._event(job, "success", "Token 已保存，CPA 和 Sub2API 文件已自动生成")

        acquired = False
        try:
            if self.worker_slots is not None:
                while not acquired:
                    with self.lock:
                        if job.cancel_requested:
                            raise asyncio.CancelledError
                    acquired = self.worker_slots.acquire(timeout=.1)
            asyncio.run(execute())
        except asyncio.CancelledError:
            with self.lock:
                job.status = "cancelled"
                self._event(job, "cancelled", "任务已取消")
        except Exception as exc:
            # These classes contain deliberately safe messages in our extractor.
            message = str(exc) if isinstance(exc, (ExtractionError, ProtocolError)) else "任务失败，请检查网络连接和账号验证状态"
            with self.lock:
                job.status = "error"
                job.error = message
                if isinstance(exc, RateLimitedError):
                    job.retry_after = exc.retry_after
                    if on_rate_limit is not None:
                        on_rate_limit(exc.retry_after)
                self._event(job, "error", message)
        finally:
            if acquired:
                self.worker_slots.release()
            credentials.clear()
            with self.lock:
                job.finished_at = self.clock()
                job.challenge = None
                job.code_future = None
                job.loop = None
                job.task = None

    def acknowledge(self, job: Job) -> None:
        """Erase transient Token payload after the owning browser cached it."""
        with self.lock:
            self.retained_bytes = max(0, self.retained_bytes - job.payload_bytes)
            job.payload = None
            job.payload_bytes = 0
            if job.result:
                job.result["delivery_status"] = "delivered"


def _credentials(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请输入账号信息")
    if set(payload) - {"email", "password", "totp_secret", "proxy", "timeout"}:
        raise ValueError("请求包含不支持的字段")
    email, password = payload.get("email"), payload.get("password")
    if (not isinstance(email, str) or len(email) > 320
            or not re.fullmatch(r"[^\s@]+@[^\s@]+", email.strip())):
        raise ValueError("请输入有效的账号邮箱")
    if not isinstance(password, str) or not password:
        raise ValueError("账号密码不能为空")
    if len(password) > 4096:
        raise ValueError("账号密码长度超过上限")
    try:
        email.encode("utf-8")
        password.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("账号信息包含无效字符，请使用 UTF-8 文本") from None
    timeout = payload.get("timeout", 180)
    if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
            or not math.isfinite(timeout) or not 1 <= timeout <= 600):
        raise ValueError("超时须为 1 到 600 秒")
    values = {"email": email.strip(), "password": password, "timeout": timeout}
    for key in ("totp_secret", "proxy"):
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("2FA 密钥和代理地址必须为文本")
            if len(value) > (4096 if key == "totp_secret" else 2048):
                raise ValueError("2FA 密钥或代理地址长度超过上限")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("账号信息包含无效字符，请使用 UTF-8 文本") from None
            if value.strip():
                values[key] = value.strip()
    if "totp_secret" in values:
        values["totp_secret"] = normalize_totp_secret(values["totp_secret"])
    _validate_proxy(values.get("proxy"))
    return values


