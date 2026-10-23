"""Loopback-only web interface for the standalone RT extractor."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import time
from typing import Callable
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

from .abuse import AbuseGuard, AbuseLimited, AbusePolicy, LeasedIterator
from .api import extract_tokens
from .batch import DEFAULT_CONCURRENCY, BatchInputError, parse_accounts
from .browser_identity import BrowserIdentities
from .seo import (DOC_PAGES, DOCS_BY_SLUG, REPOSITORY_URL, llms_text, metadata,
                  robots_text, sitemap_xml, structured_data, validate_public_url)
from .web_jobs import ACTIVE, _credentials
from .workspace_registry import WorkspaceRegistry

def create_app(output_dir: str | Path | None = None, extractor: Callable | None = None, *,
               abuse_policy: AbusePolicy | None = None, abuse_clock: Callable = time.monotonic) -> Flask:
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=3 * 1024 * 1024, MAX_FORM_MEMORY_SIZE=16 * 1024,
                      MAX_FORM_PARTS=10, RT_CSRF_TOKEN=secrets.token_urlsafe(32),
                      BOB_AI_PUBLIC_URL=validate_public_url(os.environ.get("BOB_AI_PUBLIC_URL", "")))
    abuse = AbuseGuard(abuse_policy, abuse_clock)
    app.extensions["rt_abuse"] = abuse
    browser_identities = BrowserIdentities()
    app.extensions["rt_browser_identities"] = browser_identities
    # Kept as a compatibility argument only. Web authentication never opens,
    # restores, creates, scans or writes an output directory.
    registry = WorkspaceRegistry(extractor or extract_tokens, clock=abuse_clock)
    app.extensions["rt_workspaces"] = registry

    exports = {"download", "view_rt", "download_cpa", "download_sub2api", "download_batch_zip",
               "download_batch_rt", "download_batch_sub2api", "download_batch_cpa", "batch_conversion_source",
               "batch_results", "job_result"}

    def session_value(purpose, session):
        return hmac.new(app.config["RT_CSRF_TOKEN"].encode(),
                        f"{purpose}:{session}".encode(), hashlib.sha256).hexdigest()

    def reject_access(message):
        abuse.denied_access(g.abuse_identity)
        return jsonify(error=message), 403

    def admit_task(emails, start):
        # Admission must stay registered until the task starts. Otherwise a
        # concurrent clear/expiry could detach this workspace before start(),
        # leaving credentials and results outside cancellation/TTL management.
        # Always take registry -> manager locks, as cleanup does.
        with registry.lock:
            workspace = registry.ensure(g.workspace_owner)
            manager = workspace.jobs
            try:
                with manager.lock:
                    if manager.active_batch or any(job.status in ACTIVE for job in manager.jobs.values()):
                        raise ValueError("已有任务进行中，请先完成或取消当前任务")
                    if any(job.payload is not None for job in manager.jobs.values()):
                        raise ValueError("请先将上次结果同步到本浏览器后再开始新任务")
                    reservation = abuse.reserve_accounts(g.abuse_identity, emails)
                    try:
                        return start(workspace)
                    except BaseException:
                        reservation.release()
                        raise
            except BaseException:
                registry.release_empty(g.workspace_owner, expected=workspace)
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
        abuse.check_request(g.abuse_identity, control=request.endpoint in {
            "cancel", "cancel_batch", "ack_batch", "ack_job", "clear_workspace"},
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
        endpoint = request.endpoint
        if (request.method == "GET" and endpoint in {"index", "converter", "network"}
                and (browser.device is None or browser.session is None)):
            abuse.check_identity_issuance(g.abuse_identity)
            browser = browser_identities.resolve(request.cookies, issue=True)
            g.browser_cookies = browser.cookies
        if browser.session is not None:
            g.workspace_owner = session_value("workspace", browser.session)
            g.csrf_token = session_value("csrf", browser.session)
        if request.path.startswith("/api/"):
            supplied_csrf = request.headers.get("X-RT-CSRF", "")
            if (browser.session is None or len(supplied_csrf) > 128 or not hmac.compare_digest(
                    supplied_csrf.encode(), g.csrf_token.encode())):
                return reject_access("页面凭据已过期，请刷新页面")
            g.workspace = registry.get(g.workspace_owner)
        if endpoint in {"start", "start_batch"}:
            abuse.check_action(g.abuse_identity, "submit")
        elif endpoint == "code":
            job_id = (request.view_args or {}).get("job_id")
            workspace = g.workspace
            if workspace is not None:
                with workspace.jobs.lock:
                    known_job = job_id if job_id in workspace.jobs.jobs else None
            else:
                known_job = None
            abuse.check_action(g.abuse_identity, "verify", job_id=known_job)
        elif endpoint in exports:
            abuse.check_action(g.abuse_identity, "export")
            g.export_lease = abuse.acquire_export()
        limits = {"start": 16 * 1024, "code": 4 * 1024, "cancel": 1024, "cancel_batch": 1024,
                  "ack_batch": 96 * 1024, "ack_job": 4 * 1024, "clear_workspace": 1024}
        if endpoint in limits:
            request.max_content_length = limits[endpoint]
            if request.content_length is not None and request.content_length > limits[endpoint]:
                raise RequestEntityTooLarge()

    @app.after_request
    def secure_headers(response):
        private = request.path.startswith("/api/") or request.endpoint in {"index", "converter", "network"}
        response.headers["Cache-Control"] = "private, no-store, max-age=0" if private else "no-store"
        if private:
            response.vary.add("Cookie")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        nonce = getattr(g, "structured_data_nonce", None)
        script_policy = "'self'" + (f" 'nonce-{nonce}'" if nonce and response.status_code == 200 else "")
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src {script_policy}; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self' https://ipok.io; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if request.endpoint == "docs" and response.status_code == 200:
            response.headers["X-Robots-Tag"] = (
                "index, follow" if app.config["BOB_AI_PUBLIC_URL"] else "noindex, follow"
            )
        elif (app.config["BOB_AI_PUBLIC_URL"] and request.endpoint == "static"
              and (request.view_args or {}).get("filename") in {"social-preview.png", "social-preview.svg"}
              and response.status_code == 200):
            response.headers["X-Robots-Tag"] = "index, follow"
        elif request.endpoint in {"index", "converter", "network"} and response.status_code == 200:
            response.headers["X-Robots-Tag"] = "noindex, follow, noarchive"
        else:
            response.headers["X-Robots-Tag"] = "noindex, nofollow, nosnippet, noarchive"
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
        return render_template("index.html", csrf_token=getattr(g, "csrf_token", ""),
                               workspace_owner=getattr(g, "workspace_owner", ""),
                               seo=metadata(app.config["BOB_AI_PUBLIC_URL"], "/"))

    @app.get("/converter")
    def converter():
        return render_template("converter.html", csrf_token=getattr(g, "csrf_token", ""),
                               workspace_owner=getattr(g, "workspace_owner", ""),
                               seo=metadata(app.config["BOB_AI_PUBLIC_URL"], "/converter"))

    @app.get("/network")
    def network():
        return render_template("network.html", csrf_token=getattr(g, "csrf_token", ""),
                               workspace_owner=getattr(g, "workspace_owner", ""),
                               seo=metadata(app.config["BOB_AI_PUBLIC_URL"], "/network"))

    @app.get("/docs", defaults={"slug": ""})
    @app.get("/docs/<slug>")
    def docs(slug):
        page = DOCS_BY_SLUG.get(slug)
        if page is None:
            abort(404)
        public_url = app.config["BOB_AI_PUBLIC_URL"]
        g.structured_data_nonce = secrets.token_urlsafe(24)
        return render_template(
            "docs.html", page=page, doc_pages=DOC_PAGES, repository_url=REPOSITORY_URL,
            seo=metadata(public_url, page.path, page=page),
            structured_data=structured_data(public_url, page),
            structured_nonce=g.structured_data_nonce,
        )

    @app.get("/robots.txt")
    def robots():
        return Response(robots_text(app.config["BOB_AI_PUBLIC_URL"]), mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        return Response(sitemap_xml(app.config["BOB_AI_PUBLIC_URL"]), mimetype="application/xml")

    @app.get("/llms.txt")
    def llms():
        return Response(llms_text(app.config["BOB_AI_PUBLIC_URL"]), mimetype="text/plain")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    def missing(kind="任务"):
        return jsonify(error=f"{kind}不存在"), 404

    def owned_job(job_id):
        workspace = g.workspace
        if workspace is None:
            return None
        with workspace.jobs.lock:
            return workspace.jobs.jobs.get(job_id)

    def owned_batch(batch_id):
        workspace = g.workspace
        if workspace is None:
            return None
        with workspace.jobs.lock:
            return workspace.batches.batches.get(batch_id)

    def clear_accounts(accounts):
        for _, credentials in accounts:
            credentials.clear()
        accounts.clear()

    @app.post("/api/jobs")
    def start():
        try:
            credentials = _credentials(request.get_json(silent=True))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        try:
            result = admit_task([credentials["email"]], lambda workspace: workspace.jobs.start(credentials))
            return jsonify(result), 202
        except AbuseLimited:
            credentials.clear()
            raise
        except ValueError as exc:
            credentials.clear()
            return jsonify(error=str(exc)), 409
        except Exception:
            credentials.clear()
            return jsonify(error="任务无法启动，请稍后重试"), 500

    @app.get("/api/jobs/current")
    def current():
        return jsonify(job=g.workspace.jobs.snapshot() if g.workspace else None)

    @app.get("/api/jobs/<job_id>")
    def status(job_id: str):
        job = g.workspace.jobs.snapshot(job_id) if g.workspace else None
        return jsonify(job) if job else missing()

    @app.post("/api/jobs/<job_id>/code")
    def code(job_id: str):
        if owned_job(job_id) is None:
            return missing()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="请输入验证码"), 400
        value, challenge_id = payload.get("code"), payload.get("challenge_id")
        if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value.strip())
                or not isinstance(challenge_id, str) or len(challenge_id) > 128):
            return jsonify(error="验证码须为 6 位数字"), 400
        try:
            return jsonify(g.workspace.jobs.submit_code(job_id, challenge_id, value.strip()))
        except KeyError:
            return missing()
        except ValueError as exc:
            return jsonify(error=str(exc)), 409

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel(job_id: str):
        if g.workspace is None:
            return missing()
        try:
            return jsonify(g.workspace.jobs.cancel(job_id))
        except KeyError:
            return missing()

    @app.post("/api/batches")
    def start_batch():
        payload = request.get_json(silent=True)
        try:
            accounts = parse_accounts(payload)
        except BatchInputError as exc:
            return jsonify(error=str(exc), errors=exc.errors), 400
        try:
            result = admit_task((data["email"] for _, data in accounts), lambda workspace:
                                workspace.batches.start(accounts, concurrency=payload.get("concurrency", DEFAULT_CONCURRENCY)))
            return jsonify(result), 202
        except AbuseLimited:
            clear_accounts(accounts)
            raise
        except ValueError as exc:
            clear_accounts(accounts)
            return jsonify(error=str(exc)), 409
        except Exception:
            clear_accounts(accounts)
            return jsonify(error="批次无法启动，请稍后重试"), 500

    @app.get("/api/batches/current")
    def current_batch():
        return jsonify(batch=g.workspace.batches.snapshot() if g.workspace else None)

    @app.get("/api/batches/<batch_id>")
    def batch_status(batch_id: str):
        batch = g.workspace.batches.snapshot(batch_id) if g.workspace else None
        return jsonify(batch) if batch else missing("批次")

    @app.post("/api/batches/<batch_id>/cancel")
    def cancel_batch(batch_id: str):
        if g.workspace is None:
            return missing("批次")
        try:
            return jsonify(g.workspace.batches.cancel(batch_id))
        except KeyError:
            return missing("批次")

    def handoff_record(job):
        return {"id": job.id, "line": job.line, "email": job.email, **job.payload}

    @app.get("/api/batches/<batch_id>/results")
    def batch_results(batch_id: str):
        if g.workspace is None:
            return missing("批次")
        with g.workspace.jobs.lock:
            batch = g.workspace.batches.batches.get(batch_id)
            if batch is None:
                return missing("批次")
            return jsonify(batch=batch.snapshot(), records=[handoff_record(job) for job in batch.rows
                           if job.status == "success" and job.payload is not None],
                           expires_in=registry.result_ttl)

    @app.get("/api/jobs/<job_id>/result")
    def job_result(job_id: str):
        if g.workspace is None:
            return missing()
        with g.workspace.jobs.lock:
            job = g.workspace.jobs.jobs.get(job_id)
            if job is None:
                return missing()
            return jsonify(job=job.snapshot(), records=[handoff_record(job)] if job.payload is not None else [],
                           expires_in=registry.result_ttl)

    def acknowledgement_payload():
        payload = request.get_json(silent=True)
        if (not isinstance(payload, dict) or set(payload) - {"ids", "complete"}
                or not isinstance(payload.get("ids"), list) or len(payload["ids"]) > 1000
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value)
                       for value in payload["ids"])
                or type(payload.get("complete", False)) is not bool):
            raise ValueError("无效的结果确认请求")
        return set(payload["ids"]), payload.get("complete", False)

    @app.post("/api/batches/<batch_id>/ack")
    def ack_batch(batch_id: str):
        try:
            ids, complete = acknowledgement_payload()
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        workspace = g.workspace
        if workspace is None:
            return jsonify(acknowledged=True, cleared=True)
        clear = False
        with workspace.jobs.lock:
            batch = workspace.batches.batches.get(batch_id)
            if batch is None:
                return jsonify(acknowledged=True, cleared=True)
            if not ids.issubset({job.id for job in batch.rows}):
                return jsonify(error="结果确认与当前批次不匹配"), 400
            pending = {job.id for job in batch.rows if job.payload is not None}
            if complete and (batch.status not in {"completed", "cancelled"} or pending - ids):
                return jsonify(error="请先同步全部结果并等待批次完成"), 409
            for job in batch.rows:
                if job.id in ids:
                    workspace.jobs.acknowledge(job)
            if complete:
                for job in batch.rows:
                    workspace.jobs.jobs.pop(job.id, None)
                workspace.batches.batches.pop(batch_id, None)
                if workspace.batches.current_id == batch_id:
                    workspace.batches.current_id = None
                if workspace.jobs.current_id in {job.id for job in batch.rows}:
                    workspace.jobs.current_id = None
                clear = not workspace.jobs.jobs and not workspace.batches.batches
        if clear:
            registry.release_empty(g.workspace_owner, expected=workspace)
        return jsonify(acknowledged=True, cleared=complete)

    @app.post("/api/jobs/<job_id>/ack")
    def ack_job(job_id: str):
        try:
            ids, complete = acknowledgement_payload()
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        workspace = g.workspace
        if workspace is None:
            return jsonify(acknowledged=True, cleared=True)
        clear = False
        with workspace.jobs.lock:
            job = workspace.jobs.jobs.get(job_id)
            if job is None:
                return jsonify(acknowledged=True, cleared=True)
            if not ids.issubset({job_id}):
                return jsonify(error="结果确认与当前任务不匹配"), 400
            if complete and (job.status in ACTIVE or job.payload is not None and job_id not in ids):
                return jsonify(error="请先同步结果并等待任务完成"), 409
            if job_id in ids:
                workspace.jobs.acknowledge(job)
            if complete and not job.batch_id:
                workspace.jobs.jobs.pop(job_id, None)
                if workspace.jobs.current_id == job_id:
                    workspace.jobs.current_id = None
                clear = not workspace.jobs.jobs and not workspace.batches.batches
        if clear:
            registry.release_empty(g.workspace_owner, expected=workspace)
        return jsonify(acknowledged=True, cleared=complete and not job.batch_id)

    @app.delete("/api/workspace")
    def clear_workspace():
        registry.remove(g.workspace_owner)
        return jsonify(cleared=True)

    def retired_job_export(job_id):
        if owned_job(job_id) is None:
            return missing()
        return jsonify(error="结果仅交接到当前浏览器缓存，请在当前浏览器查看或导出"), 410

    def retired_batch_export(batch_id):
        if owned_batch(batch_id) is None:
            return missing("批次")
        return jsonify(error="结果仅交接到当前浏览器缓存，请在当前浏览器查看或导出"), 410

    for route, endpoint in (("download", "download"), ("rt", "view_rt"),
                            ("cpa-download", "download_cpa"), ("sub2api-download", "download_sub2api")):
        app.add_url_rule(f"/api/jobs/<job_id>/{route}", endpoint, retired_job_export, methods=["GET"])
    for route, endpoint in (("download", "download_batch_zip"), ("rt-download", "download_batch_rt"),
                            ("sub2api-download", "download_batch_sub2api"), ("cpa-download", "download_batch_cpa"),
                            ("conversion-source", "batch_conversion_source")):
        app.add_url_rule(f"/api/batches/<batch_id>/{route}", endpoint, retired_batch_export, methods=["GET"])

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="独立 RT 提取器本地网页")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="已弃用；网页结果仅交接到浏览器缓存，此参数不读写目录")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口须为 1 到 65535")
    app = create_app(args.output_dir)
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
