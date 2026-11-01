"""Independent OpenAI-compatible requests: no retries, repairs or model fallback."""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit, quote

import httpx

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_TEXT_TOKEN = re.compile(r'"(?:delta|content|text|output_text|reasoning_content|message)"\s*:\s*("(?:[^"\\]|\\.)*")')


def redact_fragmented_wire(value, keys):
    """Mask keys split across SSE deltas while preserving unrelated wire text."""
    if not keys or ('data:' not in value and not value.lstrip().startswith('{')):
        return value
    fragments, offset = [], 0
    for match in _TEXT_TOKEN.finditer(value):
        try:
            decoded = json.loads(match.group(1))
        except ValueError:
            continue
        fragments.append((match.start(1), match.end(1), decoded, offset))
        offset += len(decoded)
    joined = ''.join(f[2] for f in fragments)
    spans = []
    for key in keys:
        if not key:
            continue
        spans.extend((m.start(), m.end()) for m in re.finditer(re.escape(key), joined))
        # A cancelled stream may end part-way through an echoed credential.
        for length in range(min(len(key)-1, len(joined)), 5, -1):
            if joined.endswith(key[:length]):
                spans.append((len(joined)-length, len(joined)))
                break
    for start, end, decoded, offset in reversed(fragments):
        ranges = sorted({(max(a-offset, 0), min(b-offset, len(decoded))) for a, b in spans if a < offset+len(decoded) and b > offset}, reverse=True)
        for a, b in ranges:
            decoded = decoded[:a] + '[REDACTED]' + decoded[b:]
        if ranges:
            value = value[:start] + json.dumps(decoded, ensure_ascii=False) + value[end:]
    return value


