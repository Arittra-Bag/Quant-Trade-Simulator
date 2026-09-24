"""Responses are compressed; only Dash's fingerprinted bundles keep a compressed copy."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


class FakeRequest:
    def __init__(self, method, path, query=""):
        """Hold the three request fields the cache key reads."""
        self.method, self.path, self.full_path = method, path, f"{path}?{query}"


def test_only_bundles_get_a_cache_key():
    """Only GET requests for Dash bundles are cacheable."""
    bundle = FakeRequest("GET", "/_dash-component-suites/plotly/package_data/plotly.min.v7_1_0m1.js")
    assert app.bundle_cache_key(bundle) == "bundle:" + bundle.path
    assert app.bundle_cache_key(FakeRequest("POST", "/_dash-update-component")) == ""
    assert app.bundle_cache_key(FakeRequest("GET", "/")) == ""


def test_cache_stores_only_bundles():
    """A key without the bundle marker is never stored."""
    cache = app.StaticBundleCache()
    cache.set("br;", b"poll")
    assert cache.get("br;") is None
    cache.set("br;bundle:/a.js", b"js")
    assert cache.get("br;bundle:/a.js") == b"js"


def test_query_strings_share_one_entry():
    """A query string cannot create a new cache entry."""
    plain = FakeRequest("GET", "/_dash-component-suites/dash/a.js", "v=1")
    other = FakeRequest("GET", "/_dash-component-suites/dash/a.js", "x=2")
    assert app.bundle_cache_key(plain) == app.bundle_cache_key(other)


def test_cache_is_capped():
    """The oldest entries go once the cap is reached."""
    cache = app.StaticBundleCache()
    for i in range(cache.MAX_ENTRIES + 10):
        cache.set(f"br;bundle:/{i}.js", b"js")
    assert cache.get("br;bundle:/0.js") is None
    assert cache.get(f"br;bundle:/{cache.MAX_ENTRIES + 9}.js") == b"js"


def test_bundles_are_served_compressed():
    """A real bundle comes back brotli-encoded, identically from the cache."""
    client = app.server.test_client()
    page = client.get("/").get_data(as_text=True)
    src = next(part.split('"')[0] for part in page.split('src="')[1:] if "_dash-component-suites" in part)
    first = client.get(src, headers={"Accept-Encoding": "br, gzip"})
    again = client.get(src, headers={"Accept-Encoding": "br, gzip"})
    assert first.headers["Content-Encoding"] == "br"
    assert first.get_data() == again.get_data()
