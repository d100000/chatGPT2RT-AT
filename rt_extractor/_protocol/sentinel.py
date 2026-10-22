"""OpenAI Sentinel 令牌生成（PoW + VM + Turnstile + SO）。

对齐 wuzu-image 的 openai_register.py + sentinel_vm_py.py 实现：
  - SentinelTokenGenerator: FNV-1a PoW 求解
  - run_sentinel_vm: 字节码 VM → turnstile_result + so_token
  - build_sentinel_token: 编排全流程 → openai-sentinel-token + openai-sentinel-so-token

版本：20260219f9f6（2025-02-19 更新，含 session observer + turnstile）
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .pkce import new_device_id
from .errors import RateLimitedError, raise_for_rate_limit

logger = logging.getLogger(__name__)

# ── 版本与端点 ────────────────────────────────────────────────────
SENTINEL_VERSION = "20260219f9f6"
SENTINEL_REQUIREMENTS_SDK_URL = (
    "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
)
SENTINEL_CHALLENGE_SDK_URL = (
    f"https://sentinel.openai.com/sentinel/{SENTINEL_VERSION}/sdk.js"
)
SENTINEL_FRAME_REFERER = (
    f"https://sentinel.openai.com/backend-api/sentinel/frame.html"
    f"?sv={SENTINEL_VERSION}"
)

_SENTINEL_MAX_ATTEMPTS = 500_000
_SENTINEL_ERR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"
MIN_VM_TOKEN_LENGTH = 32

# ── 配置常量 ──────────────────────────────────────────────────────
_CORES = (4, 8, 12, 16)


# ── 结果容器 ──────────────────────────────────────────────────────

@dataclass(slots=True)
class SentinelResult:
    """sentinel_token() 的返回值，供调用方分别设到两个 header 上。"""
    token_json: str                # openai-sentinel-token
    so_token: Optional[str] = None  # openai-sentinel-so-token（可空）


# ── VM token 校验（与 sentinel_protocol.py 对齐）──────────────────

def _is_valid_vm_token(value: object) -> bool:
    token = str(value or "").strip()
    if len(token) < MIN_VM_TOKEN_LENGTH or token.isdigit():
        return False
    try:
        decoded = base64.b64decode(token, validate=True)
    except (ValueError, binascii.Error):
        return False
    prefix, separator, _ = decoded.partition(b":")
    if separator and prefix.isdigit():
        return False
    return len(decoded) >= 16


def _is_valid_session_observer_token(
    value: object,
    challenge_token: str,
    device_id: str,
    flow: str,
) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("c") == challenge_token
        and payload.get("id") == device_id
        and payload.get("flow") == flow
        and _is_valid_vm_token(payload.get("so"))
    )


def _normalize_vm_result(
    result: Any,
    challenge_token: str,
    device_id: str,
    flow: str,
) -> dict[str, str | None]:
    source = result if isinstance(result, dict) else {}
    turnstile_result = source.get("turnstile_result")
    so_token = source.get("so_token")
    return {
        "turnstile_result": (
            str(turnstile_result) if _is_valid_vm_token(turnstile_result) else None
        ),
        "so_token": (
            str(so_token)
            if _is_valid_session_observer_token(
                so_token, challenge_token, device_id, flow
            )
            else None
        ),
    }


def _vm_result_satisfies(
    result: dict[str, str | None], sentinel_data: dict
) -> bool:
    t_req = bool((sentinel_data.get("turnstile") or {}).get("required"))
    so_req = bool((sentinel_data.get("so") or {}).get("required"))
    return (
        (not t_req or bool(result.get("turnstile_result")))
        and (not so_req or bool(result.get("so_token")))
    )


# ── FNV-1a 32 + avalanche ────────────────────────────────────────

def _fnv1a_32(data: bytes) -> int:
    """FNV-1a 32-bit + murmur3 avalanche finalizer。"""
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 2246822507) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 3266489909) & 0xFFFFFFFF
    h ^= h >> 16
    return h & 0xFFFFFFFF


def _b64_compact(v: Any) -> str:
    raw = json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


# ── 主生成器 ──────────────────────────────────────────────────────

@dataclass(slots=True)
class SentinelGenerator:
    """一个 task 持有一个 generator；deviceID / sid 在生命周期内固定。"""

    device_id: str
    user_agent: str
    sid: str = field(default_factory=new_device_id)

    # ─ 配置数组（对齐 wuzu-image SentinelTokenGenerator._get_config）
    def _config_array(self, sdk_url: str) -> list[Any]:
        perf_now = random.uniform(1000.0, 50000.0)
        time_origin = time.time() * 1000 - perf_now
        return [
            1920 + 1080,                              # [0]  分辨率 sum
            time.strftime(
                "%a %b %d %Y %H:%M:%S GMT+0000 "
                "(Coordinated Universal Time)",
                time.gmtime(),
            ),                                         # [1]  GMT 时间串
            4294705152,                                # [2]  history.length
            random.random(),                           # [3]  计数/随机
            self.user_agent,                           # [4]  UA
            sdk_url,                                   # [5]  sentinel SDK URL
            None,                                      # [6]
            "en-US",                                   # [7]  language
            "en-US,en",                                # [8]  languages
            random.random(),                           # [9]  随机
            random.choice([                            # [10] navigator 属性
                "vendorSub-undefined", "plugins-undefined",
                "mimeTypes-undefined", "hardwareConcurrency-undefined",
            ]),
            random.choice([                            # [11] document 标志
                "location", "implementation", "URL",
                "documentURI", "compatMode",
            ]),
            random.choice([                            # [12] global 标志
                "Object", "Function", "Array",
                "Number", "parseFloat", "undefined",
            ]),
            perf_now,                                  # [13] perf_now
            self.sid,                                  # [14] sid
            "",                                        # [15]
            random.choice(_CORES),                     # [16] cores
            time_origin,                               # [17] timeOrigin
            0, 0, 1, 0, 0, 0, 0,                      # [18-24] padding
        ]

    def requirements_token(self) -> str:
        data = self._config_array(SENTINEL_REQUIREMENTS_SDK_URL)
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + _b64_compact(data) + "~S"

    def solve_pow(self, seed: str, difficulty: str) -> str:
        if not difficulty:
            difficulty = "0"
        start = time.monotonic()
        data = self._config_array(SENTINEL_CHALLENGE_SDK_URL)
        seed_b = seed.encode("ascii")
        dl = len(difficulty)
        for i in range(_SENTINEL_MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.monotonic() - start) * 1000)
            payload = _b64_compact(data)
            h = _fnv1a_32(seed_b + payload.encode("ascii"))
            hex_str = f"{h:08x}"
            if hex_str[: min(dl, 8)] <= difficulty[: min(dl, 8)]:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + _SENTINEL_ERR_PREFIX + _b64_compact(None) + "~S"

    # ─ VM 运行器 ──────────────────────────────────────────────────

    def _run_vm_sync(
        self,
        req_token: str,
        flow: str,
        sentinel_data: dict,
    ) -> dict[str, str | None]:
        """在同步上下文中运行 sentinel VM。"""
        try:
            from .sentinel_vm import run_sentinel_vm

            raw = run_sentinel_vm(
                req_token, self.device_id, flow, sentinel_data, self.user_agent
            )
            token = str(sentinel_data.get("token") or "")
            result = _normalize_vm_result(raw, token, self.device_id, flow)
            t_ok = bool(result.get("turnstile_result"))
            s_ok = bool(result.get("so_token"))
            logger.info(
                "[sentinel_vm] flow=%s turnstile=%s so=%s",
                flow, "OK" if t_ok else "EMPTY", "OK" if s_ok else "EMPTY",
            )
            return result
        except Exception as exc:
            logger.warning("[sentinel_vm] Python VM failed")
            return {"turnstile_result": None, "so_token": None}

    async def _run_vm(
        self,
        req_token: str,
        flow: str,
        sentinel_data: dict,
    ) -> dict[str, str | None]:
        """异步包装——在线程池中执行阻塞 VM。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._run_vm_sync, req_token, flow, sentinel_data
        )

    # ─ 完整 sentinel token 构建 ──────────────────────────────────

    async def sentinel_token(
        self, client: httpx.AsyncClient, flow: str
    ) -> SentinelResult:
        """完整 token：/sentinel/req → PoW → VM → 拼 JSON + SO token。

        失败重试一次；都失败时退化为 RequirementsToken 兜底。
        """
        try:
            return await self._build_sentinel(client, flow)
        except RateLimitedError:
            raise
        except Exception as e:
            logger.warning("sentinel/req failed; retrying")
            try:
                return await self._build_sentinel(client, flow)
            except RateLimitedError:
                raise
            except Exception as e2:
                logger.warning(
                    "sentinel/req failed twice; using fallback token"
                )
                return self._fallback_result(flow)

    async def _build_sentinel(
        self, client: httpx.AsyncClient, flow: str
    ) -> SentinelResult:
        """POST /sentinel/req → 解 PoW + 跑 VM → 组装结果。"""
        req_token = self.requirements_token()
        body = json.dumps(
            {"p": req_token, "id": self.device_id, "flow": flow},
            separators=(",", ":"),
        )
        headers = {
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": SENTINEL_FRAME_REFERER,
            "Origin": "https://sentinel.openai.com",
            "User-Agent": self.user_agent,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        resp = await client.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            content=body,
            headers=headers,
            follow_redirects=False,
        )
        raise_for_rate_limit(resp)
        if resp.status_code != 200:
            raise RuntimeError(
                f"sentinel/req HTTP {resp.status_code}"
            )
        data = resp.json()
        token = data.get("token") or ""
        if not token:
            raise RuntimeError(
                "sentinel/req response missing token"
            )

        # PoW
        pow_cfg = data.get("proofofwork") or {}
        if pow_cfg.get("required") and pow_cfg.get("seed"):
            p = self.solve_pow(
                str(pow_cfg["seed"]), str(pow_cfg.get("difficulty") or "")
            )
        else:
            p = self.requirements_token()

        # VM → turnstile + SO
        vm_result = await self._run_vm(req_token, flow, data)
        turnstile_val = vm_result.get("turnstile_result")
        so_token = vm_result.get("so_token")

        # 检查必要性
        turnstile_data = data.get("turnstile") or {}
        so_data = data.get("so") or {}
        if turnstile_data.get("required") and not turnstile_val:
            logger.warning(
                "[sentinel] turnstile required but VM produced empty for flow=%s",
                flow,
            )
        if so_data.get("required") and not so_token:
            logger.warning(
                "[sentinel] SO required but VM produced empty for flow=%s",
                flow,
            )

        token_json = json.dumps(
            {
                "p": p,
                "t": turnstile_val,
                "c": token,
                "id": self.device_id,
                "flow": flow,
            },
            separators=(",", ":"),
        )
        return SentinelResult(token_json=token_json, so_token=so_token)

    def _fallback_result(self, flow: str) -> SentinelResult:
        """sentinel.openai.com 完全不通时的兜底。"""
        token_json = json.dumps(
            {
                "p": self.requirements_token(),
                "t": None,
                "c": "",
                "id": self.device_id,
                "flow": flow,
            },
            separators=(",", ":"),
        )
        return SentinelResult(token_json=token_json, so_token=None)
