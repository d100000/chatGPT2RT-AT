"""Loopback-only synthetic provider for tests/manual UI QA; never used by production."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400" viewBox="0 0 600 400"><rect width="600" height="400" fill="#f5f7fb"/><g fill="none" stroke="#17233d" stroke-width="7"><circle cx="180" cy="285" r="65"/><circle cx="420" cy="285" r="65"/><path d="m180 285 80-100 55 100H180m135 0 75-125 30 125m-182-100h45m107-25h35"/></g><ellipse cx="285" cy="140" rx="58" ry="38" fill="white" stroke="#17233d" stroke-width="3"/><path d="M310 124Q318 40 357 63L363 89L430 94L362 108Q342 151 330 155" fill="#fff" stroke="#17233d" stroke-width="3"/><path d="m362 89 70 5-68 17z" fill="#f1b24b"/><circle cx="354" cy="78" r="4"/><path d="m273 168 25 72 22 38m-20-136 68 30" fill="none" stroke="#17233d" stroke-width="7"/><text x="24" y="30" font-size="16">LOCAL TEST FIXTURE</text></svg>'


ANIMATED_SVG = SVG.replace('<rect ', '''<style>
/* Local declarations deliberately exercise the previously rejected syntax. */
circle {transform-box:fill-box;transform-origin:center;animation:spin 2s linear infinite;}
@keyframes spin {to {transform:rotate(360deg);}}
@media (prefers-reduced-motion:reduce) {circle {animation:none;}}
</style><path d="M10 370L90 370" stroke="orange" stroke-width="6">
<animate attributeName="d" values="M10 370L90 370;M10 360L90 360;M10 370L90 370" dur="2s" repeatCount="indefinite"/>
</path><rect ''', 1)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address):
        super().__init__(address, Handler)
        self.requests = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def json_response(self, status, value):
        encoded = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.json_response(200, {"data": [{"id": m} for m in ["mock-good", "mock-json", "mock-error", "mock-slow", "mock-judge"]]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.requests.append({"path": self.path, "body": body, "started": time.monotonic()})
            self.server.active += 1
            self.server.max_active = max(self.server.max_active, self.server.active)
        try:
            self.reply(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.server.lock:
                self.server.active -= 1

    def reply(self, body):
        model = body.get("model", "mock-good")
        if model == "mock-error":
            self.json_response(429, {"error": {"message": "synthetic quota rejection " + self.headers.get("Authorization", "")}})
            return
        text = SVG
        if model == "mock-animated": text = ANIMATED_SVG
        if model == "mock-missing": text = "No image here"
        if model == "mock-invalid": text = '<svg xmlns="http://www.w3.org/2000/svg"><g></svg>'
        if model == "mock-unsafe": text = '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>'
        if model == "mock-multiple": text = "```svg\n" + SVG + "\n```\n" + SVG
        if model == "mock-echo": text = self.headers.get("Authorization", "") + "\n" + SVG
        if model == "mock-judge": text = json.dumps({"dimensions": {k: {"score": 3, "reason": "synthetic image-review fixture", "location": "image center"} for k in "ABCD"}})
        if model == "mock-bad-judge": text = '{"dimensions":{"A":{"score":100}}}'
        chat = self.path.endswith("/chat/completions")
        usage = {"prompt_tokens": 0, "completion_tokens": 100, "total_tokens": 100} if chat else {"input_tokens": 0, "output_tokens": 100, "total_tokens": 100}
        if model == "mock-no-usage": usage = None
        if model in {"mock-json", "mock-judge", "mock-bad-judge"}:
            payload = {"model": "mock-returned", "usage": usage}
            payload.update({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]} if chat else {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]})
            self.json_response(200, payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        def event(value):
            self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())
            self.wfile.flush()
        if chat:
            event({"model": "mock-returned", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "not visible"}}]})
        else:
            event({"type": "response.created", "response": {"model": "mock-returned"}})
            event({"type": "response.reasoning_summary_text.delta", "delta": "not visible"})
        self.wfile.write(b": heartbeat\n\n"); self.wfile.flush()
        time.sleep(.035)
        half = len(text) // 2
        def delta(value):
            event({"choices": [{"index": 0, "delta": {"content": value}}]} if chat else {"type": "response.output_text.delta", "delta": value})
        delta(text[:half])
        if model in {"mock-slow", "mock-timeout"}:
            time.sleep(2)
        if model == "mock-disconnect": return
        delta(text[half:])
        if chat:
            event({"choices": [{"index": 0, "delta": {}, "finish_reason": "length" if model == "mock-length" else "stop"}], "usage": usage})
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            incomplete = model == "mock-length"
            event({"type": "response.incomplete" if incomplete else "response.completed", "response": {"status": "incomplete" if incomplete else "completed", "usage": usage, "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None}})


@contextmanager
def mock_provider(port=0):
    server = MockServer(("127.0.0.1", port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/proxy/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    with mock_provider(args.port) as (_, url):
        print(f"Local synthetic test provider: {url}", flush=True)
        threading.Event().wait()
