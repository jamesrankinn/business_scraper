import pytest

from beauty_leads.config import load_config
from beauty_leads.http import HttpResult


@pytest.fixture
def cfg():
    return load_config()


class FakeHttp:
    """Serves canned pages: {url: html or (status, html, final_url)}."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url, params=None, headers=None, crawl=False, use_cache=True):
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            return HttpResult(url=url, status=0, text="", headers={}, error="ConnectionError: unreachable")
        if isinstance(page, tuple):
            status, text, final = page
            return HttpResult(url=final, status=status, text=text, headers={})
        return HttpResult(url=url, status=200, text=page, headers={})


@pytest.fixture
def fake_http():
    return FakeHttp
