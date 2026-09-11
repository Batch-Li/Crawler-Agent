from __future__ import annotations

import pathlib

import pytest

from crawler_tool.validation.real_smoke import (
    LiveValidationError,
    ValidationConfig,
    run_toutiao_two_page_smoke,
)


ROOT = pathlib.Path(__file__).parent / "fixtures"


class FakeResponse:
    status_code = 200
    headers = {"content-type": "text/html"}

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls = []
        self.pages = {
            0: (ROOT / "toutiao_search_page_0.html").read_text(encoding="utf-8"),
            1: (ROOT / "toutiao_search_page_1.html").read_text(encoding="utf-8"),
        }

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.pages[kwargs["params"]["page_num"]])


def test_real_validation_requires_double_confirmation():
    with pytest.raises(LiveValidationError):
        run_toutiao_two_page_smoke(ValidationConfig(query="query"), client_factory=FakeClient)
    with pytest.raises(LiveValidationError):
        run_toutiao_two_page_smoke(ValidationConfig(query="query", allow_network=True), client_factory=FakeClient)


def test_toutiao_live_validation_is_limited_and_redacted():
    report, code = run_toutiao_two_page_smoke(
        ValidationConfig(query="private query", allow_network=True, confirm_real=True),
        client_factory=FakeClient,
    )

    encoded = str(report)
    assert code == 0
    assert report["requestCount"] == 2
    assert [page["page"] for page in report["pages"]] == [0, 1]
    assert "private query" not in encoded
    assert "https://" not in encoded
    assert "第一页 AI 客服产品" not in encoded
