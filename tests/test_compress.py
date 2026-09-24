"""Responses are compressed; only Dash's fingerprinted bundles keep a compressed copy."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


class FakeRequest:
    def __init__(self, method, path, query=""):
        self.method, self.path, self.full_path = method, path, f"{path}?{query}"


def test_only_bundles_get_a_cache_key():
    bundle = FakeRequest("GET", "/_dash-component-suites/plotly/package_data/plotly.min.v7_1_0m1.js")
    assert app.bundle_cache_key(bundle) == bundle.full_path
    assert app.bundle_cache_key(FakeRequest("POST", "/_dash-update-component")) == ""
    assert app.bundle_cache_key(FakeRequest("GET", "/")) == ""


def test_cache_never_stores_an_empty_key():
    cache = app.StaticBundleCache()
    cache.set("br;", b"poll")
    assert cache.get("br;") is None
    cache.set("br;/bundle.js?v=1", b"js")
    assert cache.get("br;/bundle.js?v=1") == b"js"


def test_bundles_are_served_compressed():
    client = app.server.test_client()
    page = client.get("/").get_data(as_text=True)
    src = next(part.split('"')[0] for part in page.split('src="')[1:] if "_dash-component-suites" in part)
    first = client.get(src, headers={"Accept-Encoding": "br, gzip"})
    again = client.get(src, headers={"Accept-Encoding": "br, gzip"})
    assert first.headers["Content-Encoding"] == "br"
    assert first.get_data() == again.get_data()
