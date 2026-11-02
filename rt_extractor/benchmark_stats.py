"""Measured-only R-7 statistics, deterministic observations and offline exports."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import csv
from datetime import datetime
from html import escape
import io
import json
import math
import time
from zoneinfo import ZoneInfo

from .benchmark_protocol import safe_number

METHODS = [
    '所有性能、成功率、Token 和缓存统计排除预热。完成表示计划已执行结束，不代表全部请求成功；失败不重试。',
    '首字从实际发起请求计到第一个非空白可见正文流式 delta；响应头、推理、完成事件正文和 JSON 回退不补造首字。TTFB 仅指响应头可用时间。',
    '分位数采用 R-7 线性插值。仅成功、实际流式且观测到正文 delta 的请求参与首字统计；未知显示 —，真实 0 保留。',
    '成功请求/秒和输出 Token/秒以各阶段测量墙钟窗口之和为分母；输出 Token 可能包含推理，不代表单条回答正文生成速度。',
    '缓存命中率仅在 input 与 cached 为非负安全整数且 cached≤input 的成功样本中按总量计算。cached=0 是已知未命中；缺失和异常不补零。',
    '输入长度是固定英文词库的近似 Token 目标，加上指令和 nonce；不是 tokenizer 精确计数。实际用量只取上游 usage。随机输入不保证冷缓存，共享前缀不保证命中。',
    '结果受网络、上游负载、限流、分词、输出长度、推理量及模型依次执行的时间差影响。观测最佳档位不能自动视为稳定容量；跨模式汇总仅作概览。',
    '本地取消不等于确认上游计算已停止。本模块不估算费用，不判断真实模型身份、智力或回答质量；输出仅保留最多 2048 字符预览。',
]


def ratio(a, b):
    return a / b if b else None


def distribution(values):
    values = sorted(x for x in values if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0)
    def quantile(p):
        pos = (len(values)-1)*p
        a, b = math.floor(pos), math.ceil(pos)
        return values[a] + (values[b]-values[a]) * (pos-a)
    return dict(count=len(values), mean=sum(values)/len(values) if values else None,
                p50=quantile(.5) if values else None, p95=quantile(.95) if values else None,
                p99=quantile(.99) if values else None, min=values[0] if values else None, max=values[-1] if values else None)


def aggregate(records, seconds, planned):
    rows = [r for r in records if not r['warmup']]
    success = [r for r in rows if r['status'] == 'success']
    failed = [r for r in rows if r['status'] == 'error']
    cancelled = sum(r['status'] == 'cancelled' for r in rows)
    running = sum(r['status'] == 'running' for r in rows)
    completed = len(success)+len(failed)+cancelled
    paired = [r for r in success if r.get('cacheValid') == 'valid']
    cached = sum(r['usage']['cached'] for r in paired)
    paired_input = sum(r['usage']['input'] for r in paired)
    input_rows = [r for r in success if safe_number(r.get('usage', {}).get('input'))]
    output_rows = [r for r in success if safe_number(r.get('usage', {}).get('output'))]
    known_input = sum(r['usage']['input'] for r in input_rows)
    output = sum(r['usage']['output'] for r in output_rows)
    codes = Counter(str(r.get('statusCode')) if r.get('statusCode') is not None else 'network' for r in failed)
    count429 = sum(r.get('statusCode') == 429 for r in failed)
    count5xx = sum(isinstance(r.get('statusCode'), int) and 500 <= r['statusCode'] < 600 for r in failed)
    return dict(success=len(success), failed=len(failed), cancelled=cancelled, running=running,
                completed=completed, planned=planned, unstarted=max(0, planned-len(rows)), successRate=ratio(len(success), completed),
                firstText=distribution(r.get('firstTextMs') for r in success if r.get('streamActual')),
                firstReasoning=distribution(r.get('firstReasoningMs') for r in success if r.get('streamActual')),
                ttfb=distribution(r.get('ttfbMs') for r in success), duration=distribution(r.get('durationMs') for r in success),
                measurementSeconds=seconds, successRps=ratio(len(success), seconds), outputTokensPerSecond=ratio(output, seconds) if output_rows else None,
                inputTokens=known_input if input_rows else None, inputUsageCount=len(input_rows),
                outputTokens=output if output_rows else None, outputUsageCount=len(output_rows),
                jsonFallback=sum(r.get('streamActual') is False and r.get('statusCode') is not None and 200 <= r['statusCode'] < 300 for r in rows),
                errors=dict(http429=count429, http5xx=count5xx, other=len(failed)-count429-count5xx, byStatus=dict(codes)),
                cache=dict(valid=len(paired), missing=sum(r.get('cacheValid', 'missing') == 'missing' for r in success),
                           invalid=sum(r.get('cacheValid') == 'invalid' for r in success),
                           cachedTokens=cached if paired else None, pairedInputTokens=paired_input if paired else None,
                           tokenHitRate=ratio(cached, paired_input), requestHitRate=ratio(sum(r['usage']['cached'] > 0 for r in paired), len(paired)),
                           requestCoverage=ratio(len(paired), len(success)), inputCoverage=ratio(paired_input, known_input)))


def observations(view):
    stats, progress = view['stats'], view['progress']
    report = []
    if progress['completed'] < progress['planned']:
        report.append(f"本次为部分计划结果：已结束 {progress['completed']}/{progress['planned']} 次调用；未派发样本没有补造结果。")
    else:
        report.append('计划调用已结束；请结合测量成功率判断可用性。')
    report.append(f"测量成功 {stats['success']}，失败 {stats['failed']}，取消 {stats['cancelled']}；HTTP 429：{stats['errors']['http429']}，5xx：{stats['errors']['http5xx']}。")
    report.append(f"首字有效样本 {stats['firstText']['count']}/{stats['success']} 个成功测量；JSON 回退 {stats['jsonFallback']} 次。缓存有效 {stats['cache']['valid']}，缺失 {stats['cache']['missing']}，异常 {stats['cache']['invalid']}。")
    warmup = view['warmup']
    report.append(f"预热成功 {warmup['success']}，失败 {warmup['failed']}，取消 {warmup['cancelled']}；预热失败不表示缓存已预热，预热成功也不保证命中。")
    groups = defaultdict(list)
    for stage in view['stages']:
        if stage['stats']['completed']:
            groups[(stage['model'], stage['mode'])].append(stage)
    for (model, mode), stages in groups.items():
        valid = [s for s in stages if s['status'] == 'completed' and s['stats']['success'] and s['stats']['successRps'] is not None]
        label = f'{model} / {"随机输入" if mode == "unique" else "共享前缀"}'
        if valid:
            best = max(valid, key=lambda s: s['stats']['successRps'])
            report.append(f"{label}：已完成阶段中成功 RPS 最高的档位为 {best['concurrency']}（{best['stats']['successRps']:.3f} 请求/秒，成功/完成 {best['stats']['success']}/{best['stats']['completed']}，首字样本 {best['stats']['firstText']['count']}）。")
        ttft = sorted([s for s in valid if s['stats']['firstText']['mean'] is not None], key=lambda s: s['concurrency'])
        if len(ttft) >= 2:
            low, high = ttft[0], ttft[-1]
            a, b = low['stats']['firstText']['mean'], high['stats']['firstText']['mean']
            multiple = f'{b/a:.2f} 倍' if a else '倍数未知（起始值为 0）'
            report.append(f"{label}：并发 {low['concurrency']} → {high['concurrency']} 的平均首字 {a:.1f} → {b:.1f} ms，变化 {b-a:+.1f} ms，{multiple}。")
    if any(s['stats']['completed'] and s['stats']['firstText']['count'] < 20 for s in view['stages']):
        report.append('部分阶段有效首字样本不足 20，P95/P99 的样本有限，需增加样本后再比较。')
    report.extend(METHODS[-2:])
    return report


def snapshot(run, *, full=False):
    # Called under the service lock; do not deepcopy a key or runtime handle.
    view = deepcopy({k: v for k, v in run.items() if not k.startswith('_') and k not in {'owner', 'records'}})
    records = run['records']
    by_stage = defaultdict(list)
    by_model = defaultdict(list)
    for record in records:
        by_stage[record['stageIndex']].append(record)
        by_model[record['model']].append(record)
    model_seconds = defaultdict(float)
    all_seconds = 0
    for stage, source in zip(view['stages'], run['stages']):
        elapsed = source.get('measurementMs', 0)
        if source.get('_measurementStart') is not None:
            elapsed = (time.perf_counter()-source['_measurementStart'])*1000
        for key in list(stage):
            if key.startswith('_'):
                stage.pop(key)
        stage['measurementMs'] = elapsed
        stage['stats'] = aggregate(by_stage[stage['index']], elapsed/1000, stage['planned'])
        model_seconds[stage['model']] += elapsed/1000
        all_seconds += elapsed/1000
    view['stats'] = aggregate(records, all_seconds, view['plan']['measuredPlanned'])
    view['models'] = [dict(model=m, stats=aggregate(by_model[m], model_seconds[m], sum(s['planned'] for s in view['stages'] if s['model'] == m))) for m in view['config']['models']]
    warmup = [r for r in records if r['warmup']]
    view['warmup'] = {s: sum(r['status'] == status for r in warmup) for s, status in [('success', 'success'), ('failed', 'error'), ('cancelled', 'cancelled'), ('running', 'running')]}
    warm_done = sum(view['warmup'][s] for s in ('success', 'failed', 'cancelled'))
    view['progress'] = dict(measuredCompleted=view['stats']['completed'], measuredPlanned=view['plan']['measuredPlanned'],
                            warmupCompleted=warm_done, warmupPlanned=view['plan']['warmupPlanned'],
                            completed=view['stats']['completed']+warm_done, planned=view['plan']['planned'],
                            inFlight=sum(r['status'] == 'running' for r in records),
                            unstarted=view['plan']['planned']-len(records))
    view['durationMs'] = (time.perf_counter()-run['_start'])*1000 if run.get('_start') is not None else run.get('durationMs')
    view['report'] = observations(view)
    view['records'] = deepcopy(records if full else list(reversed(records[-50:])))
    return view


def csv_export(view):
    fields = ['id', 'index', 'model', 'returnedModel', 'mode', 'concurrency', 'stageIndex', 'warmup', 'status',
              'startedAt', 'endedAt', 'durationMs', 'firstTextMs', 'firstReasoningMs', 'ttfbMs', 'streamActual',
              'finishReason', 'statusCode', 'cacheValid', 'usage.input', 'usage.output', 'usage.total', 'usage.cached',
              'usage.reasoning', 'input.targetTokens', 'input.characters', 'input.sha256', 'input.nonce', 'input.preview', 'outputPreview', 'error']
    out = io.StringIO(newline='')
    out.write('\ufeff')
    writer = csv.writer(out)
    writer.writerow(fields)
    for record in view['records']:
        row = []
        for name in fields:
            value = record
            for part in name.split('.'):
                value = value.get(part) if isinstance(value, dict) else None
            if value is None:
                value = ''
            if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n'))):
                value = "'" + value
            row.append(value)
        writer.writerow(row)
    return out.getvalue()


def html_export(view):
    def e(value):
        return escape(str(value if value is not None else '—'), quote=True)
    def number(value):
        return '—' if value is None else f'{value:,.2f}'
    def percent(value):
        return '—' if value is None else f'{value*100:.1f}%'
    def table(headers, rows):
        return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+e(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+e(c)+'</td>' for c in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    stats = view['stats']
    generated = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S Asia/Shanghai')
    model_rows = [[x['model'], f"{x['stats']['success']}/{x['stats']['completed']}", number(x['stats']['firstText']['mean']), number(x['stats']['firstText']['p95']), percent(x['stats']['cache']['tokenHitRate']), percent(x['stats']['cache']['requestHitRate']), percent(x['stats']['cache']['requestCoverage'])] for x in view['models']]
    stage_rows = [[s['model'], s['mode'], s['concurrency'], s['status'], f"{s['stats']['success']}/{s['stats']['completed']}", s['stats']['failed'], s['stats']['cancelled'], number(s['stats']['firstText']['mean']), number(s['stats']['firstText']['p95']), s['stats']['firstText']['count'], number(s['stats']['successRps']), number(s['stats']['outputTokensPerSecond']), s['stats']['outputUsageCount'], percent(s['stats']['cache']['tokenHitRate']), s['peakInFlight']] for s in view['stages']]
    failures = [r for r in view['records'] if r['status'] in {'error', 'cancelled'}]
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>并发分析报告</title><style>body{font:14px/1.7 system-ui,sans-serif;color:#17233d;max-width:1500px;margin:32px auto;padding:0 24px}h1,h2{color:#002fa7}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #dce3ee;padding:8px;text-align:left;overflow-wrap:anywhere}th{background:#f5f7fb}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fb;padding:16px}.scroll{overflow-x:auto}li{margin:8px 0}@media print{body{max-width:none;margin:0;padding:0;font-size:10px}table{font-size:8px}tr{break-inside:avoid}.scroll{overflow:visible}h2{break-after:avoid}}</style><body>' + \
        '<h1>大模型接口 · 并发分析报告</h1><p>生成时间：'+e(generated)+'</p><p>批次 '+e(view['runId'])+' · 状态 '+e(view['status'])+' · 总完成 '+e(view['progress']['completed'])+'/'+e(view['progress']['planned'])+'</p>'+ \
        '<h2>本次观察</h2><ul>'+''.join('<li>'+e(x)+'</li>' for x in view['report'])+'</ul>'+ \
        '<h2>配置与覆盖</h2><pre>'+e(json.dumps(dict(config=view['config'], seed=view['seed'], progress=view['progress'], warmup=view['warmup']), ensure_ascii=False, indent=2))+'</pre>'+ \
        '<h2>整体概览</h2>'+table(['测量成功/完成', '成功率', '平均首字 ms', 'P95 ms', '首字样本', 'Token 缓存命中率', '请求缓存覆盖', '测量窗口秒', '任务总耗时秒'], [[f"{stats['success']}/{stats['completed']}", percent(stats['successRate']), number(stats['firstText']['mean']), number(stats['firstText']['p95']), stats['firstText']['count'], percent(stats['cache']['tokenHitRate']), percent(stats['cache']['requestCoverage']), number(stats['measurementSeconds']), number(view.get('durationMs', 0)/1000)]])+ \
        '<h2>时间分布（ms）</h2>'+table(['时间', '样本', '均值', 'P50', 'P95', 'P99', '最小', '最大'], [[name, stats[field]['count'], *[number(stats[field][k]) for k in ('mean', 'p50', 'p95', 'p99', 'min', 'max')]] for name, field in [('正文首字', 'firstText'), ('推理首段', 'firstReasoning'), ('响应头 TTFB', 'ttfb'), ('请求总耗时', 'duration')]])+ \
        '<h2>模型汇总</h2>'+table(['模型', '成功/完成', '平均首字 ms', 'P95 ms', 'Token 缓存命中率', '请求命中比例', '请求覆盖率'], model_rows)+ \
        '<h2>分阶段比较</h2>'+table(['模型', '模式', '并发', '状态', '成功/完成', '失败', '取消', '平均首字 ms', 'P95 ms', '首字样本', '成功请求/秒', '输出 Token/秒', '输出 usage 样本', '缓存命中率', '峰值在途'], stage_rows)+ \
        '<h2>错误分布</h2>'+table(['HTTP 状态/网络', '测量失败次数'], list(stats['errors']['byStatus'].items()))+ \
        '<h2>失败与取消详情</h2><p>共 '+e(len(failures))+' 条，展示前 100 条；CSV/JSON 包含全部已派发记录。</p>'+table(['ID', '模型', '预热', '状态', 'HTTP', '错误', '输出预览'], [[r['id'], r['model'], r['warmup'], r['status'], r.get('statusCode'), r.get('error'), r.get('outputPreview')] for r in failures[:100]])+ \
        '<h2>测量方法与结果边界</h2><ul>'+''.join('<li>'+e(x)+'</li>' for x in METHODS)+'</ul></body></html>'
