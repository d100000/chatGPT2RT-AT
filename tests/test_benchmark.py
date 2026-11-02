"""Benchmark acceptance: actual async requests against deterministic mock streams."""
import asyncio
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

import httpx

from rt_extractor.benchmark import ACTIVE, COOKIE, BenchmarkService, Conflict, StorageError, atomic_json
from rt_extractor.benchmark_config import configuration, endpoint, material, plan, redact
from rt_extractor.benchmark_protocol import SSE, discover_models, generate, merge_usage
from rt_extractor.benchmark_stats import aggregate, csv_export, distribution, html_export
from rt_extractor.web import create_app
from tests.benchmark_mock import Chunked, Provider, event
from tests.web_client import browser_headers

KEY = 'synthetic+key/with"escaping\\ONLY'


def data(**changes):
    return dict(url='http://upstream.test/proxy/v1', apiKey=KEY, models=['alpha'], concurrencyLevels=[1],
                requestsPerStage=2, inputMinTokens=64, inputMaxTokens=80, modes=['unique'], timeoutSeconds=2, **changes) if not changes else {**data(),**changes}


def wait(check, timeout=8):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=check()
        if result:return result
        time.sleep(.015)
    raise AssertionError('等待本地 mock 任务超时')


class ConfigurationTests(unittest.TestCase):
    def test_url_normalization_and_private_query_rejection(self):
        for tail in ['', '/responses', '/response', '/chat/completions', '/models', '/']:
            self.assertEqual(endpoint('https://example.test/proxy/v1'+tail+'?api-version=2025&region=local','chat'),'https://example.test/proxy/v1/chat/completions?api-version=2025&region=local')
        self.assertEqual(endpoint('https://example.test'),'https://example.test/v1/responses')
        for value in ['file:///x','https://user:pass@host','https://host/#','https://host/?api_key=x','https://host/?access%5Ftoken=x','https://host/?signature=x','https://host:999999']:
            with self.subTest(url=value),self.assertRaises(ValueError):endpoint(value)

    def test_plan_example_and_concurrency_floor(self):
        config,_=configuration(data(models=['alpha','beta'],modes=['unique','shared-prefix'],concurrencyLevels='10，1 5',requestsPerStage=20))
        p=plan(config)
        self.assertEqual((p['stages'],p['measuredPlanned'],p['warmupPlanned'],p['planned']),(12,240,6,246))
        config,_=configuration(data(concurrencyLevels=[5],requestsPerStage=1))
        self.assertEqual(plan(config)['measuredPlanned'],5)

    def test_invalid_configs(self):
        for change in [dict(concurrencyLevels='1, 1.5'),dict(concurrencyLevels=[True]),dict(concurrencyLevels=[1]*9),dict(modes=[]),dict(inputMinTokens=64,inputMaxTokens=63),dict(models=['bad\n']),dict(models=['a']*11),dict(timeoutSeconds=0),dict(requestsPerStage='20'),dict(reasoningEffort='foo'),dict(outputTokenField='foo'),dict(maxOutputTokens=1.5),dict(models=list('abcdefghij'),modes=['unique','shared-prefix'],concurrencyLevels=[100,101,102,103,104,105,106,107],requestsPerStage=200)]:
            with self.subTest(change=change),self.assertRaises(ValueError):configuration(data(**change))

    def test_deterministic_model_independent_inputs(self):
        a,_=configuration(data(models=['alpha']));b,_=configuration(data(models=['beta']))
        for mode in ['unique','shared-prefix']:
            text,meta=material('seed',a,mode,0,1)
            self.assertEqual((text,meta),material('seed',b,mode,0,1))
            other,other_meta=material('seed',a,mode,0,2)
            self.assertNotEqual(meta['nonce'],other_meta['nonce'])
            self.assertTrue(64<=meta['targetTokens']<=80)
            if mode=='shared-prefix':
                self.assertEqual(text.split('\n')[:2],other.split('\n')[:2])
                warm,_=material('seed',a,mode,0,-1,True)
                self.assertEqual(text.split('\n')[:2],warm.split('\n')[:2])
                self.assertGreaterEqual(len(text.split('\n')[1].split()),64)
            else:self.assertTrue(text.startswith('Request nonce:'))

    def test_redaction_encoded_and_json(self):
        text=KEY+' '+quote(KEY,safe='')+' '+json.dumps(KEY)[1:-1]
        self.assertEqual(redact(text,KEY),'[REDACTED] [REDACTED] [REDACTED]')


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, model='alpha', protocol='responses', handler=None, **changes):
        config,_=configuration(data(models=[model],protocol=protocol,**changes))
        provider=Provider()
        transport=httpx.MockTransport(handler) if handler else provider.transport()
        async with httpx.AsyncClient(transport=transport,follow_redirects=False) as client:
            record={}
            await generate(client,config,KEY,model,'test input',record)
            return record

    async def test_both_protocols_timing_reasoning_and_usage(self):
        for protocol in ['chat','responses']:
            record=await self.request(protocol=protocol)
            self.assertEqual(record['status'],'success',record)
            self.assertLess(record['ttfbMs'],record['firstReasoningMs'])
            self.assertLess(record['firstReasoningMs'],record['firstTextMs'])
            self.assertLess(record['firstTextMs'],record['durationMs'])
            self.assertEqual(record['usage'],dict(input=100,output=3,total=103,cached=40,reasoning=1))
            self.assertEqual(record['cacheValid'],'valid')
            self.assertIn('ACK 测试',record['outputPreview'])

    async def test_json_does_not_invent_ttft(self):
        for protocol in ['chat','responses']:
            record=await self.request('json',protocol)
            self.assertEqual(record['status'],'success')
            self.assertFalse(record['streamActual'])
            self.assertIsNone(record['firstTextMs'])

    async def test_missing_zero_invalid_cache(self):
        for model,expected in [('zero','valid'),('missing','missing'),('invalid','invalid')]:
            record=await self.request(model)
            self.assertEqual(record['cacheValid'],expected)
            if model=='zero':self.assertEqual(record['usage']['cached'],0)

    async def test_failures_no_fake_success_and_partial_preview(self):
        for model in ['429','503','broken','tool']:
            record=await self.request(model)
            self.assertEqual(record['status'],'error',record)
            if model=='broken':self.assertIn('ACK',record['outputPreview'])

    async def test_total_timeout_includes_body_and_keeps_partial(self):
        started=time.monotonic()
        record=await self.request('hang',timeoutSeconds=1)
        self.assertEqual(record['status'],'error')
        self.assertIn('总超时',record['error'])
        self.assertIn('ACK',record['outputPreview'])
        self.assertLess(time.monotonic()-started,1.5)

    async def test_no_text_empty_json_invalid_event_and_no_done(self):
        cases=[('chat',[event({'choices':[{'delta':{'content':'  '},'finish_reason':'stop'}]}),b'data: [DONE]\n\n']),
               ('chat',[event({'choices':[{'delta':{'content':'ok'},'finish_reason':'stop'}]})]),
               ('responses',[b'data: nope\n\n']),
               ('responses',[event({'type':'response.incomplete','response':{'status':'incomplete'}})])]
        for protocol,packets in cases:
            async def handler(request):return httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Chunked(packets))
            record=await self.request(protocol=protocol,handler=handler)
            self.assertEqual(record['status'],'error',record)

    async def test_completed_text_is_not_first_delta(self):
        packets=[event({'type':'response.completed','response':{'status':'completed','output_text':'ACK','usage':{'input_tokens':0,'output_tokens':0}}})]
        async def handler(request):return httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Chunked(packets))
        record=await self.request(handler=handler)
        self.assertEqual(record['status'],'success')
        self.assertIsNone(record['firstTextMs'])
        self.assertEqual(record['usage']['input'],0)

    async def test_limit_redirect_and_echo(self):
        async def oversized(request):return httpx.Response(200,content=b'x'*(8*1024*1024+1))
        record=await self.request(handler=oversized)
        self.assertEqual(record['status'],'error');self.assertIn('8 MiB',record['error'])
        async def redirect(request):return httpx.Response(302,headers={'Location':'http://elsewhere.test'})
        record=await self.request(handler=redirect)
        self.assertEqual(record['statusCode'],302);self.assertEqual(record['status'],'error')
        record=await self.request('echo')
        self.assertNotIn(KEY,json.dumps(record));self.assertIn('[REDACTED]',record['outputPreview'])

    async def test_fragmented_secret_and_whitespace_never_create_fake_ttft(self):
        encoded=quote(KEY,safe='')
        packets=[event({'type':'response.output_text.delta','delta':'\u200b \t'})]
        for text in [KEY[:10],KEY[10:], ' ', encoded[:15],encoded[15:]]:
            packets.append(event({'type':'response.output_text.delta','delta':text}))
        packets.append(event({'type':'response.completed','response':{'status':'completed'}}))
        packets.append(b': final heartbeat')
        async def handler(request):return httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Chunked(packets))
        r=await self.request(handler=handler)
        self.assertEqual(r['status'],'success')
        self.assertNotIn(KEY,r['outputPreview']);self.assertNotIn(encoded,r['outputPreview'])
        self.assertGreaterEqual(r['firstTextMs'],12)
        self.assertLessEqual(len(r['outputPreview']),2048)

    async def test_discovery_rejects_invalid_directory(self):
        for payload in [{}, {'data':{}}, []]:
            async def handler(request):return httpx.Response(200,json=payload)
            with self.assertRaisesRegex(ValueError,'data'):
                await discover_models('http://upstream.test','responses','',httpx.MockTransport(handler))
        async def handler(request):return httpx.Response(200,content=b'not json')
        with self.assertRaisesRegex(ValueError,'JSON'):
            await discover_models('http://upstream.test','chat','',httpx.MockTransport(handler))


