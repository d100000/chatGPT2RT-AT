"""Bounded HTTP streaming; text, reasoning and header latency are independent."""
from __future__ import annotations

import asyncio
import codecs
from datetime import datetime, timezone
import json
import time
import unicodedata

import httpx

from .benchmark_config import SAFE_INTEGER, body_for, endpoint, redact, safe_preview, valid_model

MAX_BYTES = 8 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_number(value):
    return type(value) is int and 0 <= value <= SAFE_INTEGER


def merge_usage(record, value):
    if not isinstance(value, dict):
        return
    aliases = {'input': ('input_tokens', 'prompt_tokens'), 'output': ('output_tokens', 'completion_tokens'),
               'total': ('total_tokens',)}
    raw = record.setdefault('_usageRaw', {})
    for dest, names in aliases.items():
        for name in names:
            if name in value and value[name] is not None:
                raw[dest] = value[name]
                break
    for dest, containers, field in (
        ('cached', ('input_tokens_details', 'prompt_tokens_details'), 'cached_tokens'),
        ('reasoning', ('output_tokens_details', 'completion_tokens_details'), 'reasoning_tokens')):
        for name in containers:
            details = value.get(name)
            if isinstance(details, dict) and field in details and details[field] is not None:
                raw[dest] = details[field]
                break
    usage = {name: raw.get(name) if safe_number(raw.get(name)) else None for name in ('input', 'output', 'total', 'cached', 'reasoning')}
    if usage['total'] is None and usage['input'] is not None and usage['output'] is not None and safe_number(usage['input']+usage['output']):
        usage['total'] = usage['input'] + usage['output']
    record['usage'] = usage
    invalid = any(name in raw and not safe_number(raw[name]) for name in ('input', 'cached'))
    if invalid or (usage['input'] is not None and usage['cached'] is not None and usage['cached'] > usage['input']):
        record['cacheValid'] = 'invalid'
    elif usage['input'] is None or usage['cached'] is None:
        record['cacheValid'] = 'missing'
    else:
        record['cacheValid'] = 'valid'


class SSE:
    """Incremental UTF-8 + CR/LF lines, dispatch only on a complete event boundary."""
    def __init__(self, consume):
        self.decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
        self.pending = ''
        self.data = []
        self.event = ''
        self.consume = consume

    def feed(self, chunk, final=False):
        self.pending += self.decoder.decode(chunk, final=final)
        while True:
            positions = [i for i in (self.pending.find('\r'), self.pending.find('\n')) if i >= 0]
            if not positions:
                break
            i = min(positions)
            if self.pending[i] == '\r' and i+1 == len(self.pending) and not final:
                break
            size = 2 if self.pending[i:i+2] == '\r\n' else 1
            line, self.pending = self.pending[:i], self.pending[i+size:]
            if line == '':
                if self.data:
                    self.consume('\n'.join(self.data), self.event)
                self.data, self.event = [], ''
            elif not line.startswith(':'):
                name, _, value = line.partition(':')
                if value.startswith(' '):
                    value = value[1:]
                if name == 'data':
                    self.data.append(value)
                elif name == 'event':
                    self.event = value
        if final and self.pending.startswith(':'):
            self.pending = ''  # A final comment heartbeat has no data to dispatch.
        if final and (self.pending.strip() or self.data):
            raise ValueError('流提前断开：最后一个 SSE 事件没有结束边界')


def content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(x.get('text', '') for x in value if isinstance(x, dict) and x.get('type') in {'text', 'output_text'} and isinstance(x.get('text'), str))
    return ''


def response_text(value):
    output = value.get('output', [])
    if not isinstance(output, list):
        raise ValueError('Responses output 格式无效')
    if any(not isinstance(x, dict) or x.get('type') not in {'message', 'reasoning'} for x in output):
        raise ValueError('响应包含工具调用或不支持的输出类型')
    return value.get('output_text') if isinstance(value.get('output_text'), str) else ''.join(content_text(x.get('content')) for x in output if x.get('type') == 'message')


async def discover_models(url, protocol, key, transport=None):
    async def run():
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False, transport=transport) as client:
            async with client.stream('GET', endpoint(url, protocol, models=True), headers={'Authorization': f'Bearer {key}'} if key else {}) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError('模型列表正文超过 8 MiB')
                if not response.is_success:
                    raise ValueError(f'读取模型 HTTP {response.status_code}：{data.decode(errors="replace")[:800]}')
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError):
                    raise ValueError('模型接口返回非法 JSON') from None
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                    raise ValueError('模型接口缺少 JSON data 数组')
                return sorted({x['id'] for x in payload['data'] if isinstance(x, dict) and valid_model(x.get('id')) and redact(x['id'], key) == x['id']})
    try:
        return await asyncio.wait_for(run(), 30)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise ValueError('读取模型超过 30 秒，请检查连接') from None
    except (ValueError, httpx.HTTPError) as error:
        raise ValueError(safe_preview(str(error), key)) from None


