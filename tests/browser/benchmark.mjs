// BENCHMARK_PYTHON=/path/python PLAYWRIGHT_MODULE=/path/playwright/index.mjs node tests/browser/benchmark.mjs
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const { chromium }=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const server=spawn(process.env.BENCHMARK_PYTHON || 'python3',['-m','tests.benchmark_browser_server'],{cwd:fileURLToPath(new URL('../..',import.meta.url)),stdio:['ignore','pipe','pipe']});
let logs='';server.stderr.on('data',value=>logs+=value);
const lines=createInterface({input:server.stdout});
const config=await new Promise((resolve,reject)=>{lines.once('line',line=>resolve(JSON.parse(line)));server.once('exit',code=>reject(new Error(`Server ${code}: ${logs}`)));});
const artifacts=await mkdtemp(join(tmpdir(),'benchmark-ui-'));
let browser;
try {browser=await chromium.launch({headless:true,...(process.env.BENCHMARK_CHROME?{executablePath:process.env.BENCHMARK_CHROME}:{})});}
catch(error){server.kill();throw error;}
const context=await browser.newContext({viewport:{width:1680,height:1050},acceptDownloads:true});
await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
const page=await context.newPage(),errors=[];
page.on('pageerror',error=>errors.push(error.message));
const ready=()=>page.waitForFunction(()=>!document.querySelector('#bench-start').disabled);
const done=()=>page.waitForFunction(()=>document.querySelector('#bench-state').dataset.status==='completed',{},{timeout:20000});
const requests=async()=> (await (await page.request.get(config.url+'/__test__/requests')).json());
async function models(ids){await page.locator('#bench-model-clear').click();for(const id of ids)await page.locator(`#bench-model-list input[value="${id}"]`).check();}
async function smallPlan(){await page.locator('#bench-levels').fill('1, 3');await page.locator('#bench-requests').fill('3');await page.locator('#bench-input-min').fill('64');await page.locator('#bench-input-max').fill('80');}
try {
  await page.goto(config.url+'/benchmark');
  if(await page.locator('#site-notice-dialog').isVisible())await page.locator('#site-notice-dialog').evaluate(x=>x.close());
  assert.equal(await page.locator('a[href="/benchmark"][aria-current="page"]').count(),1);
  await page.screenshot({path:join(artifacts,'desktop-empty.png'),fullPage:true});
  await page.locator('#shared-connection-open').click();await page.locator('#shared-url').fill(config.provider);await page.locator('#shared-key').fill('ui-synthetic-key-only');await page.locator('#shared-model').fill('alpha');await page.locator('#shared-connection-form button[type=submit]').click();
  await page.locator('#shared-connection-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(()=>!document.querySelector('#bench-use-shared').disabled);
  await page.locator('#bench-use-shared').click();await page.locator('#bench-model-list input[value="alpha"]').waitFor();
  await models(['alpha','beta']);
  assert.match(await page.locator('#bench-plan').innerText(),/246/);
  await smallPlan();await ready();
  await page.locator('#bench-start').click();
  await page.waitForFunction(()=>document.querySelector('#bench-fields').disabled);
  await page.screenshot({path:join(artifacts,'desktop-running.png'),fullPage:true});
  await page.reload();await page.waitForFunction(()=>document.querySelector('#bench-run-info').textContent.includes('批次'));
  await done();
  assert.match(await page.locator('#bench-progress-label').innerText(),/28 \/ 28/);
  assert.match(await page.locator('#bench-metrics').innerText(),/24/);
  assert.equal(await page.locator('#bench-stages tr').count(),8);
  assert.ok((await requests()).peak<=3);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.locator('.benchmark-record summary').first().click();
  await page.waitForTimeout(1500);
  assert.equal(await page.locator('.benchmark-record').first().getAttribute('open')!==null,true);
  for(const format of ['json','csv','html']) {
    const pending=page.waitForEvent('download');await page.locator(`[data-export="${format}"]`).click();const download=await pending,path=join(artifacts,`report.${format}`);await download.saveAs(path);const content=await readFile(path,'utf8');assert.ok(!content.includes('ui-synthetic-key-only'));
    if(format==='json'){const report=JSON.parse(content);assert.equal(report.records.length,28);assert.equal(report.stats.success,24);}
    if(format==='html'){assert.ok(!content.includes('<script'));const offline=await browser.newContext({offline:true}),tab=await offline.newPage();await tab.goto(pathToFileURL(path).href);assert.equal(await tab.locator('h1').innerText(),'大模型接口 · 并发分析报告');await offline.close();}
  }
  assert.equal(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}).includes('ui-synthetic-key-only')),false);
  await page.screenshot({path:join(artifacts,'desktop-results.png'),fullPage:true});
  // Service loss disables controls without inventing a terminal task state, then reconnects.
  await page.route('**/api/benchmark/state',route=>route.abort());
  await page.waitForFunction(()=>!document.querySelector('#bench-connection-error').hidden);
  assert.equal(await page.locator('#bench-start').isDisabled(),true);
  await page.unroute('**/api/benchmark/state');
  await page.waitForFunction(()=>document.querySelector('#bench-connection-error').hidden);
  // Chat + JSON fallback and upstream failure count are separate from success.
  await page.locator('#bench-protocol').selectOption('chat');await page.locator('#bench-model-list input[value="json"]').waitFor();await models(['json','429']);
  await page.locator('#bench-levels').fill('1');await page.locator('#bench-requests').fill('1');await page.locator('#bench-shared-prefix').uncheck();await ready();await page.locator('#bench-start').click();await done();
  await page.waitForFunction(()=>document.querySelector('#bench-report').textContent.includes('JSON 回退 1'));
  assert.match(await page.locator('#bench-metrics').innerText(),/失败 1/);
  // A slow task survives navigation; stopping while viewing history targets current run.
  await page.locator('#bench-model-list input[value="hang"]').waitFor();await models(['hang']);await page.locator('#bench-levels').fill('3');await page.locator('#bench-requests').fill('20');await ready();await page.locator('#bench-start').click();
  await page.waitForFunction(()=>document.querySelector('#bench-fields').disabled);
  await page.goto(config.url+'/converter');await page.locator('#benchmark-task-badge').waitFor({state:'visible'});
  await page.goto(config.url+'/benchmark');await page.waitForFunction(()=>document.querySelector('#bench-fields').disabled);
  await page.waitForFunction(()=>document.querySelectorAll('#bench-history option').length>=4);
  const historical=await page.locator('#bench-history option').last().getAttribute('value');
  await page.locator('#bench-history').selectOption(historical);await page.waitForFunction(()=>document.querySelector('#bench-state').textContent.startsWith('历史'));
  await page.locator('#bench-stop').click();await page.waitForFunction(()=>!document.querySelector('#bench-fields').disabled);
  await page.locator('#bench-current').click();await page.waitForFunction(()=>document.querySelector('#bench-state').dataset.status==='stopped');
  const count=(await requests()).count;await page.reload();await page.waitForTimeout(1600);assert.equal((await requests()).count,count);
  assert.match(await page.locator('#bench-progress-label').innerText(),/3 \/ 20/);
  await page.setViewportSize({width:390,height:844});await page.evaluate(()=>scrollTo(0,0));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.screenshot({path:join(artifacts,'mobile-partial.png'),fullPage:true});
  await page.setViewportSize({width:320,height:740});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,artifacts,checks:'Responses + Chat real loopback HTTP; plan; shared key; reload; cross-tool background; partial stop/history; all exports; desktop/mobile; no JS errors'}));
} catch(error) {
  await page.screenshot({path:join(artifacts,'failure.png'),fullPage:true});
  console.error(JSON.stringify({artifacts,errors,ui:await page.locator('#bench-action-error').textContent(),validation:await page.locator('#bench-validation').textContent(),logs:logs.slice(-4000)}));throw error;
} finally {await browser.close();server.kill('SIGTERM');}
