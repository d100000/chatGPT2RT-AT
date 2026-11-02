import { active, api, calculatePlan, esc, modeName, num, parseLevels, pct, stamp, statusName, validURL } from './benchmark-common.mjs';
const $=id=>document.getElementById(id);
const controls={url:'bench-url',protocol:'bench-protocol',concurrencyLevels:'bench-levels',requestsPerStage:'bench-requests',inputMinTokens:'bench-input-min',inputMaxTokens:'bench-input-max',maxOutputTokens:'bench-max-output',timeoutSeconds:'bench-timeout',reasoningEffort:'bench-reasoning',outputTokenField:'bench-output-field'};
const numbers=['requestsPerStage','inputMinTokens','inputMaxTokens','maxOutputTokens','timeoutSeconds'];
const storage='bob-benchmark-config-v1';
let shared={}, models=[], selected=new Set(), desired=[], loadedConnection='', modelEpoch=0, modelTimer, modelController;
let current={status:'idle'}, viewed=null, historyId='', historyEpoch=0, online=false, pollPending=false, pollTimer, busy=false, loadingModels=false, restoredRun=null, latestStateEpoch=0;
const expanded=new Set();
const badge=status=>`<span class="benchmark-status" data-status="${esc(status)}">${esc(statusName(status))}</span>`;
function message(id, text) { $(id).textContent=text || ''; $(id).hidden=!text; }
function config() {
  const value=Object.fromEntries(Object.entries(controls).map(([k,id])=>[k,numbers.includes(k)?Number($(id).value):$(id).value.trim()]));
  value.concurrencyLevels=parseLevels(value.concurrencyLevels);
  value.models=[...selected]; value.modes=[];
  if ($('bench-unique').checked) value.modes.push('unique');
  if ($('bench-shared-prefix').checked) value.modes.push('shared-prefix');
  value.apiKey=$('bench-key').value;
  return value;
}
function fingerprint() { return JSON.stringify([$('bench-url').value.trim(),$('bench-protocol').value,$('bench-key').value,shared.revision || '']); }
function parameters(value) {
  for (const [name,low,high,label] of [['requestsPerStage',1,200,'每档请求数'],['inputMinTokens',64,16384,'输入下限'],['inputMaxTokens',64,16384,'输入上限'],['maxOutputTokens',1,16384,'最大输出 Token'],['timeoutSeconds',1,600,'总超时秒数']]) {
    if(!Number.isInteger(value[name]) || value[name]<low || value[name]>high) throw new Error(`${label}须为 ${low}–${high} 的整数。`);
  }
  if(value.inputMaxTokens<value.inputMinTokens) throw new Error('输入上限不能小于下限。');
  if(!value.modes.length) throw new Error('至少选择一种输入模式。');
  if(value.models.length>10) throw new Error('每批最多选择 10 个模型。');
  if(calculatePlan(value).planned>10000) throw new Error('总调用数超过 10,000，请减少模型、档位或请求数。');
}
function save(value) {
  try {
    validURL(value.url);
    const {apiKey,...safe}=value;
    if(apiKey && JSON.stringify(safe).includes(apiKey)) return;
    localStorage.setItem(storage,JSON.stringify(safe));
  } catch { /* Invalid URLs and credentials never enter persisted settings. */ }
}
function fill(value) {
  for(const [name,id] of Object.entries(controls)) if(value[name]!=null) $(id).value=Array.isArray(value[name])?value[name].join(', '):value[name];
  if(value.modes) { $('bench-unique').checked=value.modes.includes('unique'); $('bench-shared-prefix').checked=value.modes.includes('shared-prefix'); }
  $('bench-output-field-wrap').hidden=$('bench-protocol').value!=='chat';
}
function updatePlan() {
  let error='', value;
  try { value=config(); parameters(value); } catch(e) {error=e.message;}
  const p=value?calculatePlan(value):null;
  $('bench-plan').innerHTML=[['模型',p?.models],['输入模式',p?.modes],['档位 / 阶段',p?`${p.levels} / ${p.stages}`:null],['测量',p?.measuredPlanned],['预热',p?.warmupPlanned],['总调用',p?.planned]].map(([k,v])=>`<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('');
  message('bench-validation',error);
  $('bench-fields').disabled=active(current)||busy;
  $('bench-use-shared').disabled=active(current)||busy||!shared.url;
  $('bench-start').disabled=!online||active(current)||busy||loadingModels||!!error||!value?.models.length||loadedConnection!==fingerprint();
  $('bench-stop').disabled=!online||current.status!=='running'||busy;
  $('bench-stop').textContent=current.status==='stopping'?'正在停止…':'停止当前批次';
  $('bench-stop').title=active(current)?`停止当前后台批次 ${current.runId}，与正在查看的历史无关`:'';
  $('bench-model-load').disabled=active(current)||busy||loadingModels;
  $('bench-model-load').textContent=loadingModels?'读取中…':'读取模型';
  $('bench-output-field-wrap').hidden=$('bench-protocol').value!=='chat';
  if(value && !error && !active(current) && loadedConnection===fingerprint()) save(value);
}
function filtered() {const q=$('bench-model-search').value.toLowerCase();return models.filter(x=>x.toLowerCase().includes(q));}
function renderModels() {
  $('bench-model-count').textContent=`${selected.size} / 10`;
  const result=filtered();
  $('bench-model-list').innerHTML=result.map(m=>`<label><input type="checkbox" value="${esc(m)}" ${selected.has(m)?'checked':''}><span>${esc(m)}</span></label>`).join('');
  if(models.length && !result.length) $('bench-model-list').textContent='没有匹配的模型。';
  updatePlan();
}
function invalidate() {
  modelEpoch++; modelController?.abort(); clearTimeout(modelTimer);
  models=[]; selected.clear(); desired=[]; loadedConnection=''; loadingModels=false;
  $('bench-model-message').textContent='连接已改变，正在等待重新读取模型…';
  renderModels();
  if(!active(current) && $('bench-url').value.trim()) modelTimer=setTimeout(loadModels,700);
}
async function loadModels() {
  if(active(current)||busy) return;
  clearTimeout(modelTimer); modelController?.abort();
  const epoch=++modelEpoch, connection=fingerprint();
  let url;
  try {url=validURL($('bench-url').value.trim());} catch(e) {$('bench-model-message').textContent=e.message;return;}
  const preserved=loadedConnection===connection?[...selected]:desired;
  loadedConnection=''; loadingModels=true; modelController=new AbortController();
  $('bench-model-message').textContent='正在通过本地服务读取模型，最长等待 30 秒…'; updatePlan();
  const controller=modelController, timeout=setTimeout(()=>controller.abort(),35000);
  try {
    const response=await api('/api/benchmark/models',{method:'POST',signal:controller.signal,body:JSON.stringify({url:$('bench-url').value.trim(),protocol:$('bench-protocol').value,apiKey:$('bench-key').value})});
    if(epoch!==modelEpoch || connection!==fingerprint()) return;
    models=response.models; loadedConnection=connection;
    selected=new Set(preserved.filter(x=>models.includes(x)).slice(0,10)); desired=[];
    if(!selected.size && shared.defaultModel && models.includes(shared.defaultModel)) selected.add(shared.defaultModel);
    $('bench-model-message').textContent=models.length?`读取到 ${models.length} 个模型。选择 1–10 个后开始。`:'接口返回空模型列表，请检查连接或上游权限。';
  } catch(e) {
    if(epoch!==modelEpoch) return;
    models=[]; selected.clear();
    $('bench-model-message').textContent=e.name==='AbortError'?'模型读取已取消或超时，请重试。':e.message;
  } finally {clearTimeout(timeout); if(epoch===modelEpoch) {loadingModels=false;renderModels();}}
}
function metrics(value) {
  const s=value?.stats, f=s?.firstText, c=s?.cache;
  const cards=[['测量成功',s?num(s.success,0):'—','',s?`失败 ${s.failed} · 取消 ${s.cancelled} · 成功率 ${pct(s.successRate)}`:'等待实际测量'],['平均首字',num(f?.mean),'ms',`有效样本 ${f?.count??0} · 仅成功流式正文`],['P95 首字',num(f?.p95),'ms',`有效样本 ${f?.count??0} · R-7 分位数`],['Token 缓存命中率',pct(c?.tokenHitRate),'',`有效 ${c?.valid??0} · 请求覆盖 ${pct(c?.requestCoverage)}`]];
  $('bench-metrics').innerHTML=cards.map(([name,value,unit,detail])=>`<section class="benchmark-metric"><h2>${name}</h2><strong>${esc(value)}<small>${unit}</small></strong><p>${esc(detail)}</p></section>`).join('');
}
function statsDetail(s) {
  return [['成功 / 完成',`${s.success} / ${s.completed}（${pct(s.successRate)}）`],['平均 / P95 首字',`${num(s.firstText.mean)} / ${num(s.firstText.p95)} ms · n=${s.firstText.count}`],['Token 缓存命中率',pct(s.cache.tokenHitRate)],['请求缓存命中比例',pct(s.cache.requestHitRate)],['请求 / 输入量覆盖',`${pct(s.cache.requestCoverage)} / ${pct(s.cache.inputCoverage)}`],['缓存有效 / 缺失 / 异常',`${s.cache.valid} / ${s.cache.missing} / ${s.cache.invalid}`],['缓存 / 配对输入 Token',`${num(s.cache.cachedTokens,0)} / ${num(s.cache.pairedInputTokens,0)}`],['测量窗口 / 输出 usage',`${num(s.measurementSeconds)} 秒 / ${s.outputUsageCount} 条`]];
}
const dl=rows=>`<dl>${rows.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>`;
function recordDetail(r) {
  return dl([['返回模型',r.returnedModel],['HTTP / 结束原因',`${r.statusCode??'—'} / ${r.finishReason??'—'}`],['实际流式',r.streamActual==null?'未知':r.streamActual?'是':'否（JSON 回退）'],['首字 / 推理首段',`${num(r.firstTextMs)} / ${num(r.firstReasoningMs)} ms`],['TTFB / 总耗时',`${num(r.ttfbMs)} / ${num(r.durationMs)} ms`],['输入目标 / 字符数',`${r.input.targetTokens} / ${r.input.characters}`],['实际输入 / 输出 Token',`${num(r.usage?.input,0)} / ${num(r.usage?.output,0)}`],['缓存 / 推理 Token',`${num(r.usage?.cached,0)} / ${num(r.usage?.reasoning,0)}`],['缓存有效性',({valid:'有效配对',missing:'未知（缺失）',invalid:'异常'})[r.cacheValid]],['开始 / 结束（上海时间）',`${stamp(r.startedAt)} / ${stamp(r.endedAt)}`],['输入 SHA-256',r.input.sha256],['nonce',r.input.nonce]])+`<p class="field-help">输入摘要（前 180 字符）</p><pre>${esc(r.input.preview)}</pre><p class="field-help">输出预览（最多 2048 字符）</p><pre>${esc(r.outputPreview || '尚无可见正文')}</pre>${r.error?`<p class="inline-error">${esc(r.error)}</p>`:''}`;
}
function renderRecords(value) {
  const rows=[...(value?.records||[])].sort((a,b)=>b.index-a.index).slice(0,50), root=$('bench-records');
  const ids=new Set(rows.map(r=>`${value.runId}:${r.id}`));
  for(const child of [...root.children]) if(!ids.has(child.dataset.record)) child.remove();
  for(const r of rows) {
    const id=`${value.runId}:${r.id}`;
    let detail=[...root.children].find(x=>x.dataset.record===id);
    if(!detail) {
      detail=document.createElement('details'); detail.className='benchmark-record';detail.dataset.record=id;
      detail.innerHTML='<summary></summary><div class="benchmark-record-detail"></div>';
      detail.open=expanded.has(id);
      detail.addEventListener('toggle',()=>{if(detail.open) expanded.add(id);else expanded.delete(id);});
    }
    const signature=JSON.stringify(r);
    if(detail.dataset.signature!==signature) {
      detail.querySelector('summary').innerHTML=`<span class="benchmark-record-title">#${r.index+1} · ${esc(r.model)}<small>${esc(modeName(r.mode))} · 并发 ${r.concurrency}${r.warmup?' · 预热':''} · ${stamp(r.startedAt)}</small></span><span>${num(r.firstTextMs)} ms</span>${badge(r.status)}`;
      detail.querySelector('.benchmark-record-detail').innerHTML=recordDetail(r);detail.dataset.signature=signature;
    }
    root.append(detail);
  }
  $('bench-records-empty').hidden=!!rows.length;
}
function render(value) {
  viewed=value; const has=!!value?.runId;
  $('bench-state').dataset.status=value?.status||'idle';$('bench-state').textContent=(historyId?'历史 · ':'')+statusName(value?.status||'idle');
  $('bench-current').hidden=!historyId;
  document.querySelectorAll('[data-export]').forEach(x=>x.disabled=!has||!online);
  metrics(value);
  if(has) {
    const p=value.progress;
    $('bench-progress').max=p.planned||1;$('bench-progress').value=p.completed;
    $('bench-progress-label').textContent=`总完成 ${p.completed} / ${p.planned}`;
    $('bench-progress-counts').innerHTML=`<span>测量 <strong>${p.measuredCompleted} / ${p.measuredPlanned}</strong></span><span>预热 <strong>${p.warmupCompleted} / ${p.warmupPlanned}</strong></span><span>在途 <strong>${p.inFlight}</strong></span><span>未派发 <strong>${p.unstarted}</strong></span>`;
    const c=value.current;
    $('bench-current-stage').textContent=c?`阶段 ${c.stageIndex+1} / ${value.stages.length} · ${c.model} · ${modeName(c.mode)} · 并发 ${c.concurrency} · 实际峰值 ${value.peakInFlight}`:`${statusName(value.status)} · 测量窗口 ${num(value.stats.measurementSeconds)} 秒 · 任务总耗时 ${num(value.durationMs/1000)} 秒（含预热）`;
    $('bench-run-info').textContent=`批次 ${value.runId} · ${stamp(value.startedAt)} Asia/Shanghai · ${value.config.protocol==='chat'?'Chat Completions':'Responses'} · ${value.config.models.join('、')} · 输入约 ${value.config.inputMinTokens}–${value.config.inputMaxTokens} Token`;
  }
  message('bench-run-error',value?.error);
  const stages=value?.stages||[];
  $('bench-results-label').textContent=active(value)?'暂时结果 · 每阶段独立测量':has?'预热不参与以下指标':'完成测量后按阶段比较';
  $('bench-stages').innerHTML=stages.map(s=>{const v=s.stats;return `<tr><td>${esc(s.model)}<small>${modeName(s.mode)}${s.warmupStatus?` · 预热${statusName(s.warmupStatus)}`:''}</small></td><td>${s.concurrency}<small>峰值 ${s.peakInFlight}</small></td><td>${v.success} / ${v.completed}<small>计划 ${v.planned} · 失败 ${v.failed} · 取消 ${v.cancelled}</small></td><td>${num(v.firstText.mean)} ms<small>n=${v.firstText.count}</small></td><td>${num(v.firstText.p95)} ms<small>n=${v.firstText.count}</small></td><td>${num(v.successRps,3)}</td><td>${num(v.outputTokensPerSecond)}<small>usage ${v.outputUsageCount}/${v.success}</small></td><td>${pct(v.cache.tokenHitRate)}<small>有效 ${v.cache.valid} · 覆盖 ${pct(v.cache.requestCoverage)}</small></td><td>${badge(s.status)}</td></tr>`;}).join('');
  $('bench-stages-empty').hidden=!!stages.length;
  $('bench-models-summary').innerHTML=(value?.models||[]).map(m=>`<article class="benchmark-model-summary"><h3>${esc(m.model)}</h3>${dl(statsDetail(m.stats))}</article>`).join('');
  $('bench-report').innerHTML=(value?.report||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  $('bench-report-empty').hidden=!!value?.report?.length;
  $('bench-distributions').hidden=!has;
  if(has) {
    const distributions=[['正文首字',value.stats.firstText],['推理首段',value.stats.firstReasoning],['响应头 TTFB',value.stats.ttfb],['请求总耗时',value.stats.duration]];
    $('bench-distribution-table').innerHTML='<table class="benchmark-table"><thead><tr>'+['时间（ms）','样本','均值','P50','P95','P99','最小','最大'].map(x=>`<th>${x}</th>`).join('')+'</tr></thead><tbody>'+distributions.map(([name,d])=>`<tr><td>${name}</td><td>${d.count}</td>${['mean','p50','p95','p99','min','max'].map(k=>`<td>${num(d[k])}</td>`).join('')}</tr>`).join('')+'</tbody></table>';
    $('bench-error-distribution').textContent='测量错误分布：'+(Object.entries(value.stats.errors.byStatus).map(([code,count])=>`${code==='network'?'网络/超时':'HTTP '+code}：${count}`).join('；')||'无')+`。取消 ${value.stats.cancelled} 次单独计数。`;
  }
  renderRecords(value); updatePlan();
}
async function refreshHistory() {
  try {
    const response=await api('/api/benchmark/runs');
    $('bench-history').innerHTML='<option value="">当前任务</option>'+response.runs.map(r=>`<option value="${esc(r.runId)}">${esc(stamp(r.startedAt))} · ${esc(statusName(r.status))} · ${esc(r.models.join(' / '))}</option>`).join('');
    $('bench-history').value=historyId;
  } catch(e) {message('bench-action-error',e.message);}
}
async function viewHistory(id) {
  historyId=id;const epoch=++historyEpoch;
  if(!id) {render(current);return;}
  try {const value=await api(`/api/benchmark/runs/${encodeURIComponent(id)}`);if(epoch===historyEpoch) render(value);}
  catch(e) {if(epoch===historyEpoch) message('bench-action-error',e.message);}
}
async function poll() {
  clearTimeout(pollTimer);
  if(pollPending) return;
  pollPending=true;const epoch=latestStateEpoch;let delay=1200;
  try {
    const value=await api('/api/benchmark/state',{timeout:10000});
    if(epoch!==latestStateEpoch) return;
    const prior=current; current=value;online=true;
    message('bench-connection-error','');
    $('connection').classList.remove('offline');$('connection-label').textContent='本地服务在线';
    if(active(value) && restoredRun!==value.runId) {
      restoredRun=value.runId; modelEpoch++;modelController?.abort();clearTimeout(modelTimer);loadingModels=false;
      fill(value.config);$('bench-key').value='';models=value.config.models;selected=new Set(models);loadedConnection='';renderModels();
      $('bench-model-message').textContent='已从后台恢复运行配置，密钥不会恢复到输入框。';
    }
    if(!historyId) render(value);
    if(prior.status!==value.status || prior.runId!==value.runId) {document.dispatchEvent(new CustomEvent('benchmark:state',{detail:value}));refreshHistory();}
    if(active(prior)&&!active(value)) {desired=[...selected];loadedConnection='';updatePlan();loadModels();}
  } catch(e) {
    if(epoch!==latestStateEpoch) return;
    $('connection').classList.add('offline');$('connection-label').textContent='等待重新连接';
    online=false;delay=e.retryAfter?e.retryAfter*1000:2500;
    message('bench-connection-error',`本地服务暂不可达，正在重连；后台任务状态尚未确认。${e.message}`);
  } finally {pollPending=false;updatePlan();pollTimer=setTimeout(poll,delay);}
}
$('benchmark-form').addEventListener('submit',async event=>{
  event.preventDefault();if($('bench-start').disabled)return;
  let value;
  try {value=config();parameters(value);validURL(value.url);if(!value.models.length||loadedConnection!==fingerprint()) throw new Error('请重新读取并选择当前连接的模型。');}
  catch(e){message('bench-validation',e.message);return;}
  busy=true;latestStateEpoch++;updatePlan();message('bench-action-error','');
  try {
    const run=await api('/api/benchmark/start',{method:'POST',body:JSON.stringify(value)});
    current=run;historyId='';historyEpoch++;$('bench-history').value='';restoredRun=run.runId;loadedConnection='';
    render(run);document.dispatchEvent(new CustomEvent('benchmark:state',{detail:run}));await refreshHistory();
  } catch(e) {message('bench-action-error',e.message);}
  finally {busy=false;updatePlan();poll();}
});
$('bench-stop').addEventListener('click',async()=>{
  if($('bench-stop').disabled)return;
  busy=true;latestStateEpoch++;updatePlan();
  try {await api('/api/benchmark/stop',{method:'POST',body:JSON.stringify({runId:current.runId})});current={...current,status:'stopping'};if(!historyId)render(current);else message('bench-action-error',`已请求停止当前后台批次 ${current.runId}；正在查看的历史不受影响。`);}
  catch(e) {message('bench-action-error',e.message);}
  finally {busy=false;updatePlan();poll();}
});
for(const id of ['bench-url','bench-key','bench-protocol']) $(id).addEventListener(id==='bench-protocol'?'change':'input',invalidate);
$('bench-model-load').addEventListener('click',loadModels);
$('bench-model-search').addEventListener('input',renderModels);
$('bench-model-list').addEventListener('change',event=>{const node=event.target;if(node.type!=='checkbox')return;if(node.checked&&selected.size>=10){node.checked=false;message('bench-action-error','最多选择 10 个模型。');return;}node.checked?selected.add(node.value):selected.delete(node.value);renderModels();});
$('bench-model-select').addEventListener('click',()=>{const list=filtered();for(const model of list){if(selected.size>=10)break;selected.add(model);}if(list.some(x=>!selected.has(x)))message('bench-action-error','最多选择 10 个模型，已勾选前 10 个可选结果。');renderModels();});
$('bench-model-clear').addEventListener('click',()=>{selected.clear();renderModels();});
$('bench-key-toggle').addEventListener('click',()=>{const visible=$('bench-key').type==='password';$('bench-key').type=visible?'text':'password';$('bench-key-toggle').textContent=visible?'隐藏':'显示';$('bench-key-toggle').setAttribute('aria-pressed',String(visible));});
$('bench-fields').addEventListener('input',updatePlan);$('bench-fields').addEventListener('change',updatePlan);
document.querySelectorAll('[data-levels]').forEach(x=>x.addEventListener('click',()=>{$('bench-levels').value=x.dataset.levels;updatePlan();}));
$('bench-use-shared').addEventListener('click',()=>{if(active(current))return;$('bench-url').value=shared.url;$('bench-protocol').value=shared.protocol;invalidate();});
document.addEventListener('benchmark:connection',event=>{
  const old=shared.revision;shared=event.detail;
  $('bench-shared-status').textContent=shared.url?`${shared.url} · ${shared.hasKey?'已配置内存密钥':'无密钥'}${shared.defaultModel?' · '+shared.defaultModel:''}`:'尚未配置，可从顶部「连接设置」保存。';
  if(old!==undefined&&old!==shared.revision&&!active(current)&&!$('bench-key').value)invalidate();
  updatePlan();
});
$('bench-history-refresh').addEventListener('click',refreshHistory);
$('bench-history').addEventListener('change',event=>viewHistory(event.target.value));
$('bench-current').addEventListener('click',()=>{$('bench-history').value='';viewHistory('');});
document.querySelectorAll('[data-export]').forEach(button=>button.addEventListener('click',async()=>{
  const identifier=viewed?.runId;if(!identifier)return;button.disabled=true;
  try {const format=button.dataset.export, blob=await api(`/api/benchmark/runs/${encodeURIComponent(identifier)}/export?format=${format}`,{download:true});const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`benchmark-${identifier}.${format}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  catch(e){message('bench-action-error',e.message);}finally{button.disabled=!online;}
}));
try {const saved=JSON.parse(localStorage.getItem(storage)||'null');if(saved){fill(saved);desired=Array.isArray(saved.models)?saved.models:[];}}catch{}
render(current);refreshHistory();poll();
if($('bench-url').value.trim()) modelTimer=setTimeout(loadModels,700);
