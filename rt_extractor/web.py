"""Loopback-only web interface for the standalone RT extractor."""
from __future__ import annotations

import argparse
import hmac
import io
import json
from pathlib import Path
import re
import secrets
import time
from typing import Callable
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from .abuse import AbuseGuard, AbuseLimited, AbusePolicy, LeasedIterator
from .api import extract_tokens
from .batch import DEFAULT_CONCURRENCY, BatchInputError, BatchManager, parse_accounts
from .browser_identity import BrowserIdentities
from .cpa import build_cpa_record
from .sub2api import build_sub2api_export
from .web_jobs import ACTIVE, JobManager, _credentials

def create_app(output_dir: str | Path | None = None, extractor: Callable | None = None, *,
               abuse_policy: AbusePolicy | None = None, abuse_clock: Callable = time.monotonic) -> Flask:
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=3 * 1024 * 1024, MAX_FORM_MEMORY_SIZE=16 * 1024,
                      MAX_FORM_PARTS=10, RT_CSRF_TOKEN=secrets.token_urlsafe(32))
    abuse = AbuseGuard(abuse_policy, abuse_clock)
    app.extensions["rt_abuse"] = abuse
    browser_identities = BrowserIdentities()
    app.extensions["rt_browser_identities"] = browser_identities
    directory = Path(output_dir or Path.cwd() / "rt-output").expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    manager = JobManager(directory, extractor or extract_tokens)
    app.extensions["rt_jobs"] = manager
    batches = BatchManager(manager)
    app.extensions["rt_batches"] = batches

    exports = {"download", "view_rt", "download_cpa", "download_sub2api", "download_batch_zip",
               "download_batch_rt", "download_batch_sub2api", "download_batch_cpa", "batch_conversion_source"}

    def reject_access(message):
        abuse.denied_access(g.abuse_identity)
        return jsonify(error=message), 403

    def admit_task(emails, start):
        # Both managers share this RLock. Conflicts must not reserve account
        # attempts, and failed starts refund every account as one transaction.
        with manager.lock:
            if manager.active_batch or any(job.status in ACTIVE for job in manager.jobs.values()):
                raise ValueError("已有任务进行中，请先完成或取消当前任务")
            reservation = abuse.reserve_accounts(g.abuse_identity, emails)
            try:
                return start()
            except BaseException:
                reservation.release()
                raise

    @app.errorhandler(AbuseLimited)
    def too_frequent(error):
        response = jsonify(error=str(error), retry_after=error.retry_after)
        response.status_code = 429
        response.headers["Retry-After"] = str(error.retry_after)
        return response

    @app.before_request
    def guard():
        browser = browser_identities.resolve(request.cookies)
        g.abuse_identity = abuse.identity(request.remote_addr, browser.device, browser.session)
        abuse.check_request(g.abuse_identity, control=request.endpoint in {"cancel", "cancel_batch"},
                            static=request.endpoint == "static")
        try:
            host = urlsplit("http://" + request.host)
            local = (host.hostname in {"127.0.0.1", "localhost", "::1"}
                     and not host.username and not host.password and not host.path
                     and (host.port is None or 0 < host.port <= 65535))
        except ValueError:
            local = False
        if not local or request.remote_addr not in {"127.0.0.1", "::1"}:
            return reject_access("此服务仅允许本机访问")
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return reject_access("请求来源不匹配")
        supplied_csrf = request.headers.get("X-RT-CSRF", "")
        if request.path.startswith("/api/") and (len(supplied_csrf) > 128 or not hmac.compare_digest(
            supplied_csrf.encode(), app.config["RT_CSRF_TOKEN"].encode(),
        )):
            return reject_access("页面凭据已过期，请刷新页面")
        endpoint = request.endpoint
        if endpoint in {"start", "start_batch"}:
            abuse.check_action(g.abuse_identity, "submit")
        elif endpoint == "code":
            job_id = (request.view_args or {}).get("job_id")
            with manager.lock:
                known_job = job_id if job_id in manager.jobs else None
            abuse.check_action(g.abuse_identity, "verify", job_id=known_job)
        elif endpoint in exports:
            abuse.check_action(g.abuse_identity, "export")
            g.export_lease = abuse.acquire_export()
        limits = {"start": 16 * 1024, "code": 4 * 1024, "cancel": 1024, "cancel_batch": 1024}
        if endpoint in limits:
            request.max_content_length = limits[endpoint]
            if request.content_length is not None and request.content_length > limits[endpoint]:
                raise RequestEntityTooLarge()
        if (request.method == "GET" and endpoint in {"index", "converter", "network"}
                and (browser.device is None or browser.session is None)):
            abuse.check_identity_issuance(g.abuse_identity)
            g.browser_cookies = browser_identities.resolve(request.cookies, issue=True).cookies

    @app.after_request
    def secure_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self' https://ipok.io; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if response.status_code == 200:
            for cookie in getattr(g, "browser_cookies", ()):
                response.set_cookie(cookie.name, cookie.value, max_age=cookie.max_age,
                                    httponly=True, secure=request.is_secure, samesite="Lax", path="/")
            if getattr(g, "browser_cookies", ()):
                response.vary.add("Cookie")
        lease = getattr(g, "export_lease", None)
        if lease is not None:
            if 200 <= response.status_code < 300 and request.method != "HEAD":
                response.response = LeasedIterator(response.response, lease)
                g.export_lease_wrapped = True
            else:
                lease.release()
        return response

    @app.teardown_request
    def release_failed_export(_error):
        lease = getattr(g, "export_lease", None)
        if lease is not None and not getattr(g, "export_lease_wrapped", False):
            lease.release()

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="输入内容过长"), 413

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=app.config["RT_CSRF_TOKEN"])

    @app.get("/converter")
    def converter():
        return render_template("converter.html", csrf_token=app.config["RT_CSRF_TOKEN"])

    @app.get("/network")
    def network():
        return render_template("network.html", csrf_token=app.config["RT_CSRF_TOKEN"])

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
            return jsonify(admit_task([credentials["email"]], lambda: manager.start(credentials))), 202
        except AbuseLimited:
            credentials.clear()
            raise
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
                or not isinstance(challenge_id, str) or len(challenge_id) > 128):
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

    @app.get("/api/jobs/<job_id>/cpa-download")
    def download_cpa(job_id: str):
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
        try:
            record = build_cpa_record(payload)
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        stream = io.BytesIO((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return send_file(stream, as_attachment=True, download_name=f"codex-{job_id}.json", mimetype="application/json")

    @app.get("/api/jobs/<job_id>/sub2api-download")
    def download_sub2api(job_id: str):
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
        try:
            document = build_sub2api_export([payload])
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        stream = io.BytesIO((json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return send_file(stream, as_attachment=True, download_name=f"sub2api-{job_id}.json", mimetype="application/json")

    @app.post("/api/batches")
    def start_batch():
        payload = request.get_json(silent=True)
        try:
            accounts = parse_accounts(payload)
        except BatchInputError as exc:
            return jsonify(error=str(exc), errors=exc.errors), 400
        try:
            return jsonify(admit_task((data["email"] for _, data in accounts),
                           lambda: batches.start(accounts, concurrency=payload.get("concurrency", DEFAULT_CONCURRENCY)))), 202
        except AbuseLimited:
            for _, credentials in accounts:
                credentials.clear()
            accounts.clear()
            raise
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

    def batch_download(batch_id: str, *, rt_only: bool = False, sub2api: bool = False, cpa: bool = False):
        try:
            stream, name, mimetype = batches.export(batch_id, rt_only=rt_only, sub2api=sub2api, cpa=cpa)
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

    @app.get("/api/batches/<batch_id>/cpa-download")
    def download_batch_cpa(batch_id: str):
        return batch_download(batch_id, cpa=True)

    @app.get("/api/batches/<batch_id>/conversion-source")
    def batch_conversion_source(batch_id: str):
        try:
            return jsonify(batches.conversion_source(batch_id))
        except KeyError:
            return jsonify(error="批次不存在"), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 409

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
