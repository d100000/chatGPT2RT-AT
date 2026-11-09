import base64
import tempfile
import unittest

from rt_extractor.eval_reports import html_export
from rt_extractor.web import create_app
from tests.pelican_mock import mock_provider
from tests.test_pelican import png_fixture, wait_for
from tests.web_client import browser_headers


class AnimationExportTests(unittest.TestCase):
    def test_animation_report_plays_safe_svg_while_static_keeps_saved_frame(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"><animate attributeName="r" values="10;20;10" dur="2s" repeatCount="indefinite"/></circle></svg>'
        sample = {"id": "test", "batch_id": "batch", "round": 1,
                  "requested_model": "fixture", "status": "success", "reviews": [],
                  "question": {"id": "animation-test", "title": "圆的呼吸", "animation": True},
                  "svg": svg, "technical": {"safe": True},
                  "render_evidence": {"data_url": png_fixture()}}
        data = {"samples": [sample], "groups": [], "batches": []}
        encoded = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
        html = html_export(data)
        self.assertIn('src="' + encoded + '"', html)
        self.assertIn("完整动画周期", html)
        self.assertIn("圆的呼吸", html)
        self.assertNotIn("<animate ", html)
        sample["question"]["animation"] = False
        self.assertIn('src="' + png_fixture() + '"', html_export(data))
        sample["question"]["animation"] = True
        sample["technical"]["safe"] = False
        self.assertNotIn('src="' + encoded + '"', html_export(data))

    def test_task_polling_omits_pool_but_export_preserves_frozen_questions(self):
        with tempfile.TemporaryDirectory() as directory, mock_provider() as (_, base):
            app = create_app(pelican_dir=directory)
            try:
                client = app.test_client()
                headers = browser_headers(app, client)
                client.get("/pelican")
                result = client.post("/api/pelican/tasks", headers=headers, json={
                    "base_url": base, "api_key": "test-animation-key", "models": ["mock-good"]})
                self.assertEqual(result.status_code, 201)
                self.assertNotIn("questions", result.json["batch"]["config"])
                self.assertEqual(result.json["batch"]["config"]["question_count"], 100)
                tasks = wait_for(lambda: (payload if (payload := client.get(
                    "/api/pelican/tasks", headers=headers).json)["tasks"][0]["status"] == "completed" else None))
                self.assertNotIn("questions", tasks["tasks"][0]["config"])
                self.assertLess(len(str(tasks)), 4000)
                data = client.get("/api/pelican/export/json", headers=headers).json
                self.assertEqual(len(data["batches"][0]["config"]["questions"]), 100)
                self.assertTrue(data["samples"][0]["question"]["animation"])
            finally:
                app.extensions["pelican"].close()
