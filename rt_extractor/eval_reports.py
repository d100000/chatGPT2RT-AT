"""Review-only visual aggregates, separately reported reliability and performance."""
from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import json
import statistics

from .eval_catalog import DISCLAIMER


def mean(values):
    return round(statistics.mean(values), 3) if values else None


def review_summary(record):
    reviews = record.get("reviews", [])
    if not reviews:
        return {"count": 0, "dimensions": None, "total": None, "disagreement": None, "needs_recheck": False, "human_count": 0, "double_blind": False}
    ids = [x["id"] for x in record["question"]["scoringRubric"]["dimensions"]]
    dimensions = {key: mean([x["dimensions"][key]["score"] for x in reviews]) for key in ids}
    totals = [sum(x["dimensions"][key]["score"] for key in ids) for x in reviews]
    gap = max(totals) - min(totals) if len(reviews) > 1 else None
    dimension_gaps = {key: max(x["dimensions"][key]["score"] for x in reviews) - min(x["dimensions"][key]["score"] for x in reviews) for key in ids}
    threshold = record["question"]["scoringRubric"].get("disagreement", {"total": 4, "dimension": 2})
    human = [x for x in reviews if x["method"] == "human"]
    return {"count": len(reviews), "human_count": len(human), "dimensions": dimensions, "total": mean(totals),
            "disagreement": gap, "dimension_disagreements": dimension_gaps,
            "double_blind": len(human) == 2 and all(x.get("blind") for x in human),
            "needs_recheck": len(reviews) > 1 and (gap >= threshold["total"] or max(dimension_gaps.values()) >= threshold["dimension"]) and not record.get("recheck")}


def report(batches, records):
    groups = {}
    for record in records:
        # Comparing different parameters/rubrics as one population is misleading.
        identity = {"requested_model": record["requested_model"], "question_id": record["question"]["id"],
                    "question_version": record["question"]["version"], "rubric_version": record["question"]["rubricVersion"],
                    "parameters": record["parameters"]}
        key = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        groups.setdefault(key, {"conditions": identity, "records": []})["records"].append(record)
    summaries = []
    for group in groups.values():
        rows = group.pop("records")
        finished = [r for r in rows if r["status"] != "running"]
        scores = [review_summary(r) for r in finished if r.get("reviews")]
        totals = [s["total"] for s in scores]
        n = len(finished)
        valid = sum(r["technical"]["structure_valid"] for r in finished)
        safe = sum(r["technical"]["safe"] for r in finished)
        renders = [r["technical"]["browser_rendered"] for r in finished if r["technical"]["browser_rendered"] is not None]
        failures = sum(r["status"] in {"failed", "timeout", "interrupted"} for r in finished)
        group.update(samples=n, running=len(rows)-n, success=sum(r["status"] == "success" for r in finished), failed=failures,
                     cancelled=sum(r["status"] == "cancelled" for r in finished), failure_rate=failures/n if n else None,
                     valid_svg=valid, valid_svg_rate=valid/n if n else None, safe_svg=safe,
                     render_success=sum(renders), render_attempted=len(renders), render_rate=sum(renders)/len(renders) if renders else None,
                     render_coverage=len(renders)/n if n else None, render_success_all_rate=sum(renders)/n if n else None,
                     scored=len(scores), scoring_coverage=len(scores)/n if n else None,
                     dimensions={k: mean([s["dimensions"][k] for s in scores]) for k in (scores[0]["dimensions"] if scores else [])},
                     total_mean=mean(totals), total_min=min(totals) if totals else None, total_max=max(totals) if totals else None,
                     total_population_sd=round(statistics.pstdev(totals), 3) if len(totals) > 1 else None,
                     review_methods=sorted({v["method"] for r in finished for v in r.get("reviews", [])}),
                     reviewer_count=len({v["reviewer"] for r in finished for v in r.get("reviews", [])}),
                     single_review_samples=sum(s["count"] == 1 for s in scores), double_blind_samples=sum(s["double_blind"] for s in scores),
                     needs_recheck=sum(s["needs_recheck"] for s in scores), rechecked=sum(bool(r.get("recheck")) for r in finished),
                     mean_disagreement=mean([s["disagreement"] for s in scores if s["disagreement"] is not None]),
                     duration_mean_ms=mean([r["duration_ms"] for r in finished if r.get("duration_ms") is not None]),
                     ttft_mean_ms=mean([r["ttft_ms"] for r in finished if r.get("ttft_ms") is not None]),
                     duration_reported=sum(r.get("duration_ms") is not None for r in finished),
                     ttft_reported=sum(r.get("ttft_ms") is not None for r in finished),
                     token_usage={k: {"sum": sum(values) if values else None, "reported": len(values)} for k in ("input", "output", "total") for values in [[r.get("usage", {}).get(k) for r in finished if r.get("usage", {}).get(k) is not None]]},
                     interpretation="小样本只作描述，微小分差不是稳定排名或统计显著差异。" if n <= 3 else "描述统计，不作显著性或上游身份推断。")
        summaries.append(group)
    return {"schema_version": 1, "notice": DISCLAIMER, "groups": summaries, "batches": batches, "samples": records}


