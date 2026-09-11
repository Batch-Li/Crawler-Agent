import json
import pathlib

from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.sources.xiaohongshu import XiaohongshuAdapter
from crawler_tool.sources.xiaohongshu_authorized import (
    XhsAuthorizedPost,
    XhsCredentialError,
    XiaohongshuAuthorizedAdapter,
    parse_cookie_header,
)


ROOT = pathlib.Path(__file__).parent / "fixtures"


class FakeJsonResponse:
    def __init__(self, payload=None, *, status_code=200, content_type="application/json"):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def json(self):
        return self.payload


def _request():
    return ContentSearchRequest(query="人工智能", platforms=["xiaohongshu"], limit=10, freshness="prefer_fresh")


def _success_payload():
    return json.loads((ROOT / "xiaohongshu_search_success.json").read_text(encoding="utf-8"))


def test_parse_cookie_header_pairs():
    assert parse_cookie_header("web_session=abc; a1=x") == {"web_session": "abc", "a1": "x"}
    assert parse_cookie_header("") == {}
    assert parse_cookie_header("broken") == {}


def test_transport_injects_session_cookies_and_blocks_foreign_host():
    calls = []

    def http_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeJsonResponse({"code": 0, "data": {"items": []}})

    transport = XhsAuthorizedPost(http_post, "web_session=fake-secret; webId=v")
    url = "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes"
    transport(url)

    assert calls[0][0] == url
    assert calls[0][1]["cookies"] == {"web_session": "fake-secret", "webId": "v"}
    assert calls[0][1]["follow_redirects"] is False
    assert "fake-secret" not in calls[0][0]
    try:
        transport("https://example.com/api")
    except XhsCredentialError:
        pass
    else:
        raise AssertionError("non-XHS host must be rejected")


def test_authorized_adapter_without_session_is_unavailable_and_offline():
    calls = []
    adapter = XiaohongshuAuthorizedAdapter(http_post=lambda *args, **kwargs: calls.append(1), cookie_header="")
    result = adapter.search(_request())

    assert result.status is SourceStatus.SOURCE_UNAVAILABLE
    assert result.error_code == SourceErrorCode.SOURCE_UNAVAILABLE
    assert result.retryable is False
    assert calls == []


def test_health_reports_credential_state():
    configured = XiaohongshuAuthorizedAdapter(
        http_post=lambda *args, **kwargs: None, cookie_header="web_session=fake-secret"
    ).health()
    missing = XiaohongshuAuthorizedAdapter().health()

    assert configured["authMode"] == "authorized_first_page"
    assert configured["credentialStatus"] == "configured"
    assert configured["capabilities"] == ["search"]
    assert configured["pagination"] == "disabled"
    assert configured["detail"] == "disabled"
    assert missing["authMode"] == "authorized_first_page"
    assert missing["credentialStatus"] == "unavailable"
    assert missing["capabilities"] == []


def test_authorized_search_maps_fixture_success_to_content_items():
    response = FakeJsonResponse(_success_payload())
    adapter = XiaohongshuAuthorizedAdapter(http_post=lambda *a, **kw: response, cookie_header="web_session=fake-secret")
    result = adapter.search(_request())

    assert result.status is SourceStatus.SUCCESS
    assert len(result.items) == 1
    item = result.items[0].payload
    assert item["sourceItemId"] == "note-001"
    assert item["method"] == "authorized_session"
    assert item["hasFullContent"] is False
    assert item["metrics"]["likeCount"] == 12
    assert "fake-secret" not in str(result.response_metadata)


def test_authorized_search_flags_non_json_and_schema_change_as_parser_changed():
    for response in (
        FakeJsonResponse(None, content_type="text/html"),
        FakeJsonResponse({"code": 0, "data": {}}),
    ):
        adapter = XiaohongshuAuthorizedAdapter(http_post=lambda *a, **kw: response, cookie_header="web_session=fake-secret")
        result = adapter.search(_request())

        assert result.status is SourceStatus.PARSER_CHANGED
        assert result.items == []


def test_authorized_search_classifies_http_and_business_errors_without_retrying():
    cases = [
        (FakeJsonResponse(status_code=403), SourceStatus.AUTHENTICATION_REQUIRED),
        (FakeJsonResponse(status_code=429), SourceStatus.RATE_LIMITED),
        (FakeJsonResponse({"code": 300012, "success": False}), SourceStatus.AUTHENTICATION_REQUIRED),
        (FakeJsonResponse({"code": 300011}), SourceStatus.RATE_LIMITED),
        (FakeJsonResponse({"code": -100, "msg": "unknown"}), SourceStatus.NETWORK_ERROR),
    ]
    for response, expected_status in cases:
        calls = []

        def http_post(*args, response=response, **kwargs):
            calls.append(1)
            return response

        adapter = XiaohongshuAuthorizedAdapter(http_post=http_post, cookie_header="web_session=fake-secret")
        result = adapter.search(_request())

        assert result.status is expected_status
        assert len(calls) == 1


def test_authorized_search_surfaces_http_status_for_other_error_codes():
    adapter = XiaohongshuAuthorizedAdapter(http_post=lambda *a, **kw: FakeJsonResponse(status_code=461), cookie_header="web_session=fake-secret")
    result = adapter.search(_request())

    assert result.status is SourceStatus.NETWORK_ERROR
    assert result.error_code == SourceErrorCode.INVALID_RESPONSE
    assert result.response_metadata["httpStatus"] == 461
    assert "fake-secret" not in str(result.response_metadata)


def test_disabled_and_anonymous_production_modes_stay_offline():
    for mode in ("disabled", "anonymous"):
        calls = []
        adapter = XiaohongshuAdapter(http_post=lambda *args, **kwargs: calls.append(1), mode=mode)
        result = adapter.search(_request())

        assert result.status is SourceStatus.SOURCE_UNAVAILABLE
        assert calls == []
