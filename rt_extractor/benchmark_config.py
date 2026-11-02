"""Validated benchmark plans and reproducible, model-independent test material."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit, urlunsplit

MAX_CALLS = 10_000
SAFE_INTEGER = 2**53 - 1
MODES = ('unique', 'shared-prefix')
INSTRUCTION = 'Read this load-test material. Reply only ACK followed by the request nonce. Do not summarize the material.'
WORDS = ('river forest window garden silver paper cloud ocean field stone morning quiet green bright '
         'table bridge hill water light road tree sand blue bird book spring rain path leaf glass').split()
DEFAULTS = dict(protocol='responses', concurrencyLevels=[1, 5, 10], requestsPerStage=20,
                inputMinTokens=1024, inputMaxTokens=2048, modes=list(MODES), maxOutputTokens=128,
                timeoutSeconds=120, reasoningEffort='', outputTokenField='max_completion_tokens')


def valid_model(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and bool(value.strip()) and not any(c in value for c in '\r\n\0')


def endpoint(value, protocol='responses', *, models=False):
    if protocol not in {'responses', 'chat'}:
        raise ValueError('协议须为 Responses 或 Chat Completions')
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() for c in value) or '\\' in value:
        raise ValueError('请输入有效的 HTTP(S) 接口 URL')
    try:
        p = urlsplit(value)
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username is not None or p.password is not None or '#' in value:
            raise ValueError()
        if p.port is not None and not 1 <= p.port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError('URL 须为 HTTP(S)，不得包含用户名、密码或片段') from None
    for name, _ in parse_qsl(p.query, keep_blank_values=True):
        name = re.sub('[^a-z0-9]', '', unquote(name).lower())
        if any(x in name for x in ('key', 'token', 'auth', 'secret', 'credential', 'signature', 'password')) or name in {'sig', 'jwt'}:
            raise ValueError('URL 查询参数不得携带鉴权信息，请在 API key 字段填写')
    path = p.path.rstrip('/')
    for suffix in ('/chat/completions', '/responses', '/response', '/models'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    path = (path or '/v1') + ('/models' if models else '/responses' if protocol == 'responses' else '/chat/completions')
    return urlunsplit((p.scheme, p.netloc, path, p.query, ''))


def origin(value):
    p = urlsplit(value)
    return (p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80))


def key_value(value):
    if not isinstance(value, str) or len(value) > 4096 or not value.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('API key 格式无效；不需要鉴权时可留空')
    return value


def secret_forms(key):
    if not key:
        return ()
    forms = {key, quote(key, safe=''), quote_plus(key, safe=''), json.dumps(key, ensure_ascii=True)[1:-1]}
    forms |= {re.sub(r'%[0-9A-F]{2}', lambda m: m[0].lower(), s) for s in forms}
    forms |= {json.dumps(s, ensure_ascii=True)[1:-1] for s in forms}
    return sorted(forms, key=len, reverse=True)


def redact(value, key=''):
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items() if k not in {'apiKey', 'Authorization'}}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if not isinstance(value, str):
        return value
    for secret in secret_forms(key):
        value = value.replace(secret, '[REDACTED]')
    return re.sub(r'(?i)\bBearer\s+[^\s"<>]+', 'Bearer [REDACTED]', value)


def safe_preview(value, key):
    value = redact(value, key)
    # A timeout or the preview limit can cut off an echoed key mid-delta.
    for secret in secret_forms(key):
        for length in range(min(len(secret) - 1, len(value)), 0, -1):
            if value.endswith(secret[:length]):
                value = value[:-length] + '[REDACTED]'
                break
    return value[:2048]


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name}须为 {low}–{high} 的整数')
    return value


def levels(value):
    if isinstance(value, str):
        parts = re.split(r'[,，\s]+', value.strip())
        if any(not re.fullmatch(r'[0-9]+', x) for x in parts):
            raise ValueError('并发档位仅接受整数，以逗号或空白分隔')
        value = [int(x) for x in parts]
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError('请输入 1–8 个并发档位')
    return sorted({integer(x, 1, 200, '并发档位') for x in value})


def plan(config):
    m, k, c, n = len(config['models']), len(config['modes']), config['concurrencyLevels'], config['requestsPerStage']
    measured = m * k * sum(max(n, x) for x in c)
    warmup = m * len(c) if 'shared-prefix' in config['modes'] else 0
    return dict(models=m, modes=k, levels=len(c), stages=m*k*len(c), measuredPlanned=measured,
                warmupPlanned=warmup, planned=measured+warmup)


def configuration(data):
    if not isinstance(data, dict):
        raise ValueError('需要 JSON 对象')
    config = {k: data.get(k, v) for k, v in DEFAULTS.items()}
    config['url'] = endpoint(data.get('url', ''), config['protocol'])
    models = data.get('models')
    if not isinstance(models, list) or not 1 <= len(models) <= 10 or not all(valid_model(x) for x in models):
        raise ValueError('请选择 1–10 个有效模型，ID 最长 256 字符，不允许换行或空字符')
    config['models'] = list(dict.fromkeys(models))
    config['concurrencyLevels'] = levels(config['concurrencyLevels'])
    for name, low, high, label in (
        ('requestsPerStage', 1, 200, '每档请求数'), ('inputMinTokens', 64, 16384, '输入下限'),
        ('inputMaxTokens', 64, 16384, '输入上限'), ('maxOutputTokens', 1, 16384, '最大输出 Token'),
        ('timeoutSeconds', 1, 600, '总超时秒数')):
        config[name] = integer(config[name], low, high, label)
    if config['inputMaxTokens'] < config['inputMinTokens']:
        raise ValueError('输入上限不得小于下限')
    modes = config['modes']
    if not isinstance(modes, list) or not modes or any(x not in MODES for x in modes):
        raise ValueError('至少选择一种有效输入模式')
    config['modes'] = [x for x in MODES if x in modes]
    if config['reasoningEffort'] not in {'', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh'}:
        raise ValueError('推理强度无效')
    if config['outputTokenField'] not in {'max_completion_tokens', 'max_tokens'}:
        raise ValueError('输出上限字段无效')
    if plan(config)['planned'] > MAX_CALLS:
        raise ValueError('总调用数超过 10,000，请减少模型、档位或请求数')
    key = key_value(data.get('apiKey', ''))
    if redact(config, key) != config:
        raise ValueError('URL 和模型 ID 等非敏感配置不得包含 API key')
    return config, key


def material(seed, config, mode, level_index, sample_index, warmup=False):
    identity = f'{seed}|{mode}|{level_index}|{sample_index}|{int(warmup)}'
    digest = hashlib.sha256(identity.encode()).digest()
    nonce = digest.hex()[:24]
    minimum, maximum = config['inputMinTokens'], config['inputMaxTokens']
    target = minimum + int.from_bytes(digest[12:20], 'big') % (maximum - minimum + 1)

    def words(label, count):
        raw = hashlib.shake_256(label.encode()).digest(count)
        return ' '.join(WORDS[x % len(WORDS)] for x in raw)

    if mode == 'unique':
        text = f'Request nonce: {nonce}\n{INSTRUCTION}\n' + words(identity, target)
    else:
        prefix = words(f'{seed}|shared-prefix|{level_index}', minimum)
        text = f'{INSTRUCTION}\n{prefix}\n' + words(identity, target-minimum) + f'\nRequest nonce: {nonce}'
    return text, dict(targetTokens=target, characters=len(text), sha256=hashlib.sha256(text.encode()).hexdigest(),
                      nonce=nonce, preview=text[:180])


def body_for(config, model, text):
    body = dict(model=model, stream=True)
    if config['protocol'] == 'responses':
        body.update(input=text, store=False, max_output_tokens=config['maxOutputTokens'])
        if config['reasoningEffort']:
            body['reasoning'] = {'effort': config['reasoningEffort']}
    else:
        body.update(messages=[{'role': 'user', 'content': text}], stream_options={'include_usage': True})
        body[config['outputTokenField']] = config['maxOutputTokens']
        if config['reasoningEffort']:
            body['reasoning_effort'] = config['reasoningEffort']
    return body
