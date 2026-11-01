"""Evaluation routes retain the application's local-only / CSRF access guards."""
import asyncio
import json
import sqlite3

from flask import Response, g, jsonify, render_template, request

from .eval_catalog import catalog
from .eval_protocol import discover_models
from .eval_reports import csv_export, html_export, review_summary
from .eval_svg import svg_hash
from .pelican import COOKIE, HistoryError, PelicanService, configuration
from .seo import metadata


def register_pelican(app, *, directory=None, transport=None):
    service = PelicanService(directory, transport=transport)
    app.extensions["pelican"] = service

    @app.before_request
    def pelican_owner():
        if request.path == "/pelican" or request.path.startswith("/api/pelican/"):
            cookie_value = request.cookies.get(COOKIE)
            if request.endpoint == "pelican_tasks" and request.method == "GET" and not cookie_value:
                return jsonify(tasks=[])
            # A fresh browser cannot list other browsers' persisted history.
            owner, cookie = service.history.identity(cookie_value)
            if cookie and request.path != "/pelican":
                # Only page navigation establishes history identity. A delayed
                # sidebar poll must never overwrite a newly issued page cookie.
                if request.endpoint == "pelican_tasks" and request.method == "GET":
                    return jsonify(tasks=[])
                return jsonify(error="评测历史凭据已过期，请打开或刷新 /pelican 页面"), 403
            g.pelican_owner = owner
            g.pelican_cookie = cookie

    @app.after_request
    def pelican_headers(response):
        cookie = getattr(g, "pelican_cookie", None)
        if cookie and response.status_code < 400:
            response.set_cookie(COOKIE, cookie, max_age=365 * 86400, httponly=True,
                                secure=request.is_secure, samesite="Strict", path="/")
        return response

    @app.errorhandler(HistoryError)
    def storage_error(error):
        return jsonify(error=str(error)), 507

    def api(action):
        try:
            return action()
        except KeyError:
            return jsonify(error="记录不存在"), 404
        except (ValueError, TypeError) as error:
            return jsonify(error=str(error)), 400
        except (sqlite3.Error, OSError):
            return jsonify(error="历史存储不可用，请检查磁盘空间和目录权限"), 507

    def data():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("需要 JSON 对象")
        return value

    def sample_view(record, detail=False):
        record = dict(record)
        record["score"] = review_summary(record)
        record["svg_sha256"] = svg_hash(record["svg"]) if record.get("svg") else None
        if not detail:
            for key in ("raw_output", "raw_wire", "request_body", "review_audit"):
                record.pop(key, None)
            if record.get("render_evidence"):
                record["render_evidence"] = {k: v for k, v in record["render_evidence"].items() if k != "data_url"}
        return record

    @app.get("/pelican")
    def pelican_page():
        return render_template("pelican.html", csrf_token=g.csrf_token,
                               workspace_owner=g.workspace_owner,
                               seo=metadata(app.config["BOB_AI_PUBLIC_URL"], "/pelican"))

    @app.get("/api/pelican/catalog")
    def pelican_catalog():
        return jsonify(questions=catalog())

    @app.post("/api/pelican/models")
    def pelican_models():
        def run():
            config, key = configuration(data())
            try:
                models = asyncio.run(discover_models(config, key, transport=service.transport))
                return jsonify(models=models)
            except Exception as error:
                # Do not expose transport exceptions that might embed headers.
                from .eval_protocol import redact
                return jsonify(error=redact(str(error) or "模型列表请求超时，请手动输入模型 ID", (key,))), 400
        return api(run)

    @app.get("/api/pelican/tasks")
    def pelican_tasks():
        return api(lambda: jsonify(tasks=service.tasks(g.pelican_owner)))

    @app.post("/api/pelican/tasks")
    def pelican_start():
        return api(lambda: (jsonify(batch=service.start(g.pelican_owner, data())), 201))

    @app.post("/api/pelican/tasks/<identifier>/stop")
    def pelican_stop(identifier):
        return api(lambda: jsonify(batch=service.stop(g.pelican_owner, identifier)))

    @app.get("/api/pelican/samples")
    def pelican_samples():
        def run():
            page = max(1, int(request.args.get("page", 1)))
            rows, total = service.history.sample_page(g.pelican_owner, request.args, page)
            return jsonify(samples=[sample_view(r) for r in rows], total=total, page=page, page_size=12)
        return api(run)

    @app.get("/api/pelican/samples/<identifier>")
    def pelican_sample(identifier):
        return api(lambda: jsonify(sample=sample_view(service.history.get("sample", g.pelican_owner, identifier), True)))

    @app.post("/api/pelican/samples/<identifier>/render")
    def pelican_render(identifier):
        return api(lambda: jsonify(sample=sample_view(service.render(g.pelican_owner, identifier, data()))))

    @app.post("/api/pelican/samples/<identifier>/reviews")
    def pelican_review(identifier):
        return api(lambda: jsonify(sample=sample_view(service.add_review(g.pelican_owner, identifier, data()))))

    @app.post("/api/pelican/samples/<identifier>/recheck")
    def pelican_recheck(identifier):
        return api(lambda: jsonify(sample=sample_view(service.recheck(g.pelican_owner, identifier, data()))))

    @app.post("/api/pelican/samples/<identifier>/ai-review")
    def pelican_ai_review(identifier):
        return api(lambda: jsonify(sample=sample_view(asyncio.run(service.ai_review(g.pelican_owner, identifier, data())))))

    @app.post("/api/pelican/blind")
    def pelican_blind():
        def run():
            value = data()
            return jsonify(service.blind(g.pelican_owner, value.get("reviewer"), value.get("filters", {})))
        return api(run)

    @app.post("/api/pelican/blind/<token>/<alias>")
    def pelican_blind_review(token, alias):
        def run():
            service.blind_review(g.pelican_owner, token, alias, data())
            return jsonify(saved=True)
        return api(run)

    @app.get("/api/pelican/report")
    def pelican_report():
        def run():
            result = service.export(g.pelican_owner, request.args)
            return jsonify(groups=result["groups"], notice=result["notice"])
        return api(run)

    def download(value, mimetype, filename):
        response = Response(value, mimetype=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @app.get("/api/pelican/export/<format_name>")
    def pelican_export(format_name):
        def run():
            result = service.export(g.pelican_owner, request.args)
            if format_name == "json":
                return download(json.dumps(result, ensure_ascii=False, indent=2), "application/json", "pelican-report.json")
            if format_name == "csv":
                return download(csv_export(result), "text/csv", "pelican-report.csv")
            if format_name == "html":
                return download(html_export(result), "text/html", "pelican-report.html")
            raise ValueError("导出格式不支持")
        return api(run)

    @app.get("/api/pelican/samples/<identifier>/svg")
    def pelican_svg(identifier):
        def run():
            record = service.history.get("sample", g.pelican_owner, identifier)
            if not record.get("svg"):
                raise ValueError("该样本没有完整 SVG；请在 JSON 中查看完整原始输出")
            # Always download as bytes, never execute an SVG document inline.
            return download(record["svg"], "application/octet-stream", f"pelican-{identifier}.svg")
        return api(run)
