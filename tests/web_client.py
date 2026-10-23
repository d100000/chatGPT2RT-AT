"""Test clients must bootstrap the same signed browser session as the real UI."""

from __future__ import annotations

import re


def browser_headers(app, client, *, refresh=False):
    if not refresh and getattr(client, "_rt_headers", None):
        return dict(client._rt_headers)
    response = client.get("/")
    if response.status_code != 200:
        raise AssertionError(f"Browser bootstrap failed: {response.status_code}")
    html = response.get_data(as_text=True)
    csrf = re.search(r'<meta\s+name="rt-csrf"\s+content="([^"]+)"', html)
    owner = re.search(r'<meta\s+name="rt-workspace"\s+content="([^"]+)"', html)
    if csrf is None or owner is None:
        raise AssertionError("A tool page must provide session-bound CSRF and workspace metadata")
    client._rt_headers = {"X-RT-CSRF": csrf.group(1)}
    client._rt_owner = owner.group(1)
    return dict(client._rt_headers)


def workspace_for(app, client):
    browser_headers(app, client)
    return app.extensions["rt_workspaces"].ensure(client._rt_owner)


def new_browser(app):
    client = app.test_client()
    return client, browser_headers(app, client)