async def generate(client, config, key, model, text, record):
    start = time.perf_counter()
    record.update(startedAt=now(), streamActual=False, firstTextMs=None, firstReasoningMs=None, ttfbMs=None,
                  durationMs=None, returnedModel=None, finishReason=None, statusCode=None, error=None,
                  usage={name: None for name in ('input', 'output', 'total', 'cached', 'reasoning')}, cacheValid='missing', outputPreview='')
    raw_preview = ''
    has_text = False
    completed = False
    done = False

    def elapsed():
        return round((time.perf_counter()-start)*1000, 3)

    def text_delta(value, *, reasoning=False, streaming=True):
        nonlocal raw_preview, has_text
        if not isinstance(value, str):
            return
        if any(not c.isspace() and unicodedata.category(c) not in {'Cc', 'Cf', 'Cs'} for c in value):
            name = 'firstReasoningMs' if reasoning else 'firstTextMs'
            if streaming and record[name] is None:
                record[name] = elapsed()
            if not reasoning:
                has_text = True
        if not reasoning:
            # Extra key-sized lookahead makes truncation/redaction safe across deltas.
            raw_preview = (raw_preview + value)[:2048 + max(8192, len(key)*6)]

    def meta(value):
        if isinstance(value.get('model'), str):
            record['returnedModel'] = safe_preview(value['model'], key)[:256]
        merge_usage(record, value.get('usage'))
        if value.get('error'):
            raise ValueError('上游错误：' + json.dumps(value['error'], ensure_ascii=False))

    def consume(payload, streaming, event=''):
        nonlocal completed
        if not isinstance(payload, dict):
            raise ValueError('响应事件须为 JSON 对象')
        meta(payload)
        if config['protocol'] == 'chat':
            choices = payload.get('choices', [])
            if not isinstance(choices, list) or any(not isinstance(x, dict) for x in choices):
                raise ValueError('Chat choices 格式无效')
            for choice in choices:
                message = choice.get('delta' if streaming else 'message', {})
                if not isinstance(message, dict):
                    raise ValueError('Chat 正文格式无效')
                if message.get('tool_calls') or message.get('function_call'):
                    raise ValueError('工具调用不能作为有效回答正文')
                text_delta(content_text(message.get('content')), streaming=streaming)
                text_delta(content_text(message.get('reasoning_content') or message.get('reasoning')), reasoning=True, streaming=streaming)
                finish = choice.get('finish_reason')
                if finish is not None:
                    record['finishReason'] = finish
                    if finish not in {'stop', 'length'}:
                        raise ValueError('Chat 非正常结束：' + str(finish))
                    completed = True
        elif streaming:
            kind = payload.get('type') or event
            response = payload.get('response', {})
            if not isinstance(response, dict):
                raise ValueError('Responses response 格式无效')
            meta(response)
            item = payload.get('item', {})
            if ('function_call' in kind or 'tool_call' in kind or (kind in {'response.output_item.added', 'response.output_item.done'} and isinstance(item, dict) and item.get('type') not in {'message', 'reasoning'})):
                raise ValueError('响应包含工具调用')
            if kind == 'response.output_text.delta':
                text_delta(payload.get('delta'))
            elif kind.startswith('response.reasoning') and kind.endswith('.delta'):
                text_delta(payload.get('delta'), reasoning=True)
            elif kind == 'response.completed':
                fallback = response_text(response)
                if not has_text:
                    text_delta(fallback, streaming=False)
                record['finishReason'] = response.get('status', 'completed')
                if record['finishReason'] != 'completed':
                    raise ValueError('Responses 未正常完成')
                completed = True
            elif kind in {'response.failed', 'response.incomplete', 'error', 'response.error'}:
                record['finishReason'] = kind
                raise ValueError('上游错误事件：' + json.dumps(payload, ensure_ascii=False)[:2000])
        else:
            text_delta(response_text(payload), streaming=False)
            record['finishReason'] = payload.get('status')
            completed = record['finishReason'] == 'completed'
            if not completed:
                raise ValueError('Responses JSON 未正常完成：' + str(record['finishReason']))

    def event(data, kind):
        nonlocal done
        if data.strip() == '[DONE]':
            done = True
            return
        if done:
            raise ValueError('[DONE] 后仍收到数据')
        try:
            payload = json.loads(data)
        except ValueError:
            raise ValueError('SSE 事件包含非法 JSON') from None
        consume(payload, True, kind)

    async def run():
        async with client.stream('POST', config['url'], json=body_for(config, model, text),
                                 headers={'Accept': 'text/event-stream', **({'Authorization': f'Bearer {key}'} if key else {})}) as response:
            record.update(ttfbMs=elapsed(), statusCode=response.status_code)
            is_sse = 'text/event-stream' in response.headers.get('content-type', '').lower() and response.is_success
            record['streamActual'] = is_sse
            parser = SSE(event)
            body, size = bytearray(), 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('单次响应正文超过 8 MiB')
                if is_sse:
                    parser.feed(chunk)
                else:
                    body.extend(chunk)
            if not response.is_success:
                raise ValueError(f'HTTP {response.status_code}：{body.decode(errors="replace")[:2000]}')
            if is_sse:
                parser.feed(b'', final=True)
            else:
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    raise ValueError('接口返回非法 JSON') from None
                consume(payload, False)
            if not completed or (is_sse and config['protocol'] == 'chat' and not done):
                raise ValueError('流提前断开或缺少合法结束状态 / [DONE]')
            if not has_text:
                raise ValueError('没有有效回答正文')

    try:
        await asyncio.wait_for(run(), config['timeoutSeconds'])
        record['status'] = 'success'
    except asyncio.CancelledError:
        record.update(status='cancelled', error='本地连接已取消；无法确认上游计算是否停止')
    except (asyncio.TimeoutError, httpx.TimeoutException):
        record.update(status='error', error='请求总超时（包括等待响应及读取正文），未自动重试')
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
        record.update(status='error', error=safe_preview(str(error) or type(error).__name__, key))
    finally:
        record.update(endedAt=now(), durationMs=elapsed(), outputPreview=safe_preview(raw_preview, key))
        record.pop('_usageRaw', None)
        # Sanitize returned model and all metadata, not just errors/output.
        clean = redact(record, key)
        record.clear()
        record.update(clean)
    return record
