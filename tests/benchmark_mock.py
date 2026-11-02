"""Local synthetic streams for benchmark tests; no paid provider calls."""
import asyncio
import json
import threading

import httpx

MODELS = ['alpha', 'beta', 'reasoning', 'json', 'zero', 'missing', 'invalid', '429', '503', 'broken', 'hang', 'tool', 'echo']


def event(payload, *, multiline=False):
    text = json.dumps(payload, ensure_ascii=False)
    if multiline:
        text = text.replace(', ', ',\ndata: ', 1)
    return ('data: ' + text + '\r\n\r\n').encode()


def fixture(body, key=''):
    model = body['model']
    chat = 'messages' in body
    usage = {'prompt_tokens': 100, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 40}, 'completion_tokens_details': {'reasoning_tokens': 1}} if chat else {'input_tokens': 100, 'output_tokens': 3, 'input_tokens_details': {'cached_tokens': 40}, 'output_tokens_details': {'reasoning_tokens': 1}}
    details = 'prompt_tokens_details' if chat else 'input_tokens_details'
    if model == 'zero': usage[details]['cached_tokens'] = 0
    if model == 'missing': usage.pop(details)
    if model == 'invalid': usage[details]['cached_tokens'] = '40'
    text = 'ACK 测试' if model != 'echo' else 'secret ' + key + ' <script>alert(1)</script>'
    if chat:
        final = {'model':model,'choices':[{'index':0,'message':{'content':text},'finish_reason':'stop'}], 'usage':usage}
        packets = [event({'choices':[{'delta':{'role':'assistant','content':'  '}}]}),
                   event({'choices':[{'delta':{'reasoning_content':'thinking'}}]}),
                   event({'model':model, 'choices':[{'delta':{'content':text}}]}, multiline=True),
                   event({'choices':[{'delta':{},'finish_reason':'length'}]}),
                   event({'choices':[], 'usage':usage}), b'data: [DONE]\r\n\r\n']
        if model == 'tool': packets.insert(0, event({'choices':[{'delta':{'tool_calls':[{'id':'t'}]}}]}))
    else:
        final = {'model':model, 'status':'completed', 'output':[{'type':'message','content':[{'type':'output_text','text':text}]}], 'usage':usage}
        packets = [event({'type':'response.created','response':{'model':model}}),
                   event({'type':'response.reasoning_summary_text.delta','delta':'thinking'}),
                   event({'type':'response.output_text.delta','delta':text}, multiline=True),
                   event({'type':'response.completed','response':final})]
        if model == 'tool': packets.insert(0,event({'type':'response.output_item.added','item':{'type':'function_call'}}))
    if model == 'broken': packets=packets[:3]
    return final, [b': heartbeat\r\n\r\n', *packets]


class Chunked(httpx.AsyncByteStream):
    def __init__(self, packets, provider=None, hang=False, delay=.008):
        self.packets,self.provider,self.hang,self.delay=packets,provider,hang,delay
        self.closed=False

    async def __aiter__(self):
        for packet in self.packets:
            await asyncio.sleep(self.delay)
            # Split CRLF, UTF-8, JSON tokens and event boundaries across packets.
            for i in range(0,len(packet),7):
                yield packet[i:i+7]
        if self.hang:
            await asyncio.sleep(600)

    async def aclose(self):
        if self.provider and not self.closed:
            with self.provider.lock:
                self.provider.inflight-=1
        self.closed=True


class Provider:
    def __init__(self, delay=.008):
        self.lock=threading.Lock()
        self.calls=[]
        self.inflight=0
        self.peak=0
        self.delay=delay
        self.models_payload={'data':[{'id':m} for m in MODELS]+[{'id':'alpha'},{'id':'bad\nmodel'},{'id':23}]}

    async def handle(self, request):
        if request.method=='GET':
            return httpx.Response(200, json=self.models_payload)
        body=json.loads(request.content)
        with self.lock:
            self.calls.append({'body':body,'path':request.url.path,'auth':request.headers.get('Authorization')})
        if body['model'] in {'429','503'}:
            return httpx.Response(int(body['model']), json={'error':'synthetic failure'})
        final,packets=fixture(body,request.headers.get('Authorization','').removeprefix('Bearer '))
        if body['model']=='json':
            return httpx.Response(200,json=final)
        with self.lock:
            self.inflight+=1;self.peak=max(self.peak,self.inflight)
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Chunked(packets,self,body['model']=='hang',self.delay))

    def transport(self):
        return httpx.MockTransport(self.handle)
