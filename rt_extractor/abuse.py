"""Bounded, process-local abuse controls for the single-worker web service."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import heapq
import hmac
import ipaddress
import math
import secrets
import threading
import time
from typing import Callable, Iterable


@dataclass(frozen=True)
class Quota:
    limit: int
    seconds: float

    def __post_init__(self):
        if (isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1
                or not math.isfinite(self.seconds) or self.seconds <= 0):
            raise ValueError("限流额度和时间窗口必须为正数")


@dataclass(frozen=True)
class ClientPolicy:
    """Additional budgets for an anonymous browser or short browser session."""
    requests: Quota = Quota(450, 60)
    burst: Quota = Quota(90, 1)
    static_requests: Quota = Quota(1200, 60)
    static_burst: Quota = Quota(240, 1)
    submissions: Quota = Quota(8, 60)
    verification: Quota = Quota(12, 60)
    exports: Quota = Quota(24, 60)
    control: Quota = Quota(60, 60)
    access_failures: Quota = Quota(10, 300)
    account_work: Quota = Quota(1500, 3600)


@dataclass(frozen=True)
class AbusePolicy:
    requests: Quota = Quota(600, 60)
    burst: Quota = Quota(120, 1)
    global_requests: Quota = Quota(2400, 60)
    global_burst: Quota = Quota(240, 1)
    static_requests: Quota = Quota(1200, 60)
    global_static_requests: Quota = Quota(4800, 60)
    static_burst: Quota = Quota(240, 1)
    global_static_burst: Quota = Quota(480, 1)
    submissions: Quota = Quota(10, 60)
    global_submissions: Quota = Quota(30, 60)
    account_attempts: Quota = Quota(5, 900)
    account_work: Quota = Quota(2000, 3600)
    global_account_work: Quota = Quota(5000, 3600)
    verification: Quota = Quota(15, 60)
    global_verification: Quota = Quota(60, 60)
    job_verification: Quota = Quota(5, 60)
    exports: Quota = Quota(30, 60)
    global_exports: Quota = Quota(120, 60)
    control: Quota = Quota(60, 60)
    global_control: Quota = Quota(240, 60)
    access_failures: Quota = Quota(10, 300)
    identity_issuance: Quota = Quota(20, 600)
    global_identity_issuance: Quota = Quota(60, 600)
    device: ClientPolicy = ClientPolicy()
    session: ClientPolicy = ClientPolicy(
        requests=Quota(300, 60), burst=Quota(60, 1), submissions=Quota(6, 60),
        verification=Quota(10, 60), exports=Quota(20, 60), account_work=Quota(1000, 3600),
    )
    max_entries: int = 10_000
    export_concurrency: int = 2

    def __post_init__(self):
        for value in (self.max_entries, self.export_concurrency):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("安全策略容量必须为正整数")


@dataclass(frozen=True)
class RequestIdentity:
    peer: str
    device: str
    session: str


class AbuseLimited(Exception):
    def __init__(self, retry_after: int, message: str = "请求过于频繁，请稍后重试。"):
        super().__init__(message)
        self.retry_after = max(1, math.ceil(retry_after))


@dataclass
class _Window:
    used: int
    reset_at: float


class Reservation:
    def __init__(self, limiter, debits):
        self._limiter, self._debits = limiter, debits

    def release(self):
        """Refund a failed admission without touching a replacement window."""
        with self._limiter.lock:
            debits, self._debits = self._debits, []
            for key, window, amount in debits:
                if self._limiter.windows.get(key) is window:
                    window.used = max(0, window.used - amount)


class WindowLimiter:
    """Atomic fixed windows anchored at first use; denied traffic never resets them.

    Active entries are never evicted to make room for attacker-chosen new keys.
    The expiry heap has exactly one item per retained window, including refunds.
    """
    def __init__(self, max_entries: int, clock: Callable[[], float] = time.monotonic):
        self.max_entries, self.clock = max_entries, clock
        self.lock = threading.RLock()
        self.windows: dict[tuple[str, str], _Window] = {}
        self._expiry = []
        self._serial = 0

    def reserve(self, charges, message="请求过于频繁，请稍后重试。") -> Reservation:
        combined = {}
        for scope, identity, quota, amount in charges:
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
                raise ValueError("限流计数必须为正整数")
            key = (scope, identity)
            previous = combined.get(key)
            if previous and previous[0] != quota:
                raise ValueError("同一限流键的策略必须一致")
            combined[key] = (quota, amount + (previous[1] if previous else 0))
        with self.lock:
            now = self.clock()
            while self._expiry and self._expiry[0][0] <= now:
                _end, _serial, key = heapq.heappop(self._expiry)
                self.windows.pop(key, None)
            retry = 0
            for key, (quota, amount) in combined.items():
                window = self.windows.get(key)
                if amount > quota.limit:
                    retry = max(retry, quota.seconds)
                elif window and window.used + amount > quota.limit:
                    retry = max(retry, window.reset_at - now)
            new_count = sum(key not in self.windows for key in combined)
            if len(self.windows) + new_count > self.max_entries:
                retry = max(retry, self._expiry[0][0] - now if self._expiry else 1)
            if retry > 0:
                raise AbuseLimited(retry, message)
            debits = []
            for key, (quota, amount) in combined.items():
                window = self.windows.get(key)
                if window is None:
                    window = self.windows[key] = _Window(0, now + quota.seconds)
                    self._serial += 1
                    heapq.heappush(self._expiry, (window.reset_at, self._serial, key))
                window.used += amount
                debits.append((key, window, amount))
            return Reservation(self, debits)


class ExportLease:
    def __init__(self, guard):
        self.guard, self.released = guard, False

    def release(self):
        with self.guard._export_lock:
            if not self.released:
                self.released = True
                self.guard._exports -= 1


class LeasedIterator:
    """Release on exhaustion, exception, or disconnect, including send_file passthrough."""
    def __init__(self, body: Iterable, lease: ExportLease):
        self.body, self.iterator, self.lease = body, iter(body), lease
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.iterator)
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                close = getattr(self.body, "close", None)
                if close:
                    close()
            finally:
                self.lease.release()


class AbuseGuard:
    def __init__(self, policy: AbusePolicy | None = None, clock=time.monotonic):
        self.policy = policy or AbusePolicy()
        self.limiter = WindowLimiter(self.policy.max_entries, clock)
        self._secret = secrets.token_bytes(32)
        self._export_lock = threading.Lock()
        self._exports = 0

    def _fingerprint(self, namespace: str, value: str) -> str:
        return hmac.new(self._secret, (namespace + ":" + value).encode(), hashlib.sha256).hexdigest()

    def peer(self, remote_addr: str | None) -> str:
        # Never accept X-Forwarded-For / Forwarded from an untrusted request.
        try:
            address = ipaddress.ip_address(remote_addr or "")
            identity = "loopback" if address.is_loopback else str(address)
        except ValueError:
            identity = "unknown"
        return self._fingerprint("peer", identity)

    def identity(self, remote_addr: str | None, device: str | None = None,
                 session: str | None = None) -> RequestIdentity:
        peer = self.peer(remote_addr)
        # Missing or invalid cookies share one stable anonymous budget per IP.
        # Valid server-issued identities intentionally survive an IP change.
        return RequestIdentity(
            peer,
            self._fingerprint("device" if device else "anonymous-device", device or peer),
            self._fingerprint("session" if session else "anonymous-session", session or peer),
        )

    def _client_charges(self, identity: str | RequestIdentity, scope: str,
                        quota_name: str, amount: int = 1):
        peer = identity.peer if isinstance(identity, RequestIdentity) else identity
        charges = [(scope, peer, getattr(self.policy, quota_name), amount)]
        if isinstance(identity, RequestIdentity):
            for dimension in ("device", "session"):
                quota = getattr(getattr(self.policy, dimension), quota_name)
                charges.append((scope + "-" + dimension, getattr(identity, dimension), quota, amount))
        return charges

    def check_request(self, identity: str | RequestIdentity, *, control=False, static=False):
        p = self.policy
        if control:
            charges = self._client_charges(identity, "control", "control")
            charges.append(("control-global", "*", p.global_control, 1))
        elif static:
            charges = self._client_charges(identity, "static", "static_requests")
            charges.extend(self._client_charges(identity, "static-burst", "static_burst"))
            charges.extend([("static-global", "*", p.global_static_requests, 1),
                            ("static-burst-global", "*", p.global_static_burst, 1)])
        else:
            charges = self._client_charges(identity, "requests", "requests")
            charges.extend(self._client_charges(identity, "burst", "burst"))
            charges.extend([("requests-global", "*", p.global_requests, 1),
                            ("burst-global", "*", p.global_burst, 1)])
        self.limiter.reserve(charges)

    def denied_access(self, identity: str | RequestIdentity):
        self.limiter.reserve(self._client_charges(identity, "access-failures", "access_failures"),
                             "访问校验失败次数过多，请稍后重试。")

    def check_identity_issuance(self, identity: str | RequestIdentity):
        peer = identity.peer if isinstance(identity, RequestIdentity) else identity
        self.limiter.reserve([
            ("identity-issuance", peer, self.policy.identity_issuance, 1),
            ("identity-issuance-global", "*", self.policy.global_identity_issuance, 1),
        ], "新建浏览器会话过于频繁，请稍后重试。")

    def check_action(self, identity: str | RequestIdentity, action: str, *, job_id: str | None = None):
        p = self.policy
        rules = {
            "submit": ("submissions", p.global_submissions, "任务提交过于频繁，请稍后重试。"),
            "verify": ("verification", p.global_verification, "验证码提交过于频繁，请稍后重试。"),
            "export": ("exports", p.global_exports, "结果读取或下载过于频繁，请稍后重试。"),
        }
        local, total, message = rules[action]
        charges = self._client_charges(identity, action, local)
        charges.append((action + "-global", "*", total, 1))
        if action == "verify" and job_id:
            charges.append(("verify-job", job_id, p.job_verification, 1))
        self.limiter.reserve(charges, message)

    def reserve_accounts(self, identity: str | RequestIdentity, emails: Iterable[str]) -> Reservation:
        accounts = list(emails)
        p = self.policy
        charges = self._client_charges(identity, "account-work", "account_work", len(accounts))
        charges.append(("account-work-global", "*", p.global_account_work, len(accounts)))
        charges.extend(("account", self._fingerprint("account", email.strip().casefold()),
                        p.account_attempts, 1) for email in accounts)
        return self.limiter.reserve(charges, "账号提取次数或账号处理总量已达上限，请稍后重试。")

    def acquire_export(self) -> ExportLease:
        with self._export_lock:
            if self._exports >= self.policy.export_concurrency:
                raise AbuseLimited(2, "同时读取或导出的请求过多，请稍后重试。")
            self._exports += 1
            return ExportLease(self)
