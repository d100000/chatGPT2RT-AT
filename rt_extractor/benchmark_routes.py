"""Benchmark API under the existing local-host, origin and session-CSRF guards."""
import json

from flask import Response, g, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

from .benchmark import ACTIVE, COOKIE, BenchmarkService, Conflict, StorageError
from .benchmark_stats import METHODS, csv_export, html_export
from .seo import metadata


def register_benchmark(app, *, directory=None, transport=None):
    service = BenchmarkService(directory, transport=transport)
    app.extensions['benchmark'] = service

    @app.before_request
    def benchmark_guard():
        relevant = request.path == '/benchmark' or request.path.startswith('/api/benchmark/') or request.path in {'/api/workspace', '/api/workspace/connection'}
        if not relevant or request.endpoint == 'clear_workspace':
            return
        if request.method in {'POST', 'PUT', 'DELETE'}:
            request.max_content_length = 128*1024
            if request.content_length is not None and request.content_length > 128*1024:
                raise RequestEntityTooLarge()
            if request.mimetype != 'application/json':
                return jsonify(error='需要 application/json Content-Type'), 400
        owner, cookie = service.identity(request.cookies.get(COOKIE), issue=request.path == '/benchmark' or request.endpoint == 'workspace_connection_save')
        g.benchmark_owner, g.benchmark_cookie = owner, cookie
        if not owner and request.path.startswith('/api/benchmark/'):
            return jsonify(error='请先打开或刷新并发分析页面以恢复历史凭据'), 403

    @app.after_request
    def benchmark_cookie(response):
        cookie = getattr(g, 'benchmark_cookie', None)
        if cookie and response.status_code < 400:
            response.set_cookie(COOKIE, cookie, max_age=365*86400, httponly=True, secure=request.is_secure, samesite='Strict', path='/')
        return response

    @app.errorhandler(StorageError)
    def benchmark_storage_error(error):
        return jsonify(error=str(error)), 507

    def api(action):
        try:
            return action()
        except Conflict as error:
            return jsonify(error=str(error)), 409
        except KeyError:
            return jsonify(error='批次不存在'), 404
        except (ValueError, TypeError) as error:
            return jsonify(error=str(error)), 400

    def data():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError('请求体必须为 JSON 对象')
        return payload

    @app.get('/benchmark')
    def benchmark_page():
        return render_template('benchmark.html', csrf_token=g.csrf_token, workspace_owner=g.workspace_owner,
                               methods=METHODS, seo=metadata(app.config['BOB_AI_PUBLIC_URL'], '/benchmark'))

    @app.post('/api/benchmark/models')
    def benchmark_models():
        return api(lambda: jsonify(service.models(g.benchmark_owner, data())))

    @app.post('/api/benchmark/start')
    def benchmark_start():
        return api(lambda: (jsonify(service.start(g.benchmark_owner, data())), 201))

    @app.post('/api/benchmark/stop')
    def benchmark_stop():
        def stop():
            payload = data()
            with service.lock:
                if payload.get('runId') and (not service.runtime or service.runtime['run']['runId'] != payload['runId']):
                    raise Conflict('当前后台批次已改变，请刷新状态后停止')
                return jsonify(service.stop(g.benchmark_owner))
        return api(stop)

    @app.get('/api/benchmark/state')
    def benchmark_state():
        return api(lambda: jsonify(service.state(g.benchmark_owner)))

    @app.get('/api/benchmark/runs')
    def benchmark_runs():
        return api(lambda: jsonify(runs=service.history(g.benchmark_owner)))

    @app.get('/api/benchmark/runs/<identifier>')
    def benchmark_run(identifier):
        return api(lambda: jsonify(service.get(g.benchmark_owner, identifier, full=True)))

    @app.get('/api/benchmark/runs/<identifier>/export')
    def benchmark_export(identifier):
        def export():
            format_name = request.args.get('format', 'html')
            if format_name not in {'json', 'csv', 'html'}:
                raise ValueError('导出格式须为 HTML、CSV 或 JSON')
            value = service.get(g.benchmark_owner, identifier, full=True)
            body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) if format_name == 'json' else csv_export(value) if format_name == 'csv' else html_export(value)
            response = Response(body, mimetype={'json': 'application/json', 'csv': 'text/csv', 'html': 'text/html'}[format_name])
            response.headers['Content-Disposition'] = f'attachment; filename="benchmark-{identifier}.{format_name}"'
            return response
        return api(export)

    @app.get('/api/workspace')
    def workspace_state():
        owner = g.benchmark_owner
        with service.lock:
            runtime = service.runtime
            active = runtime['run'] if runtime and runtime['run']['owner'] == owner else None
            benchmark = dict(runId=active['runId'], status=active['status']) if active else None
        workspace = getattr(g, 'workspace', None)
        extraction = 1 if workspace and workspace.active() else 0
        evaluation = 0
        pelican = app.extensions.get('pelican')
        if pelican and pelican._history and request.cookies.get('bob_pelican_history'):
            eval_owner, replacement = pelican._history.identity(request.cookies['bob_pelican_history'])
            if replacement is None:
                with pelican.lock:
                    evaluation = sum(r['owner'] == eval_owner and r['batch']['status'] in {'running', 'waiting', 'stopping'} for r in pelican.runtimes.values())
        return jsonify(connection=service.connection(owner), benchmark=benchmark, activeTasks=extraction+evaluation+int(bool(benchmark)))

    @app.post('/api/workspace/connection')
    def workspace_connection_save():
        return api(lambda: jsonify(connection=service.save_connection(g.benchmark_owner, data())))

    @app.delete('/api/workspace/connection')
    def workspace_connection_clear():
        return jsonify(connection=service.clear_connection(g.benchmark_owner))