class StatisticsTests(unittest.TestCase):
    def row(self,input=100,cached=0,status='success',warmup=False,ttft=10,**changes):
        r=dict(status=status,warmup=warmup,streamActual=True,firstTextMs=ttft,usage={},cacheValid='missing')
        merge_usage(r,dict(input_tokens=input,output_tokens=10,input_tokens_details=dict(cached_tokens=cached)))
        r.update(changes);return r

    def test_r7_zero_and_valid_samples(self):
        self.assertEqual(distribution([0,10])['p95'],9.5)
        s=aggregate([self.row(ttft=0),self.row(ttft=100,warmup=True),self.row(ttft=50,status='error'),self.row(ttft=30,streamActual=False)],2,3)
        self.assertEqual(s['firstText']['count'],1);self.assertEqual(s['firstText']['mean'],0)
        self.assertEqual(s['successRps'],1);self.assertEqual(s['outputTokensPerSecond'],10)
        self.assertEqual(s['successRate'],2/3)

    def test_weighted_paired_cache_and_coverage(self):
        rows=[self.row(input=100,cached=100),self.row(input=900,cached=0),self.row(input=1000,cached=None),self.row(input=3,cached=4)]
        c=aggregate(rows,1,4)['cache']
        self.assertEqual(c['tokenHitRate'],.1);self.assertEqual(c['requestHitRate'],.5)
        self.assertEqual(c['requestCoverage'],.5);self.assertEqual(c['inputCoverage'],1000/2003)
        self.assertEqual((c['valid'],c['missing'],c['invalid']),(2,1,1))
        for bad in [-1,1.1,'3',2**53,True]:
            self.assertEqual(self.row(cached=bad)['cacheValid'],'invalid')
        c=aggregate([self.row(input=0,cached=0)],1,1)['cache']
        self.assertIsNone(c['tokenHitRate']);self.assertEqual(c['requestHitRate'],0)

    def test_usage_updates_merge_not_add(self):
        r={};merge_usage(r,{'prompt_tokens':10});merge_usage(r,{'completion_tokens':4});merge_usage(r,{'completion_tokens':5})
        self.assertEqual(r['usage']['input'],10);self.assertEqual(r['usage']['output'],5);self.assertEqual(r['usage']['total'],15)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.provider=Provider();self.service=BenchmarkService(self.temp.name,transport=self.provider.transport());self.owner='a'*64
    def tearDown(self):
        if self.service.runtime:
            self.service.stop(self.owner);self.service.runtime['thread'].join(3) if self.service.runtime else None
        self.temp.cleanup()
    def start(self,**changes):
        payload=data(**changes);self.service.models(self.owner,payload);return self.service.start(self.owner,payload)
    def done(self,identifier):
        wait(lambda:not self.service.runtime)
        return self.service.get(self.owner,identifier,full=True)

    def test_order_bounds_floor_warmups_and_identical_model_inputs(self):
        run=self.start(models=['alpha','beta'],modes=['unique','shared-prefix'],concurrencyLevels=[3,1],requestsPerStage=1)
        view=self.done(run['runId'])
        self.assertEqual(view['status'],'completed',view['error'])
        self.assertEqual(view['progress']['completed'],20);self.assertEqual(view['stats']['success'],16)
        self.assertEqual(view['warmup']['success'],4)
        self.assertLessEqual(self.provider.peak,3);self.assertEqual(self.provider.peak,3)
        sequence=[(r['model'],r['mode'],r['concurrency']) for r in view['records']]
        self.assertEqual(sequence,sorted(sequence,key=lambda x:(['alpha','beta'].index(x[0]),['unique','shared-prefix'].index(x[1]),x[2])))
        rows=view['records'];half=len(rows)//2
        self.assertEqual([r['input'] for r in rows[:half]],[r['input'] for r in rows[half:]])
        self.assertAlmostEqual(view['stats']['measurementSeconds'],sum(s['measurementMs']/1000 for s in view['stages']))
        self.assertGreater(sum(r['durationMs'] for r in rows if not r['warmup'])/1000,view['stats']['measurementSeconds'])
        self.assertIsNone(self.service.runtime)
        for path in Path(self.temp.name).rglob('*.json'):
            text=path.read_text();self.assertNotIn(KEY,text);self.assertNotIn('apiKey',text)

    def test_stop_no_dispatch_conflict_refresh_and_partial_export(self):
        run=self.start(models=['hang'],concurrencyLevels=[3],requestsPerStage=20,timeoutSeconds=10)
        wait(lambda:len(self.provider.calls)==3)
        with self.assertRaises(Conflict):self.service.start(self.owner,data())
        self.assertEqual(self.service.state(self.owner)['runId'],run['runId'])
        self.service.stop(self.owner)
        view=self.done(run['runId'])
        self.assertEqual(view['status'],'stopped');self.assertEqual(len(self.provider.calls),3)
        self.assertEqual(view['stats']['cancelled'],3);self.assertEqual(view['stats']['unstarted'],17)
        self.assertEqual(view['progress']['completed'],3)
        self.assertEqual(len(list(csv.reader(io.StringIO(csv_export(view))))),4)
        with self.assertRaises(KeyError):self.service.get('b'*64,run['runId'])

    def test_failed_warmup_continues_but_is_excluded(self):
        original=self.provider.handle
        warmed=False
        async def handler(request):
            nonlocal warmed
            if request.method=='POST' and not warmed:
                warmed=True
                return httpx.Response(503,json={'error':'warmup failed'})
            return await original(request)
        self.service.transport=httpx.MockTransport(handler)
        run=self.start(modes=['shared-prefix'])
        view=self.done(run['runId'])
        self.assertEqual(view['warmup']['failed'],1)
        self.assertEqual(view['stats']['failed'],0)
        self.assertEqual(view['stats']['success'],2)
        self.assertEqual(view['stages'][0]['warmupStatus'],'error')
        self.assertEqual(view['progress']['completed'],3)

    def test_unjournaled_request_is_left_unstarted(self):
        self.service.models(self.owner,data())
        with patch.object(self.service,'persist_record',side_effect=StorageError('disk')):
            run=self.service.start(self.owner,data())
            view=self.done(run['runId'])
        self.assertEqual(view['status'],'error')
        self.assertEqual(self.provider.calls,[])
        self.assertEqual(view['records'],[])
        self.assertEqual(view['progress']['completed'],0)
        self.assertEqual(view['progress']['unstarted'],2)
        self.assertEqual(view['peakInFlight'],0)

    def test_history_restart_no_resume(self):
        run=self.start();view=self.done(run['runId']);identifier=run['runId']
        path=Path(self.temp.name)/identifier/'summary.json';stored=json.loads(path.read_text());stored['status']='running';stored['stages'][0]['status']='running';atomic_json(path,stored)
        row_path=next((path.parent/'records').glob('*.json'));r=json.loads(row_path.read_text());r['status']='running';atomic_json(row_path,r)
        restarted=BenchmarkService(self.temp.name,transport=self.provider.transport())
        recovered=restarted.get(self.owner,identifier,full=True)
        self.assertEqual(recovered['status'],'interrupted');self.assertEqual(recovered['stats']['cancelled'],1)
        self.assertIsNone(restarted.runtime);self.assertEqual(len(self.provider.calls),2)
        self.assertEqual(recovered['stages'][0]['status'],'interrupted')

    def test_shared_key_origin_override_and_list_proof(self):
        self.service.save_connection(self.owner,dict(url='https://upstream.test',apiKey=KEY))
        for url,expected in [('https://upstream.test/path',KEY),('http://upstream.test',''),('https://upstream.test:444',''),('https://different.test','')]:
            self.assertEqual(self.service.resolve(self.owner,dict(url=url,apiKey=''))[2],expected)
        self.assertEqual(self.service.resolve(self.owner,dict(url='https://upstream.test',apiKey='manual'))[2],'manual')
        self.assertNotIn(KEY,json.dumps(self.service.connection(self.owner)))
        self.service.models(self.owner,data())
        with self.assertRaisesRegex(ValueError,'模型列表'):self.service.start(self.owner,data(apiKey='changed'))

    def test_storage_failure_before_and_during(self):
        self.service.models(self.owner,data())
        with patch('rt_extractor.benchmark.atomic_json',side_effect=StorageError('disk')):
            with self.assertRaises(StorageError):self.service.start(self.owner,data())
        self.assertEqual(self.provider.calls,[])
        original=self.service.persist_record
        def fail_on_finish(run,record):
            if record['status']!='running':raise StorageError('disk')
            return original(run,record)
        with patch.object(self.service,'persist_record',side_effect=fail_on_finish):
            run=self.start(concurrencyLevels=[1],requestsPerStage=10)
            view=self.done(run['runId'])
        self.assertEqual(view['status'],'error');self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(len(view['records']),1)

    def test_exports_escaping_all_records_and_nulls(self):
        run=self.start(models=['echo'],requestsPerStage=55,concurrencyLevels=[10]);view=self.done(run['runId'])
        self.assertEqual(len(view['records']),55);self.assertEqual(len(self.service.state(self.owner)['records']),50)
        view['records'][0]['model']='=HYPERLINK("x")';view['records'][0]['error']='<script>bad</script>';view['records'][0]['status']='error'
        html=html_export(view);self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html);self.assertIn('Asia/Shanghai',html)
        csv_value=csv_export(view);self.assertTrue(csv_value.startswith('\ufeff'));self.assertIn("'=HYPERLINK",csv_value)
        self.assertEqual(len(list(csv.reader(io.StringIO(csv_value)))),56)
        self.assertNotIn(KEY,csv_value+html+json.dumps(view))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.provider=Provider();self.app=create_app(benchmark_dir=self.temp.name,benchmark_transport=self.provider.transport());self.app.testing=True
        self.client=self.app.test_client();self.client.get('/benchmark');self.headers=browser_headers(self.app,self.client);self.service=self.app.extensions['benchmark']
    def tearDown(self):
        if self.service.runtime:
            runtime=self.service.runtime;self.service.stop(runtime['run']['owner']);runtime['thread'].join(3)
        self.temp.cleanup()
    def test_routes_workflow_and_isolation(self):
        self.assertIn('并发分析',self.client.get('/converter').text)
        response=self.client.post('/api/benchmark/models',json=data(),headers=self.headers)
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json['models'],sorted(set(response.json['models'])))
        response=self.client.post('/api/benchmark/start',json=data(),headers=self.headers)
        self.assertEqual(response.status_code,201,response.text);identifier=response.json['runId']
        wait(lambda:not self.service.runtime)
        self.assertEqual(self.client.get('/api/benchmark/state',headers=self.headers).json['status'],'completed')
        self.assertEqual(len(self.client.get('/api/benchmark/runs',headers=self.headers).json['runs']),1)
        for fmt in ['html','csv','json']:
            response=self.client.get(f'/api/benchmark/runs/{identifier}/export?format={fmt}',headers=self.headers)
            self.assertEqual(response.status_code,200);self.assertNotIn(KEY,response.text)
        self.assertEqual(self.client.get('/api/benchmark/runs/invalid',headers=self.headers).status_code,404)
        self.assertEqual(self.client.get('/api/benchmark/start',headers=self.headers).status_code,405)
        other=self.app.test_client();other.get('/benchmark');headers=browser_headers(self.app,other)
        self.assertEqual(other.get(f'/api/benchmark/runs/{identifier}',headers=headers).status_code,404)
    def test_protection_content_limit_and_no_invalid_upstream(self):
        self.assertEqual(self.client.post('/api/benchmark/start',json=data()).status_code,403)
        self.assertEqual(self.client.post('/api/benchmark/start',json=data(),headers={**self.headers,'Origin':'https://foreign.test'}).status_code,403)
        self.assertEqual(self.client.post('/api/benchmark/start',data='{}',headers=self.headers).status_code,400)
        self.assertEqual(self.client.post('/api/benchmark/start',json=data(requestsPerStage=0),headers=self.headers).status_code,400)
        self.assertEqual(self.client.post('/api/benchmark/start',json={'url':'x'*140000},headers=self.headers).status_code,413)
        self.assertEqual(self.provider.calls,[])
    def test_shared_connection_in_workspace(self):
        response=self.client.post('/api/workspace/connection',json={'url':'http://upstream.test','apiKey':KEY},headers=self.headers)
        self.assertEqual(response.status_code,200);self.assertNotIn(KEY,response.text);self.assertTrue(response.json['connection']['hasKey'])
        self.assertTrue(self.client.get('/api/workspace',headers=self.headers).json['connection']['hasKey'])
        response=self.client.delete('/api/workspace/connection',json={},headers=self.headers)
        self.assertFalse(response.json['connection']['hasKey'])


if __name__=='__main__':unittest.main()
