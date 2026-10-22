"""Shared local web job execution; credentials are held only by the worker."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import math
from pathlib import Path
import re
import secrets
import threading
from typing import Any, Callable
import uuid

from .api import ExtractionError, _validate_proxy
from .storage import save_tokens
from ._protocol.login import ProtocolError, RateLimitedError
from ._protocol.totp import normalize_totp_secret

ACTIVE = {"queued", "running", "awaiting_code"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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

    def snapshot(self) -> dict:
        return {
            "id": self.id, "line": self.line, "email": self.email, "status": self.status,
            "stage": self.stage, "message": self.message,
            "events": [dict(event) for event in self.events],
            "challenge": dict(self.challenge) if self.challenge else None,
            "error": self.error, "result": dict(self.result) if self.result else None,
        }


class JobManager:
    def __init__(self, output_dir: Path, extractor: Callable) -> None:
        self.output_dir = output_dir
        self.extractor = extractor
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
            threading.Thread(target=self._run, args=(job, credentials), daemon=True).start()
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
                output = (job.output_dir or self.output_dir) / f"tokens-{job.id}.json"
                save_tokens(result, output)
                job.result = {
                    "filename": output.name, "path": str(output),
                    "download_url": f"/api/jobs/{job.id}/download",
                    "obtained_at": result.obtained_at, "expires_at": result.expires_at,
                }
                job.status = "success"
                self._event(job, "success", "AT、RT 和 ID Token 已完整保存")

        try:
            asyncio.run(execute())
        except asyncio.CancelledError:
            with self.lock:
                job.status = "cancelled"
                self._event(job, "cancelled", "任务已取消")
        except Exception as exc:
            # These classes contain deliberately safe messages in our extractor.
            message = str(exc) if isinstance(exc, (ExtractionError, ProtocolError)) else "任务失败，请检查网络连接和输出目录权限"
            with self.lock:
                job.status = "error"
                job.error = message
                if isinstance(exc, RateLimitedError):
                    job.retry_after = exc.retry_after
                    if on_rate_limit is not None:
                        on_rate_limit(exc.retry_after)
                self._event(job, "error", message)
        finally:
            credentials.clear()
            with self.lock:
                job.challenge = None
                job.code_future = None
                job.loop = None
                job.task = None


def _credentials(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请输入账号信息")
    if set(payload) - {"email", "password", "totp_secret", "proxy", "timeout"}:
        raise ValueError("请求包含不支持的字段")
    email, password = payload.get("email"), payload.get("password")
    if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", email.strip()):
        raise ValueError("请输入有效的账号邮箱")
    if not isinstance(password, str) or not password:
        raise ValueError("账号密码不能为空")
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
            if value.strip():
                values[key] = value.strip()
    if "totp_secret" in values:
        values["totp_secret"] = normalize_totp_secret(values["totp_secret"])
    _validate_proxy(values.get("proxy"))
    return values


