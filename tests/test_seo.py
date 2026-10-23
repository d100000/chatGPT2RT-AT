"""Static documentation, explicit public origins, and private surface indexing."""

from __future__ import annotations

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from rt_extractor.seo import DOC_PAGES, REPOSITORY_URL, validate_public_url
from rt_extractor.web import create_app
from tests.web_client import browser_headers


PUBLIC_URL = "https://tools.example.com"


class HTML(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.title = ""
        self.text = []
        self.scripts = []
        self._title = False
        self._script = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.tags.append((tag, values))
        if tag == "title":
            self._title = True
        if tag == "script":
            self._script = {"attrs": values, "body": ""}
            self.scripts.append(self._script)

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False
        if tag == "script":
            self._script = None

    def handle_data(self, data):
        if self._title:
            self.title += data
        if self._script is not None:
            self._script["body"] += data
        else:
            self.text.append(data)

    def meta(self, name):
        return [attrs["content"] for tag, attrs in self.tags
                if tag == "meta" and (attrs.get("name") == name or attrs.get("property") == name)]

    def links(self, rel):
        return [attrs["href"] for tag, attrs in self.tags if tag == "link" and attrs.get("rel") == rel]


class PublicURLTests(unittest.TestCase):
    def test_explicit_https_origin_is_normalized(self):
        for given, expected in (("", ""), (None, ""),
                                ("https://TOOLS.EXAMPLE.COM/", PUBLIC_URL),
                                ("https://tools.example.com:443", PUBLIC_URL),
                                ("https://tools.example.com:8443", PUBLIC_URL + ":8443"),
                                ("https://[2001:db8::1]:8443", "https://[2001:db8::1]:8443")):
            with self.subTest(value=given):
                self.assertEqual(validate_public_url(given), expected)

    def test_unsafe_or_non_origin_settings_fail_closed(self):
        for value in ("http://tools.example.com", "//tools.example.com", "tools.example.com",
                      "https://user:password@tools.example.com", "https://user@tools.example.com",
                      "https://tools.example.com/docs", "https://tools.example.com/?x=1",
                      "https://tools.example.com?", "https://tools.example.com/#part",
                      "https://tools.example.com#", "https://tools.example.com:0",
                      "https://tools.example.com:65536", "https://tools.example.com:invalid",
                      "https://tools.example.com:",
                      "https://tools.example.com\\evil", "https://tools.example.com\n",
                      " https://tools.example.com", "https://-invalid.example.com",
                      "https://tools..example.com", "https://", 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_public_url(value)


class SEOTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def app(self, public_url=""):
        with patch.dict(os.environ, {"BOB_AI_PUBLIC_URL": public_url}):
            app = create_app(Path(self.directory.name))
        app.config["TESTING"] = True
        return app, app.test_client()

    def test_default_local_documents_are_noindex_without_public_urls(self):
        _app, client = self.app()
        for page in DOC_PAGES:
            with self.subTest(page=page.path):
                response = client.get(page.path)
                self.assertEqual(response.status_code, 200)
                document = HTML(response.get_data(as_text=True))
                self.assertEqual(document.meta("robots"), ["noindex,follow"])
                self.assertIn("noindex", response.headers["X-Robots-Tag"])
                self.assertEqual(document.links("canonical"), [])
                self.assertEqual(document.meta("og:url"), [])
                self.assertNotIn("http://localhost", response.get_data(as_text=True))
        root = ElementTree.fromstring(client.get("/sitemap.xml").data)
        self.assertEqual(list(root), [])
        self.assertNotIn("Sitemap:", client.get("/robots.txt").get_data(as_text=True))
        self.assertNotRegex(client.get("/llms.txt").get_data(as_text=True), r"https?://[^\s]+/docs")

    def test_configured_docs_have_unique_matching_metadata_and_social_cards(self):
        _app, client = self.app(PUBLIC_URL)
        titles, descriptions = set(), set()
        for page in DOC_PAGES:
            with self.subTest(page=page.path):
                response = client.get(page.path)
                document = HTML(response.get_data(as_text=True))
                self.assertEqual(document.title, page.title + " · bob ai工具")
                self.assertEqual(document.meta("description"), [page.description])
                self.assertEqual(document.meta("robots"), ["index,follow"])
                self.assertEqual(response.headers["X-Robots-Tag"], "index, follow")
                self.assertEqual(document.links("canonical"), [PUBLIC_URL + page.path])
                self.assertEqual(document.meta("og:url"), [PUBLIC_URL + page.path])
                self.assertEqual(document.meta("og:title"), [document.title])
                self.assertEqual(document.meta("og:locale"), ["zh_CN"])
                self.assertEqual(document.meta("twitter:card"), ["summary_large_image"])
                self.assertEqual(document.meta("og:image"), [PUBLIC_URL + "/static/social-preview.png"])
                self.assertEqual(document.meta("twitter:image"), document.meta("og:image"))
                self.assertTrue(document.meta("og:image:alt")[0])
                self.assertIn(page.heading, "".join(document.text))
                titles.add(document.title)
                descriptions.add(document.meta("description")[0])
        self.assertEqual(len(titles), 5)
        self.assertEqual(len(descriptions), 5)

    def test_three_tools_remain_noindex_and_use_independent_descriptions(self):
        _app, client = self.app(PUBLIC_URL)
        titles, descriptions = set(), set()
        for path in ("/", "/converter", "/network"):
            response = client.get(path)
            document = HTML(response.get_data(as_text=True))
            self.assertEqual(document.meta("robots"), ["noindex,follow"])
            self.assertIn("noindex", response.headers["X-Robots-Tag"])
            self.assertIn("noarchive", response.headers["X-Robots-Tag"])
            self.assertEqual(document.links("canonical"), [PUBLIC_URL + path])
            self.assertEqual(document.meta("og:title"), [document.title])
            self.assertIn(("a", {"href": "/docs"}), document.tags)
            titles.add(document.title)
            descriptions.add(document.meta("description")[0])
        self.assertEqual(len(titles), 3)
        self.assertEqual(len(descriptions), 3)

    def test_canonical_and_artifacts_ignore_queries_host_and_forwarding_headers(self):
        _app, client = self.app(PUBLIC_URL)
        for path in ("/docs", "/docs/token-types", "/converter"):
            response = client.get(path + "?source=latest&next=https://untrusted.example/",
                                  base_url="http://localhost:8765", headers={
                                      "X-Forwarded-Host": "untrusted.example",
                                      "X-Forwarded-Proto": "http",
                                  })
            document = HTML(response.get_data(as_text=True))
            self.assertEqual(document.links("canonical"), [PUBLIC_URL + path])
            self.assertNotIn("untrusted.example", response.get_data(as_text=True))
            self.assertNotIn("?source", document.links("canonical")[0])
        with patch.dict(os.environ, {"BOB_AI_PUBLIC_URL": PUBLIC_URL + "/subpath"}), self.assertRaises(ValueError):
            create_app(Path(self.directory.name))

    def test_sitemap_and_llms_list_only_five_canonical_docs(self):
        _app, client = self.app(PUBLIC_URL)
        response = client.get("/sitemap.xml")
        self.assertEqual(response.mimetype, "application/xml")
        root = ElementTree.fromstring(response.data)
        locations = [node.text for node in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        expected = [PUBLIC_URL + page.path for page in DOC_PAGES]
        self.assertEqual(locations, expected)
        llms = client.get("/llms.txt").get_data(as_text=True)
        self.assertEqual(re.findall(r"\]\((https://[^)]+)\)", llms), expected)
        for private in ("/api/", "source=latest", "/health", "X-RT-CSRF", "refresh_token="):
            self.assertNotIn(private, llms)
        self.assertIn("不保证", llms)
        self.assertIn("实验性", llms)

    def test_robots_exposes_sitemap_without_blocking_static_assets(self):
        _app, client = self.app(PUBLIC_URL)
        response = client.get("/robots.txt")
        self.assertEqual(response.mimetype, "text/plain")
        text = response.get_data(as_text=True)
        self.assertIn("Allow: /static/", text)
        self.assertIn("Disallow: /api/", text)
        self.assertIn("Sitemap: " + PUBLIC_URL + "/sitemap.xml", text)
        self.assertNotIn("Disallow: /\n", text)
        self.assertNotIn("Disallow: /static/", text)

    def test_only_configured_successful_share_images_are_indexable(self):
        for public_url in ("", PUBLIC_URL):
            _app, client = self.app(public_url)
            for asset in ("social-preview.png", "social-preview.svg"):
                with self.subTest(public_url=public_url, asset=asset):
                    with client.get("/static/" + asset) as response:
                        self.assertEqual(response.status_code, 200)
                        expected = "index, follow" if public_url else "noindex, nofollow, nosnippet, noarchive"
                        self.assertEqual(response.headers["X-Robots-Tag"], expected)
            for path in ("/static/app.css", "/static/social-preview-missing.png",
                         "/static/nested/social-preview.png", "/social-preview.png"):
                with self.subTest(public_url=public_url, path=path), client.get(path) as response:
                    self.assertIn("noindex", response.headers["X-Robots-Tag"])
            with patch("rt_extractor.web.Flask.send_static_file", side_effect=FileNotFoundError("synthetic")):
                # Framework failures must not inherit the public image exception.
                _app.config["TESTING"] = False
                with client.get("/static/social-preview.png") as response:
                    self.assertEqual(response.status_code, 500)
                    self.assertIn("noindex", response.headers["X-Robots-Tag"])

    def test_docs_show_project_maintenance_and_concept_sources_in_one_footer(self):
        _app, client = self.app(PUBLIC_URL)
        document = HTML(client.get("/docs/token-types").get_data(as_text=True))
        self.assertIn("来源与维护", "".join(document.text))
        for source in ("https://www.rfc-editor.org/rfc/rfc6749",
                       "https://openid.net/specs/openid-connect-core-1_0.html#IDToken"):
            links = [attrs for tag, attrs in document.tags if tag == "a" and attrs.get("href") == source]
            self.assertEqual(len(links), 1)
            self.assertIn("noopener", links[0]["rel"])

    def test_docs_are_read_only_ssr_without_csrf_cookies_or_script_requests(self):
        app, client = self.app(PUBLIC_URL)
        for page in DOC_PAGES:
            response = client.get(page.path)
            html = response.get_data(as_text=True)
            document = HTML(html)
            self.assertFalse(response.headers.getlist("Set-Cookie"))
            for secret in (app.config["RT_CSRF_TOKEN"], "rt-csrf", "/api/", "workspace.js", "network.js", "ipok.io/api"):
                self.assertNotIn(secret, html)
            self.assertFalse(any(tag in {"form", "input", "textarea"} for tag, _ in document.tags))
            self.assertEqual(len(document.scripts), 1)
            self.assertEqual(document.scripts[0]["attrs"]["type"], "application/ld+json")
            self.assertNotIn("src", document.scripts[0]["attrs"])
            for path in ("/", "/converter", "/network", REPOSITORY_URL):
                self.assertTrue(any(tag == "a" and attrs.get("href") == path for tag, attrs in document.tags))
            for section in page.sections:
                self.assertIn(section.heading, "".join(document.text))
                for paragraph in section.paragraphs:
                    self.assertIn(paragraph, "".join(document.text))

    def test_structured_data_uses_facts_and_nonces_without_unsafe_inline(self):
        _app, client = self.app(PUBLIC_URL)
        nonces = []
        for page in DOC_PAGES:
            response = client.get(page.path)
            document = HTML(response.get_data(as_text=True))
            script = document.scripts[0]
            nonce = script["attrs"]["nonce"]
            self.assertIn("'nonce-" + nonce + "'", response.headers["Content-Security-Policy"])
            self.assertNotIn("unsafe-inline", response.headers["Content-Security-Policy"])
            nonces.append(nonce)
            data = json.loads(script["body"])
            self.assertEqual(data["@context"], "https://schema.org")
            source, site, webpage = data["@graph"]
            self.assertEqual([source["@type"], site["@type"], webpage["@type"]],
                             ["SoftwareSourceCode", "WebSite", "WebPage"])
            self.assertEqual(source["codeRepository"], REPOSITORY_URL)
            self.assertEqual(source["programmingLanguage"], ["Python", "JavaScript"])
            self.assertEqual(webpage["url"], PUBLIC_URL + page.path)
            self.assertEqual(webpage["description"], page.description)
            for invented in ("aggregateRating", "offers", "license", "dateModified", "FAQPage"):
                self.assertNotIn(invented, script["body"])
        self.assertEqual(len(set(nonces)), 5)
        self.assertNotIn("nonce-", client.get("/network").headers["Content-Security-Policy"])

    def test_structured_data_cannot_close_the_script_element(self):
        _app, client = self.app(PUBLIC_URL)
        payload = {"text": "</script><script>alert('synthetic')</script>"}
        with patch("rt_extractor.web.structured_data", return_value=payload):
            response = client.get("/docs")
        html = response.get_data(as_text=True)
        document = HTML(html)
        self.assertEqual(len(document.scripts), 1)
        self.assertEqual(json.loads(document.scripts[0]["body"]), payload)
        self.assertNotIn("<script>alert", html)

    def test_api_health_download_and_errors_are_explicitly_not_indexable(self):
        app, client = self.app(PUBLIC_URL)
        headers = browser_headers(app, client)
        for path in (
            "/health",
            "/api/jobs/current",
            "/api/jobs/missing/download",
            "/docs/missing",
            "/missing",
        ):
            response = client.get(path, headers=headers)
            for directive in ("noindex", "nosnippet", "noarchive"):
                self.assertIn(directive, response.headers["X-Robots-Tag"])
        response = client.get("/api/jobs/current")
        self.assertEqual(response.status_code, 403)
        self.assertIn("noindex", response.headers["X-Robots-Tag"])
        self.assertIn("nosnippet", response.headers["X-Robots-Tag"])

    def test_new_routes_retain_existing_host_origin_and_client_guards(self):
        _app, client = self.app(PUBLIC_URL)
        for path in ("/docs", "/docs/token-types", "/robots.txt", "/sitemap.xml", "/llms.txt"):
            for overrides in ({"headers": {"Host": "untrusted.example"}},
                              {"headers": {"Origin": "https://untrusted.example"}},
                              {"environ_overrides": {"REMOTE_ADDR": "203.0.113.9"}}):
                with self.subTest(path=path, overrides=overrides):
                    response = client.get(path, **overrides)
                    self.assertEqual(response.status_code, 403)
                    self.assertIn("noindex", response.headers["X-Robots-Tag"])
                    self.assertNotIn("index, follow", response.headers["X-Robots-Tag"])
                    self.assertFalse(response.headers.getlist("Set-Cookie"))


if __name__ == "__main__":
    unittest.main()