def csv_export(data):
    stream = io.StringIO(newline="")
    fields = ["id", "batch_id", "round", "requested_model", "returned_model", "question", "version", "mode", "status", "http_status", "finish_reason", "started_at", "ended_at", "duration_ms", "ttft_ms", "input_tokens", "output_tokens", "total_tokens", "svg_count", "structure_valid", "safe", "browser_rendered", "review_count", "A", "B", "C", "D", "total_score", "disagreement", "needs_recheck", "parameters", "review_evidence", "error"]
    writer = csv.writer(stream)
    writer.writerow(fields)
    def safe(value):
        if value is None:
            return ""
        text = str(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) or text.startswith(("\t", "\r", "\n")) else text
    for r in data["samples"]:
        score = review_summary(r)
        dimensions = score["dimensions"] or {}
        row = [r["id"], r["batch_id"], r["round"], r["requested_model"], r.get("returned_model"), r["question"]["id"], r["question"]["version"], r["mode"], r["status"], r.get("http_status"), r.get("finish_reason"), r["started_at"], r.get("ended_at"), r.get("duration_ms"), r.get("ttft_ms"), *[r.get("usage", {}).get(k) for k in ("input", "output", "total")], *[r["technical"].get(k) for k in ("svg_count", "structure_valid", "safe", "browser_rendered")], score["count"], *[dimensions.get(k) for k in "ABCD"], score["total"], score["disagreement"], score["needs_recheck"], json.dumps(r["parameters"], ensure_ascii=False), json.dumps(r.get("reviews", []), ensure_ascii=False), r.get("error")]
        writer.writerow([safe(v) for v in row])
    return "\ufeff" + stream.getvalue()


def html_export(data):
    esc = lambda x: html.escape(str(x if x is not None else "未知"))
    pretty = lambda x: esc(json.dumps(x, ensure_ascii=False, indent=2))
    sections = []
    for r in data["samples"]:
        image = ""
        if r.get("render_evidence"):
            image = r["render_evidence"]["data_url"]
        elif r.get("svg") and r["technical"]["safe"]:
            image = "data:image/svg+xml;base64," + base64.b64encode(r["svg"].encode()).decode()
        preview = f'<img alt="样本 {esc(r["id"])}" src="{image}">' if image else '<p>无可安全预览的图片</p>'
        sections.append(f'<article><h3>{esc(r["requested_model"])} · 第 {r["round"]} 轮 · {esc(r["status"])}</h3>{preview}<p>样本 {esc(r["id"])} / 批次 {esc(r["batch_id"])}</p><p>视觉总分 {esc(review_summary(r)["total"])} / 20；评审 {len(r.get("reviews", []))} 名</p><pre>{pretty({k: v for k, v in r.items() if k not in {"raw_wire", "svg", "render_evidence"}})}</pre></article>')
    style = "body{font:15px/1.7 system-ui,sans-serif;color:#17233d;background:#f5f7fb;margin:32px auto;padding:0 20px;max-width:1200px}h1{font-size:28px}article,section{padding:20px;background:white;border:1px solid #dce3ee;border-radius:8px;margin:20px 0}img{width:100%;max-height:420px;object-fit:contain}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 monospace}.samples{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:20px}"
    style_hash = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    policy = f"default-src 'none'; img-src data:; style-src 'sha256-{style_hash}'; base-uri 'none'; form-action 'none'"
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{html.escape(policy, quote=True)}"><title>GPT 能力评测 · 鹈鹕报告</title><style>{style}</style></head><body><h1>GPT 能力评测 · 鹈鹕报告</h1><p>{esc(DISCLAIMER)}</p><p>视觉均分只基于已评分样本。技术失败、取消、性能与视觉分分别统计。浏览器渲染率以已尝试渲染的样本为分母，同时展示渲染覆盖率。小样本不作稳定排名。</p><section><h2>分组统计（参数相同才合并）</h2><pre>{pretty(data["groups"])}</pre></section><details><summary>题面、来源、规则与批次配置快照</summary><pre>{pretty(data["batches"])}</pre></details><div class="samples">{"".join(sections)}</div></body></html>'
