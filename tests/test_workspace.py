"""Contracts for the three-tool workspace and browser-only IPOK integration."""
from pathlib import Path
import tempfile
import unittest

from rt_extractor.web import create_app


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name))
        self.client = self.app.test_client()

    def tearDown(self):
        self.directory.cleanup()

    def test_each_tool_has_navigation_and_can_authenticate_local_result_requests(self):
        for route, page in (("/", "extractor"), ("/converter", "converter"), ("/network", "network")):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn(f'data-page="{page}"', html)
                self.assertIn(self.app.config["RT_CSRF_TOKEN"], html)
                for href in ("/", "/converter", "/network"):
                    self.assertIn(f'href="{href}"', html)
                self.assertIn('id="network-summary-status"', html)
                self.assertIn('src="/static/network.js"', html)
                self.assertIn('href="https://ipok.io/"', html)

    def test_csp_allows_ipok_fetch_without_external_scripts_or_framing(self):
        response = self.client.get("/network")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("connect-src 'self' https://ipok.io;", csp)
        self.assertIn("script-src 'self';", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("<iframe", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_all_tools_retain_local_access_guards(self):
        for route in ("/", "/converter", "/network"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route, environ_overrides={"REMOTE_ADDR": "203.0.113.1"}).status_code, 403)
                self.assertEqual(self.client.get(route, headers={"Origin": "https://untrusted.example"}).status_code, 403)

    def test_reference_design_styles_only_load_for_the_ip_tool(self):
        for route in ("/", "/converter"):
            self.assertNotIn('href="/static/network.css"', self.client.get(route).get_data(as_text=True))
        html = self.client.get("/network").get_data(as_text=True)
        self.assertIn('href="/static/network.css"', html)
        self.assertIn('id="network-query-form"', html)
        self.assertIn('id="network-gauge-arc"', html)
        response = self.client.get("/static/network.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/css")
        response.close()


if __name__ == "__main__":
    unittest.main()
