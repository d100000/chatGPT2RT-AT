"""Isolated UI acceptance server + real loopback HTTP mock. Temporary history only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile
import threading
import time

from flask import jsonify
from werkzeug.serving import make_server

from rt_extractor.web import create_app
from tests.benchmark_mock import MODELS, fixture


@contextmanager
def mock_provider():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(json.dumps({'data':[{'id':m} for m in MODELS]}).encode())
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with self.server.lock:
                self.server.calls.append(body);self.server.active+=1;self.server.peak=max(self.server.peak,self.server.active)
            model=body['model']
            try:
                if model in {'429','503'}:
                    self.send_response(int(model));self.end_headers();self.wfile.write(b'{"error":"synthetic HTTP error"}');return
                final,packets=fixture(body,self.headers.get('Authorization','').removeprefix('Bearer '))
                self.send_response(200);self.send_header('Content-Type','application/json' if model=='json' else 'text/event-stream');self.end_headers()
                if model=='json':self.wfile.write(json.dumps(final).encode());return
                for packet in packets:
                    time.sleep(.025)
                    for i in range(0,len(packet),7):self.wfile.write(packet[i:i+7]);self.wfile.flush()
                if model=='hang':
                    # Heartbeats continue until the local client disconnects.
                    for _ in range(120):
                        time.sleep(.1);self.wfile.write(b': heartbeat\n\n');self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
            finally:
                with self.server.lock:self.server.active-=1
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True;server.calls=[];server.active=0;server.peak=0;server.lock=threading.Lock()
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server,f'http://127.0.0.1:{server.server_port}/proxy/v1'
    finally:server.shutdown();server.server_close();thread.join(2)


def main():
    with tempfile.TemporaryDirectory(prefix='benchmark-browser-') as directory,mock_provider() as (provider,url):
        app=create_app(benchmark_dir=directory)
        @app.get('/__test__/requests')
        def requests():
            with provider.lock:return jsonify(count=len(provider.calls),peak=provider.peak,models=[b['model'] for b in provider.calls])
        server=make_server('127.0.0.1',0,app,threaded=True)
        print(json.dumps(dict(url=f'http://127.0.0.1:{server.server_port}',provider=url)),flush=True)
        try:server.serve_forever()
        finally:server.server_close()

if __name__=='__main__':main()
