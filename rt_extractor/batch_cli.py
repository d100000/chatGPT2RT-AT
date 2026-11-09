"""Explicit local extraction with private files and sequential 1,000-account groups.

Unlike the browser workspace, this command owns its output directory and keeps
each successful result on disk. Input passwords and 2FA seeds are never logged.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
from typing import Callable

from .api import extract_tokens
from .batch import (DEFAULT_CONCURRENCY, MAX_ACCOUNTS, MAX_CONCURRENCY,
                    BatchInputError, BatchManager, parse_accounts,
                    validate_concurrency, validate_skip_email_verification)
from .cpa import save_export
from .web_jobs import JobManager

MAX_INPUT_BYTES = 20 * 1024 * 1024
_COUNT_KEYS = ("queued", "running", "awaiting_code", "success", "error", "cancelled", "skipped")


def _erase(groups) -> None:
    for group in groups:
        for _, credentials in group:
            credentials.clear()
        group.clear()


def parse_grouped_accounts(text: str, *, concurrency: int = DEFAULT_CONCURRENCY,
                           skip_email_verification: bool = False,
                           timeout: float = 180, proxy: str | None = None) -> list[list[tuple[int, dict]]]:
    """Validate the complete input before returning any executable group.

    Original file line numbers survive grouping. Duplicate emails are rejected
    across groups as well as within a group. Errors never quote input values.
    """
    validate_concurrency(concurrency)
    validate_skip_email_verification(skip_email_verification)
    if not isinstance(text, str):
        raise ValueError("账号文件须为 UTF-8 文本")
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("账号文件不能超过 20 MiB")
    lines = [(number, raw) for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1) if raw.strip()]
    if not lines:
        raise BatchInputError([{"line": 0, "message": "账号文件没有非空账号行"}])
    groups, errors, seen = [], [], set()
    try:
        for start in range(0, len(lines), MAX_ACCOUNTS):
            chunk = lines[start:start + MAX_ACCOUNTS]
            try:
                parsed = parse_accounts({
                    "accounts_text": "\n".join(raw for _, raw in chunk),
                    "concurrency": concurrency, "skip_email_verification": skip_email_verification,
                    "timeout": timeout, "proxy": proxy,
                })
            except BatchInputError as exc:
                errors.extend({"line": chunk[item["line"] - 1][0] if item["line"] else chunk[0][0],
                               "message": item["message"]} for item in exc.errors)
                continue
            group = []
            groups.append(group)
            for number, credentials in parsed:
                original = chunk[number - 1][0]
                key = credentials["email"].casefold()
                if key in seen:
                    errors.append({"line": original, "message": "邮箱在导入文件中重复，请移除重复行"})
                    credentials.clear()
                else:
                    seen.add(key)
                    group.append((original, credentials))
        if errors:
            raise BatchInputError(errors)
        return groups
    except BaseException:
        _erase(groups)
        raise


def _write_json(path: Path, value: dict) -> None:
    """Replace our own progress file atomically, always with private permissions."""
    fd, temporary = tempfile.mkstemp(prefix=".progress-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_groups(groups: list[list[tuple[int, dict]]], output_dir: Path, *,
               concurrency: int = DEFAULT_CONCURRENCY, skip_email_verification: bool = False,
               extractor: Callable = extract_tokens, stop_event: threading.Event | None = None,
               poll_interval: float = 1, cancel_grace: float = 30,
               report: Callable[[str], None] = print) -> dict:
    """Run prevalidated groups once, in order, returning a credential-free report.

    Takes ownership of credential dictionaries and clears them on completion or
    cancellation. The output directory must be new; existing results are never
    overwritten. A cleanup timeout is explicit and prevents the next group.
    """
    concurrency = validate_concurrency(concurrency)
    validate_skip_email_verification(skip_email_verification)
    if not groups or any(not group or len(group) > MAX_ACCOUNTS for group in groups):
        raise ValueError("每个分组须包含 1 到 1,000 条已校验账号")
    if poll_interval <= 0 or cancel_grace < 0:
        raise ValueError("轮询间隔须大于零，取消等待时间不能为负数")
    output_dir = Path(output_dir).expanduser().absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("输出目录已存在，请指定一个全新的目录")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.name == "posix":
        output_dir.chmod(0o700)
    stop = stop_event if stop_event is not None else threading.Event()
    manager = BatchManager(JobManager(output_dir, extractor))
    metadata = [{"group": index + 1, "total": len(group), "first_line": group[0][0],
                 "last_line": group[-1][0], "status": "queued"}
                for index, group in enumerate(groups)]
    total = sum(item["total"] for item in metadata)
    finished = {key: 0 for key in _COUNT_KEYS}
    current_id = None
    current_counts = {key: 0 for key in _COUNT_KEYS}
    last_notice = None
    status = "running"
    group_number = 0

    def publish(*, final=False):
        nonlocal last_notice
        counts = {key: finished[key] + current_counts.get(key, 0) for key in _COUNT_KEYS}
        remaining = total - sum(counts.values())
        counts["cancelled" if status in {"cancelled", "cleanup_timeout"} else "queued"] += remaining
        value = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat(),
                 "total": total, "group": group_number, "groups_total": len(metadata),
                 "concurrency": concurrency, "skip_email_verification": skip_email_verification,
                 "counts": {"total": total, **counts}, "groups": metadata}
        _write_json(output_dir / "progress.json", value)
        if final:
            _write_json(output_dir / "report.json", value)
        # Counts only: no email, exception bodies, proxy, OTP, or Token values.
        notice = {"status": status, "group": group_number, "groups_total": len(metadata), "counts": value["counts"]}
        if notice != last_notice:
            report(json.dumps(notice, ensure_ascii=False))
            last_notice = notice
        return value

    try:
        publish()
        for index, group in enumerate(groups):
            if stop.is_set():
                status = "cancelled"
                break
            group_number = index + 1
            accepted = manager.start(group, concurrency=concurrency,
                                     skip_email_verification=skip_email_verification)
            current_id = accepted["id"]
            item = metadata[index]
            item.update({"id": current_id, "directory": f"batch-{current_id}", "status": "running"})
            cancelled_at = None
            while True:
                if stop.is_set() and cancelled_at is None:
                    manager.cancel(current_id)
                    cancelled_at = time.monotonic()
                snapshot = manager.snapshot(current_id)
                current_counts = {key: snapshot["counts"].get(key, 0) for key in _COUNT_KEYS}
                item["status"] = snapshot["status"]
                item["counts"] = dict(snapshot["counts"])
                publish()
                if snapshot["status"] in {"completed", "cancelled"}:
                    break
                if cancelled_at is not None and time.monotonic() - cancelled_at >= cancel_grace:
                    status = "cleanup_timeout"
                    item["status"] = "cleanup_timeout"
                    return publish(final=True)
                time.sleep(poll_interval)
            exports = {}
            if snapshot["counts"]["success"]:
                for kind, kwargs in (("raw_zip", {}), ("rt_text", {"rt_only": True})):
                    stream, filename, _ = manager.export(current_id, **kwargs)
                    save_export(stream.getvalue(), output_dir / item["directory"] / filename)
                    exports[kind] = f"{item['directory']}/{filename}"
                # These two are generated by BatchManager before publishing its
                # terminal snapshot. Do not claim they exist after conversion errors.
                if snapshot["result"] and snapshot["result"]["conversion_status"] == "ready":
                    for kind, filename in (("cpa_zip", f"cpa-{current_id}.zip"),
                                           ("sub2api_json", f"sub2api-{current_id}.json")):
                        exports[kind] = f"{item['directory']}/{filename}"
            item["exports"] = exports
            item["conversion_status"] = snapshot["result"]["conversion_status"] if snapshot["result"] else "not_saved"
            for key in _COUNT_KEYS:
                finished[key] += current_counts[key]
            current_counts = {key: 0 for key in _COUNT_KEYS}
            current_id = None
            if stop.is_set() or snapshot["status"] == "cancelled":
                status = "cancelled"
                break
        else:
            status = "completed"
        if status == "cancelled":
            for item in metadata:
                if item["status"] == "queued":
                    item["status"] = "cancelled"
        return publish(final=True)
    except BaseException:
        if current_id is not None:
            manager.cancel(current_id)
        raise
    finally:
        _erase(groups)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地分组提取 Token；每组最多 1,000 条，成功结果立即保存")
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 账号文本文件")
    parser.add_argument("--output-dir", required=True, type=Path, help="全新的私有结果目录")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, choices=range(1, MAX_CONCURRENCY + 1))
    parser.add_argument("--skip-email-verification", action="store_true", help="需要邮箱验证码时跳过该账号")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--proxy", help="统一登录代理；不会写入结果或日志")
    args = parser.parse_args(argv)
    groups = []
    stop = threading.Event()
    previous = {}
    try:
        if args.input.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("账号文件不能超过 20 MiB")
        text = args.input.read_text(encoding="utf-8-sig")
        groups = parse_grouped_accounts(text, concurrency=args.concurrency,
                                        skip_email_verification=args.skip_email_verification,
                                        timeout=args.timeout, proxy=args.proxy)
        text = ""
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda _number, _frame: stop.set())
        result = run_groups(groups, args.output_dir, concurrency=args.concurrency,
                            skip_email_verification=args.skip_email_verification, stop_event=stop,
                            report=lambda message: print(message, flush=True))
        if result["status"] == "cleanup_timeout":
            # Cancelling coroutine work cannot forcibly stop a stuck synchronous
            # VM thread. All workers have received cancellation; terminate this
            # standalone process after the report is safely written.
            os._exit(130)
        if result["status"] == "cancelled":
            return 130
        export_failed = any(group.get("conversion_status") == "error" for group in result["groups"])
        return 1 if result["counts"]["error"] or export_failed else 0
    except BatchInputError as exc:
        print(json.dumps({"status": "invalid_input", "errors": exc.errors}, ensure_ascii=False), flush=True)
        return 2
    except (ValueError, UnicodeError):
        print("输入或输出配置无效；请检查 UTF-8、账号格式、并发数和全新输出目录。", flush=True)
        return 2
    except OSError:
        print("无法读取输入或保存结果；请检查本地路径和文件权限。", flush=True)
        return 2
    finally:
        _erase(groups)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
