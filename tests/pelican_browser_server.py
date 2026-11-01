"""Isolated browser QA server. All history is temporary; upstream is a loopback fixture."""
import json
import tempfile

from flask import jsonify
from werkzeug.serving import make_server

from rt_extractor.web import create_app
from tests.pelican_mock import mock_provider

with tempfile.TemporaryDirectory(prefix="pelican-browser-qa-") as directory, mock_provider() as (provider, base_url):
    app = create_app(pelican_dir=directory)

    @app.get('/__test__/requests')
    def requests():
        with provider.lock:
            return jsonify(requests=provider.requests, maximum_active=provider.max_active)

    server = make_server('127.0.0.1', 0, app, threaded=True)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "provider": base_url}), flush=True)
    try:
        server.serve_forever()
    finally:
        app.extensions['pelican'].close()
        server.server_close()