def endpoint(value, protocol):
    if protocol not in {"responses", "chat"}:
        raise ValueError("协议必须为 Responses 或 Chat Completions")
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value) or "\\" in value:
        raise ValueError("请输入有效接口地址")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        _ = parsed.port
    except ValueError:
        raise ValueError("接口地址须为 HTTP(S)，不含用户名、密码、查询参数或片段；密钥请单独填写") from None
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/models"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if not path:
        path = "/v1"
    path += "/responses" if protocol == "responses" else "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def models_endpoint(value, protocol):
    target = endpoint(value, protocol)
    suffix = "/responses" if protocol == "responses" else "/chat/completions"
    return target[:-len(suffix)] + "/models"


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {redact(str(k), secrets): ("[REDACTED]" if str(k).lower() in {"api_key", "authorization", "x-api-key"} else redact(v, secrets)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    value = redact_fragmented_wire(value, secrets)
    for key in secrets:
        if key:
            for encoded in {key, quote(key, safe=""), json.dumps(key, ensure_ascii=True)[1:-1]}:
                value = value.replace(encoded, "[REDACTED]")
            for length in range(min(len(key)-1, len(value)), 5, -1):
                if value.endswith(key[:length]):
                    value = value[:-length] + "[REDACTED]"
                    break
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)


def body_for(config, model, prompt):
    body = {"model": model, "stream": True}
    effort = config["reasoning_effort"]
    if config["protocol"] == "responses":
        body.update(input=[{"role": "user", "content": prompt}], store=False,
                    max_output_tokens=config["max_output_tokens"])
        if effort:
            body["reasoning"] = {"effort": effort}
    else:
        body.update(messages=[{"role": "user", "content": prompt}],
                    max_completion_tokens=config["max_output_tokens"])
        if effort:
            body["reasoning_effort"] = effort
    return body


def visible_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") in {"text", "output_text"} and isinstance(x.get("text"), str))
    return ""


def response_text(payload):
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    return "".join(visible_content(x.get("content")) for x in payload.get("output", []) if isinstance(x, dict) and x.get("type") == "message")


def usage_from(value):
    value = value if isinstance(value, dict) else {}
    def number(*keys):
        for key in keys:
            x = value.get(key)
            if type(x) is int and x >= 0:
                return x
        return None
    return {"input": number("input_tokens", "prompt_tokens"), "output": number("output_tokens", "completion_tokens"), "total": number("total_tokens")}


async def discover_models(config, key, *, transport=None):
    async def run():
        async with httpx.AsyncClient(timeout=config["timeout_seconds"], follow_redirects=False, trust_env=False, transport=transport) as client:
            async with client.stream("GET", models_endpoint(config["base_url"], config["protocol"]), headers={"Authorization": f"Bearer {key}"}) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError("模型目录超过 2 MiB 限制")
                if not response.is_success:
                    raise ValueError(f"模型列表 HTTP {response.status_code}：{redact(data.decode(errors='replace')[:1000], (key,))}")
                payload = json.loads(data)
                models = payload.get("data", [])
                if not isinstance(models, list):
                    raise ValueError("模型目录格式无效，请手动填写模型 ID")
                return sorted({redact(x["id"], (key,)) for x in models if isinstance(x, dict) and isinstance(x.get("id"), str)})
    return await asyncio.wait_for(run(), min(config["timeout_seconds"], 60))


async def generate(config, key, body, record, checkpoint, *, transport=None):
    """Mutate one record, keeping partial output on cancellation or interruption.

    checkpoint is synchronous durable storage; exceptions must reach the scheduler.
    wire is decoded HTTP body, including SSE framing (no response headers/secrets).
    """
    started = time.monotonic()
    record.update(raw_output="", raw_wire="", returned_model=None, http_status=None,
                  finish_reason=None, ttft_ms=None, usage=usage_from(None), status="running", error=None,
                  response_format=None)
    ended = False
    wire_bytes = 0
    last_checkpoint = 0.0

    def checkpoint_now():
        nonlocal last_checkpoint
        now = time.monotonic()
        if now - last_checkpoint >= 1:
            checkpoint(record)
            last_checkpoint = now

    def meta(payload):
        if isinstance(payload.get("model"), str):
            record["returned_model"] = payload["model"]
        if isinstance(payload.get("usage"), dict):
            for name, value in usage_from(payload["usage"]).items():
                if value is not None:
                    record["usage"][name] = value

    def text_delta(text):
        nonlocal last_checkpoint
        if not isinstance(text, str):
            return
        if text.strip() and record["ttft_ms"] is None:
            record["ttft_ms"] = round((time.monotonic() - started) * 1000, 3)
            last_checkpoint = 0  # Journal the first visible partial immediately.
        record["raw_output"] += text

    def consume(payload, streaming):
        nonlocal ended
        if not isinstance(payload, dict):
            raise ValueError("响应必须是 JSON 对象")
        meta(payload)
        if payload.get("error"):
            raise ValueError("上游错误：" + json.dumps(payload["error"], ensure_ascii=False))
        if config["protocol"] == "chat":
            choices = payload.get("choices", [])
            if choices:
                # n is not sent. If the server supplies multiple choices, use index 0.
                choice = next((c for c in choices if c.get("index", 0) == 0), {})
                if streaming:
                    text_delta(visible_content(choice.get("delta", {}).get("content")))
                else:
                    record["raw_output"] = visible_content(choice.get("message", {}).get("content"))
                if choice.get("finish_reason"):
                    record["finish_reason"] = choice["finish_reason"]
                    ended = True
        elif streaming:
            kind = payload.get("type", "")
            response = payload.get("response", {})
            if isinstance(response, dict):
                meta(response)
            if kind == "response.output_text.delta":
                text_delta(payload.get("delta", ""))
            elif kind == "response.output_text.done" and not record["raw_output"]:
                text_delta(payload.get("text", ""))
            elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                ended = True
                if not record["raw_output"]:
                    record["raw_output"] = response_text(response)
                record["finish_reason"] = response.get("status") or kind.split(".")[-1]
                record["incomplete_details"] = response.get("incomplete_details")
                if response.get("error"):
                    record["error"] = json.dumps(response["error"], ensure_ascii=False)
            elif kind == "error":
                raise ValueError(str(payload.get("message", "流式错误")))
        else:
            record["raw_output"] = response_text(payload)
            record["finish_reason"] = payload.get("status")
            record["incomplete_details"] = payload.get("incomplete_details")
            ended = payload.get("status") in {"completed", "incomplete", "failed"}

    async def run():
        nonlocal wire_bytes
        async with httpx.AsyncClient(timeout=config["timeout_seconds"], follow_redirects=False, trust_env=False, transport=transport) as client:
            async with client.stream("POST", endpoint(config["base_url"], config["protocol"]),
                                     headers={"Authorization": f"Bearer {key}", "Accept": "text/event-stream"}, json=body) as response:
                record["http_status"] = response.status_code
                is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()
                record["response_format"] = "sse" if is_sse else "json"
                pending = ""
                event_data = []

                def line(value):
                    if not value:
                        if event_data:
                            data = "\n".join(event_data)
                            event_data.clear()
                            if data.strip() != "[DONE]":
                                consume(json.loads(data), True)
                    elif value.startswith("data:"):
                        event_data.append(value[5:].lstrip(" "))

                async for chunk in response.aiter_text():
                    wire_bytes += len(chunk.encode())
                    if wire_bytes > MAX_RESPONSE_BYTES:
                        raise ValueError("响应超过 16 MiB 安全限制；已保留收到的部分输出，未重试")
                    record["raw_wire"] += chunk
                    if response.is_success and is_sse:
                        pending += chunk
                        while "\n" in pending:
                            item, pending = pending.split("\n", 1)
                            line(item.rstrip("\r"))
                    checkpoint_now()
                if not response.is_success:
                    raise ValueError(f"HTTP {response.status_code}：{record['raw_wire'][:2000]}")
                if is_sse:
                    if pending:
                        line(pending.rstrip("\r"))
                    line("")
                else:
                    consume(json.loads(record["raw_wire"]), False)

    try:
        await asyncio.wait_for(run(), config["timeout_seconds"])
        if not ended:
            raise ValueError("响应未提供正常结束事件（可能流式中断）")
        if record["finish_reason"] not in {"stop", "completed"}:
            raise ValueError("输出未正常完成：" + str(record["finish_reason"]) + " " + str(record.get("incomplete_details") or ""))
        record["status"] = "success"
    except asyncio.CancelledError:
        record.update(status="cancelled", error="用户停止，已保留收到的部分输出")
    except (asyncio.TimeoutError, httpx.TimeoutException):
        record.update(status="timeout", error="达到单次总超时，未重试")
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        record.update(status="failed", error=str(error))
    finally:
        record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    return record
