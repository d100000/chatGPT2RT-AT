"""One bounded background benchmark, isolated durable history and memory-only keys."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

import httpx
from itsdangerous import BadData, URLSafeTimedSerializer

from .benchmark_config import configuration, endpoint, key_value, material, origin, plan, redact, valid_model
from .benchmark_protocol import discover_models, generate, now
from .benchmark_stats import snapshot

COOKIE = 'bob_benchmark_history'
ACTIVE = {'running', 'stopping'}


class Conflict(ValueError):
    pass


class StorageError(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            os.chmod(temporary, 0o600)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError) as error:
        raise StorageError('历史保存失败，已停止派发；请检查磁盘空间及目录权限，内存结果仍可导出') from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class BenchmarkService:
    def __init__(self, directory=None, *, transport=None):
        self.directory = Path(directory or os.environ.get('GPT_BENCHMARK_DATA_DIR') or Path.home()/'.local/share/bob-ai-tools/benchmark')
        self.transport = transport
        self.lock = threading.RLock()
        self.runs = {}
        self.connections = {}
        self.catalogs = {}
        self.runtime = None
        self.signer = None
        self.cache = {}

    def initialize(self):
        with self.lock:
            if self.signer is not None:
                return
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                secret_path = self.directory / '.identity.key'
                if not secret_path.exists():
                    try:
                        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, 'w') as handle:
                            handle.write(secrets.token_hex(32))
                    except FileExistsError:
                        pass
                signer = URLSafeTimedSerializer(secret_path.read_text(), salt='benchmark-history-v1')
                loaded = {}
                for path in self.directory.glob('*/summary.json'):
                    value = json.loads(path.read_text())
                    if value.get('runId') != path.parent.name:
                        continue
                    value['records'] = sorted((json.loads(p.read_text()) for p in (path.parent/'records').glob('*.json')), key=lambda r: r['index'])
                    if value['status'] in ACTIVE:
                        value.update(status='interrupted', endedAt=now(), current=None, error='服务重启，未自动续测或重发；未确认完成的本地请求已标记取消')
                        for record in value['records']:
                            if record['status'] == 'running':
                                record.update(status='cancelled', endedAt=now(), durationMs=None, error='服务重启，无法确认上游结果，未自动重发')
                                atomic_json(path.parent/'records'/f"{record['id']}.json", record)
                        for stage in value['stages']:
                            if stage['status'] == 'running':
                                stage.update(status='interrupted', endedAt=now())
                        self.persist_summary(value)
                    loaded[value['runId']] = value
                self.runs = loaded
                self.signer = signer
            except (OSError, ValueError, KeyError) as error:
                raise StorageError('无法读取并发测试历史，请检查历史目录与 JSON 文件') from error

    def identity(self, cookie, *, issue=False):
        if not cookie and not issue:
            return None, None
        self.initialize()
        try:
            owner = self.signer.loads(cookie or '', max_age=365*86400)
            if isinstance(owner, str) and len(owner) == 64 and all(c in '0123456789abcdef' for c in owner):
                return owner, None
        except BadData:
            pass
        if not issue:
            return None, None
        owner = secrets.token_hex(32)
        return owner, self.signer.dumps(owner)

    def connection(self, owner):
        with self.lock:
            value = self.connections.get(owner, {})
            return dict(url=value.get('url', ''), protocol=value.get('protocol', 'responses'),
                        defaultModel=value.get('defaultModel', ''), hasKey=bool(value.get('apiKey')),
                        revision=value.get('revision', ''))

    def save_connection(self, owner, data):
        protocol = data.get('protocol', 'responses')
        url = endpoint(data.get('url', ''), protocol)
        key = key_value(data.get('apiKey', ''))
        default = data.get('defaultModel', '')
        if default and not valid_model(default):
            raise ValueError('默认模型 ID 无效')
        if redact([url, default], key) != [url, default]:
            raise ValueError('共享连接的非敏感字段不得包含密钥')
        with self.lock:
            self.connections[owner] = dict(url=url, protocol=protocol, defaultModel=default, apiKey=key, revision=uuid.uuid4().hex)
            self.catalogs.pop(owner, None)
        return self.connection(owner)

    def clear_connection(self, owner):
        with self.lock:
            self.connections.pop(owner, None)
            self.catalogs.pop(owner, None)
        return self.connection(owner)

    def resolve(self, owner, data):
        protocol = data.get('protocol', 'responses')
        url = endpoint(data.get('url', ''), protocol)
        manual = key_value(data.get('apiKey', ''))
        with self.lock:
            shared = self.connections.get(owner, {})
            key = manual or (shared.get('apiKey', '') if shared.get('url') and origin(url) == origin(shared['url']) else '')
        if redact(url, key) != url:
            raise ValueError('URL 不得包含 API key')
        fingerprint = hashlib.sha256(json.dumps([url, protocol, key]).encode()).hexdigest()
        return url, protocol, key, fingerprint

    def models(self, owner, data):
        url, protocol, key, fingerprint = self.resolve(owner, data)
        # A generation prevents an older, slow discovery from replacing a new one.
        generation = uuid.uuid4().hex
        with self.lock:
            self.catalogs[owner] = dict(generation=generation, fingerprint=fingerprint, models=None)
        models = asyncio.run(discover_models(url, protocol, key, self.transport))
        with self.lock:
            if self.catalogs.get(owner, {}).get('generation') == generation:
                self.catalogs[owner]['models'] = models
        return dict(models=models, url=url, protocol=protocol)

    def persist_summary(self, run):
        view = snapshot(run)
        view.pop('records', None)
        view['owner'] = run['owner']
        atomic_json(self.directory/run['runId']/'summary.json', view)
        run['_lastSaved'] = time.monotonic()

    def persist_record(self, run, record):
        atomic_json(self.directory/run['runId']/'records'/f"{record['id']}.json", record)

    def start(self, owner, data):
        config, _ = configuration(data)
        url, protocol, key, fingerprint = self.resolve(owner, data)
        if redact(config, key) != config:
            raise ValueError('配置不得包含共享密钥')
        self.initialize()
        with self.lock:
            if self.runtime is not None:
                raise Conflict('并发分析已有活动批次，请等待完成或停止当前批次')
            catalog = self.catalogs.get(owner, {})
            if catalog.get('fingerprint') != fingerprint or catalog.get('models') is None or not set(config['models']).issubset(catalog['models']):
                raise ValueError('模型列表与当前连接不一致，请重新读取模型后开始')
            identifier = uuid.uuid4().hex
            stages = []
            for model in config['models']:
                for mode in config['modes']:
                    for level_index, concurrency in enumerate(config['concurrencyLevels']):
                        stages.append(dict(index=len(stages), model=model, mode=mode, concurrency=concurrency,
                                           levelIndex=level_index, planned=max(config['requestsPerStage'], concurrency),
                                           status='pending', startedAt=None, endedAt=None, measurementMs=0,
                                           peakInFlight=0, warmupStatus=None))
            run = dict(runId=identifier, seed=secrets.token_hex(32), owner=owner, config=config,
                       status='running', startedAt=now(), endedAt=None, current=None, error=None,
                       stages=stages, records=[], plan=plan(config), durationMs=0, peakInFlight=0, _start=time.perf_counter())
            try:
                (self.directory/identifier/'records').mkdir(parents=True, mode=0o700)
                self.persist_summary(run)
            except OSError as error:
                raise StorageError('无法创建历史记录，未发起任何上游请求') from error
            self.runs[identifier] = run
            runtime = dict(run=run, stop=threading.Event(), loop=None, tasks=set(), key=key, storageFailed=False)
            self.runtime = runtime
            thread = threading.Thread(target=self.execute, args=(runtime,), name='benchmark-'+identifier[:8], daemon=True)
            runtime['thread'] = thread
            try:
                thread.start()
            except RuntimeError:
                self.runtime = None
                runtime.pop('key', None)
                run.update(status='error', endedAt=now(), error='无法启动后台任务')
                self.persist_summary(run)
                raise
            return snapshot(run)

    def stop(self, owner):
        with self.lock:
            runtime = self.runtime
            if runtime is None or runtime['run']['owner'] != owner:
                raise Conflict('当前浏览器没有正在运行的并发批次')
            run = runtime['run']
            run['status'] = 'stopping'
            runtime['stop'].set()
            self.cancel_connections(runtime)
            self.cache.clear()
            return dict(runId=run['runId'], status='stopping')

    def cancel_connections(self, runtime):
        loop = runtime.get('loop')
        if loop and not loop.is_closed():
            def cancel():
                for task in list(runtime['tasks']):
                    task.cancel()
            try:
                loop.call_soon_threadsafe(cancel)
            except RuntimeError:
                pass  # The event loop finished while a stop request arrived.

    def storage_failure(self, runtime):
        runtime['storageFailed'] = True
        runtime['run']['error'] = '执行期间历史保存失败，已停止派发并取消在途连接；可导出当前内存结果'
        runtime['stop'].set()
        self.cancel_connections(runtime)

    def execute(self, runtime):
        run = runtime['run']
        try:
            asyncio.run(self.run_stages(runtime))
        except BaseException as error:
            with self.lock:
                run['error'] = redact(str(error) or type(error).__name__, runtime.get('key', ''))
                run['status'] = 'error'
        finally:
            with self.lock:
                if runtime['storageFailed'] or run.get('error'):
                    run['status'] = 'error'
                elif runtime['stop'].is_set():
                    run['status'] = 'stopped'
                else:
                    run['status'] = 'completed'
                run.update(endedAt=now(), current=None, durationMs=(time.perf_counter()-run.pop('_start'))*1000)
                for stage in run['stages']:
                    if stage['status'] == 'running':
                        stage['status'] = 'interrupted' if run['status'] == 'error' else 'stopped'
                    if stage.get('_measurementStart') is not None:
                        stage['measurementMs'] = (time.perf_counter()-stage.pop('_measurementStart'))*1000
                try:
                    self.persist_summary(run)
                except StorageError:
                    run.update(status='error', error='历史摘要保存失败；内存结果仍可导出')
                runtime.pop('key', None)
                runtime['tasks'].clear()
                runtime['loop'] = None
                self.runtime = None
                self.cache.clear()

    async def run_stages(self, runtime):
        run, stop = runtime['run'], runtime['stop']
        runtime['loop'] = asyncio.get_running_loop()
        limits = httpx.Limits(max_connections=max(run['config']['concurrencyLevels']), max_keepalive_connections=200)
        async with httpx.AsyncClient(timeout=run['config']['timeoutSeconds'], follow_redirects=False,
                                     trust_env=False, transport=self.transport, limits=limits) as client:
            for stage in run['stages']:
                with self.lock:
                    if stop.is_set():
                        break
                    stage.update(status='running', startedAt=now())
                    run['current'] = dict(stageIndex=stage['index'], model=stage['model'], mode=stage['mode'], concurrency=stage['concurrency'])
                    try:
                        self.persist_summary(run)
                    except StorageError:
                        self.storage_failure(runtime)
                        break
                if stage['mode'] == 'shared-prefix':
                    task = asyncio.create_task(self.one(runtime, client, stage, -1, True))
                    runtime['tasks'].add(task)
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    finally:
                        runtime['tasks'].discard(task)
                with self.lock:
                    if stop.is_set():
                        stage.update(status='stopped', endedAt=now())
                        break
                    stage['_measurementStart'] = time.perf_counter()
                next_index = 0

                async def worker():
                    nonlocal next_index
                    while not stop.is_set() and next_index < stage['planned']:
                        sample = next_index
                        next_index += 1
                        await self.one(runtime, client, stage, sample, False)

                tasks = [asyncio.create_task(worker()) for _ in range(stage['concurrency'])]
                runtime['tasks'].update(tasks)
                results = await asyncio.gather(*tasks, return_exceptions=True)
                unexpected = [x for x in results if isinstance(x, BaseException) and not isinstance(x, asyncio.CancelledError)]
                if unexpected:
                    run['error'] = redact(str(unexpected[0]) or type(unexpected[0]).__name__, runtime['key'])
                    stop.set()
                runtime['tasks'].difference_update(tasks)
                with self.lock:
                    ended = stage.pop('_lastMeasurementEnd', time.perf_counter())
                    stage.update(measurementMs=(ended-stage.pop('_measurementStart'))*1000,
                                 status='stopped' if stop.is_set() else 'completed', endedAt=now())
                    try:
                        self.persist_summary(run)
                    except StorageError:
                        self.storage_failure(runtime)
                    self.cache.clear()

    async def one(self, runtime, client, stage, sample, warmup):
        run = runtime['run']
        text, input_meta = material(run['seed'], run['config'], stage['mode'], stage['levelIndex'], sample, warmup)
        with self.lock:
            if runtime['stop'].is_set():
                return
            record = dict(id=f"{len(run['records']):05d}", index=len(run['records']), sampleIndex=sample,
                          model=stage['model'], mode=stage['mode'], concurrency=stage['concurrency'], stageIndex=stage['index'],
                          warmup=warmup, status='running', startedAt=now(), endedAt=None, input=input_meta,
                          returnedModel=None, durationMs=None, firstTextMs=None, firstReasoningMs=None, ttfbMs=None,
                          streamActual=None, finishReason=None, statusCode=None, error=None,
                          usage={k: None for k in ('input', 'output', 'total', 'cached', 'reasoning')}, cacheValid='missing', outputPreview='')
            previous_peak = (run['peakInFlight'], stage['peakInFlight'])
            run['records'].append(record)
            in_flight = sum(r['status'] == 'running' for r in run['records'])
            run['peakInFlight'] = max(run['peakInFlight'], in_flight)
            stage['peakInFlight'] = max(stage['peakInFlight'], in_flight)
            try:
                self.persist_record(run, record)
            except StorageError:
                # No upstream request was made: leave this sample unstarted.
                run['records'].pop()
                run['peakInFlight'], stage['peakInFlight'] = previous_peak
                self.storage_failure(runtime)
                return
        local = deepcopy(record)
        try:
            await generate(client, run['config'], runtime['key'], stage['model'], text, local)
        except asyncio.CancelledError:
            local.update(status='cancelled', endedAt=now(), error='本地连接取消，无法确认上游是否停止')
        except Exception as error:
            local.update(status='error', endedAt=now(), error=redact(str(error), runtime['key']))
        ended = time.perf_counter()
        with self.lock:
            record.update(local)
            if not warmup:
                stage['_lastMeasurementEnd'] = ended
            if warmup:
                stage['warmupStatus'] = record['status']
            try:
                self.persist_record(run, record)
                if time.monotonic()-run.get('_lastSaved', 0) >= 1:
                    self.persist_summary(run)
            except StorageError:
                self.storage_failure(runtime)

    def get(self, owner, identifier, *, full=False):
        self.initialize()
        with self.lock:
            run = self.runs.get(identifier)
            if not run or run['owner'] != owner:
                raise KeyError(identifier)
            if full:
                return snapshot(run, full=True)
            cached = self.cache.get(identifier)
            if cached and time.monotonic()-cached[0] < 1:
                return deepcopy(cached[1])
            value = snapshot(run)
            self.cache[identifier] = time.monotonic(), value
            return deepcopy(value)

    def state(self, owner):
        if not owner:
            return dict(status='idle', runId=None)
        self.initialize()
        with self.lock:
            owned = [r for r in self.runs.values() if r['owner'] == owner]
            if not owned:
                return dict(status='idle', runId=None)
            run = max(owned, key=lambda r: r['startedAt'])
            return self.get(owner, run['runId'])

    def history(self, owner):
        if not owner:
            return []
        self.initialize()
        with self.lock:
            runs = sorted((r for r in self.runs.values() if r['owner'] == owner), key=lambda r: r['startedAt'], reverse=True)[:50]
            return [dict(runId=r['runId'], startedAt=r['startedAt'], status=r['status'], models=r['config']['models']) for r in runs]
