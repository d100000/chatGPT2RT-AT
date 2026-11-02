import { api, active } from './benchmark-common.mjs';
const $ = id => document.getElementById(id);
const dialog = $('shared-connection-dialog');
let connection = {}, workspaceState = {}, pending = false, timer, generation = 0;
function publish(value) {
  workspaceState = value;
  connection = value.connection || {};
  const badge = $('benchmark-task-badge');
  if (badge) { badge.hidden=!value.benchmark; badge.textContent=value.benchmark?.status==='stopping'?'停止中':'进行中'; badge.dataset.active=String(!!value.benchmark); }
  const count = $('workspace-active-count');
  if (count) count.textContent=`后台任务 ${value.activeTasks || 0}`;
  document.dispatchEvent(new CustomEvent('benchmark:connection',{detail:connection}));
}
async function refresh() {
  if (pending) return;
  clearTimeout(timer); pending=true;
  const epoch = generation;
  try { const value = await api('/api/workspace',{timeout:10000}); if(epoch===generation) publish(value); } catch { /* The active page handles loss of service. */ }
  finally { pending=false; timer=setTimeout(refresh,12000); }
}
$('shared-connection-open')?.addEventListener('click', async()=>{
  $('shared-connection-error').hidden=true;
  $('shared-url').value=connection.url || '';
  $('shared-protocol').value=connection.protocol || 'responses';
  $('shared-model').value=connection.defaultModel || '';
  $('shared-key').value='';
  $('shared-key-status').textContent=connection.hasKey?'已配置内存密钥。保存时请重新填写，留空保存将移除现有密钥。':'尚未配置密钥，无需鉴权的接口可以留空。';
  dialog.showModal();
});
$('shared-connection-close')?.addEventListener('click',()=>dialog.close());
dialog?.addEventListener('close',()=>{$('shared-key').value='';});
async function change(method, value) {
  const controls=[...dialog.querySelectorAll('button,input,select')]; controls.forEach(x=>x.disabled=true);
  try {
    const response = await api('/api/workspace/connection',{method,body:JSON.stringify(value)});
    generation++; publish({...workspaceState,connection:response.connection});
    $('shared-key').value=''; dialog.close(); await refresh();
  } catch(error) { $('shared-connection-error').textContent=error.message; $('shared-connection-error').hidden=false; }
  finally { controls.forEach(x=>x.disabled=false); }
}
$('shared-connection-form')?.addEventListener('submit',event=>{
  event.preventDefault();
  change('POST',{url:$('shared-url').value.trim(), protocol:$('shared-protocol').value, apiKey:$('shared-key').value, defaultModel:$('shared-model').value.trim()});
});
$('shared-connection-clear')?.addEventListener('click',()=>change('DELETE',{}));
document.addEventListener('benchmark:state', event=>{
  const value=event.detail, badge=$('benchmark-task-badge');
  if (badge) { badge.hidden=!active(value); badge.textContent=value.status==='stopping'?'停止中':'进行中'; badge.dataset.active=String(active(value)); }
  refresh();
});
refresh();
