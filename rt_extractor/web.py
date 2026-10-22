"""Loopback-only web interface for the standalone RT extractor."""
from __future__ import annotations

import argparse
import hmac
import json
from pathlib import Path
import re
import secrets
from typing import Callable
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, send_file

from .api import extract_tokens
from .batch import DEFAULT_CONCURRENCY, BatchInputError, BatchManager, parse_accounts
from .web_jobs import JobManager, _credentials

def create_app(output_dir: str | Path | None = None, extractor: Callable | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=3 * 1024 * 1024, RT_CSRF_TOKEN=secrets.token_urlsafe(32))
    directory = Path(output_dir or Path.cwd() / "rt-output").expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    manager = JobManager(directory, extractor or extract_tokens)
    app.extensions["rt_jobs"] = manager
    batches = BatchManager(manager)
    app.extensions["rt_batches"] = batches

    @app.before_request
    def guard():
        try:
            host = urlsplit("http://" + request.host)
            local = (host.hostname in {"127.0.0.1", "localhost", "::1"}
                     and not host.username and not host.password and not host.path
                     and (host.port is None or 0 < host.port <= 65535))
        except ValueError:
            local = False
        if not local or request.remote_addr not in {"127.0.0.1", "::1"}:
            return jsonify(error="此服务仅允许本机访问"), 403
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="请求来源不匹配"), 403
        if request.path.startswith("/api/") and not hmac.compare_digest(
            request.headers.get("X-RT-CSRF", "").encode(), app.config["RT_CSRF_TOKEN"].encode(),
        ):
            return jsonify(error="页面凭据已过期，请刷新页面"), 403

    @app.after_request
    def secure_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="输入内容过长"), 413

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=app.config["RT_CSRF_TOKEN"])

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.post("/api/jobs")
    def start():
        try:
            credentials = _credentials(request.get_json(silent=True))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        try:
            return jsonify(manager.start(credentials)), 202
        except ValueError as exc:
            credentials.clear()
            return jsonify(error=str(exc)), 409

    @app.get("/api/jobs/current")
    def current():
        return jsonify(job=manager.snapshot())

    @app.get("/api/jobs/<job_id>")
    def status(job_id: str):
        job = manager.snapshot(job_id)
        return jsonify(job) if job else (jsonify(error="任务不存在"), 404)

    @app.post("/api/jobs/<job_id>/code")
    def code(job_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="请输入验证码"), 400
        value, challenge_id = payload.get("code"), payload.get("challenge_id")
        if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value.strip())
                or not isinstance(challenge_id, str)):
            return jsonify(error="验证码须为 6 位数字"), 400
        try:
            return jsonify(manager.submit_code(job_id, challenge_id, value.strip()))
        except KeyError:
            return jsonify(error="任务不存在"), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 409

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel(job_id: str):
        try:
            return jsonify(manager.cancel(job_id))
        except KeyError:
            return jsonify(error="任务不存在"), 404

    @app.get("/api/jobs/<job_id>/download")
    def download(job_id: str):
        job = manager.snapshot(job_id)
        if job is None:
            return jsonify(error="任务不存在"), 404
        if job["status"] != "success":
            return jsonify(error="任务尚未成功完成"), 409
        output = Path(job["result"]["path"])
        if not output.is_file() or output.is_symlink():
            return jsonify(error="结果文件已被移走，请查看保存目录"), 404
        return send_file(output, as_attachment=True, download_name=output.name, mimetype="application/json")

    @app.get("/api/jobs/<job_id>/rt")
    def view_rt(job_id: str):
        job = manager.snapshot(job_id)
        if job is None:
            return jsonify(error="任务不存在"), 404
        if job["status"] != "success":
            return jsonify(error="任务尚未成功完成"), 409
        output = Path(job["result"]["path"])
        if not output.is_file() or output.is_symlink():
            return jsonify(error="结果文件已被移走，请查看保存目录"), 404
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return jsonify(error="结果文件无法读取，请检查已保存的 JSON"), 409
        value = payload.get("refresh_token") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value.strip():
            return jsonify(error="结果文件缺少有效 RT"), 409
        return jsonify(refresh_token=value)

    @app.post("/api/batches")
    def start_batch():
        payload = request.get_json(silent=True)
        try:
            accounts = parse_accounts(payload)
        except BatchInputError as exc:
            return jsonify(error=str(exc), errors=exc.errors), 400
        try:
            return jsonify(batches.start(accounts, concurrency=payload.get("concurrency", DEFAULT_CONCURRENCY))), 202
        except ValueError as exc:
            for _, credentials in accounts:
                credentials.clear()
            accounts.clear()
            return jsonify(error=str(exc)), 409
        except Exception:
            for _, credentials in accounts:
                credentials.clear()
            accounts.clear()
            return jsonify(error="批次无法启动，请检查输出目录权限"), 500

    @app.get("/api/batches/current")
    def current_batch():
        return jsonify(batch=batches.snapshot())

    @app.get("/api/batches/<batch_id>")
    def batch_status(batch_id: str):
        batch = batches.snapshot(batch_id)
        return jsonify(batch) if batch else (jsonify(error="批次不存在"), 404)

    @app.post("/api/batches/<batch_id>/cancel")
    def cancel_batch(batch_id: str):
        try:
            return jsonify(batches.cancel(batch_id))
        except KeyError:
            return jsonify(error="批次不存在"), 404

    def batch_download(batch_id: str, *, rt_only: bool = False, sub2api: bool = False):
        try:
            stream, name, mimetype = batches.export(batch_id, rt_only=rt_only, sub2api=sub2api)
        except KeyError:
            return jsonify(error="批次不存在"), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        return send_file(stream, as_attachment=True, download_name=name, mimetype=mimetype)

    @app.get("/api/batches/<batch_id>/download")
    def download_batch_zip(batch_id: str):
        return batch_download(batch_id)

    @app.get("/api/batches/<batch_id>/rt-download")
    def download_batch_rt(batch_id: str):
        return batch_download(batch_id, rt_only=True)

    @app.get("/api/batches/<batch_id>/sub2api-download")
    def download_batch_sub2api(batch_id: str):
        return batch_download(batch_id, sub2api=True)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="独立 RT 提取器本地网页")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "rt-output")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口须为 1 到 65535")
    app = create_app(args.output_dir)
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
