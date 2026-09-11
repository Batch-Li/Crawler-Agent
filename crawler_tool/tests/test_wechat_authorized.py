from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.sources.wechat_authorized import WechatAuthorizedListAdapter


def test_wechat_service_health_states():
    from crawler_tool.application.wechat_search_service import WechatKeywordSearchService

    disabled = WechatKeywordSearchService(lambda: None).health()
    assert disabled["platform"] == "wechat"
    assert disabled["credentialStatus"] == "disabled"
    assert disabled["capabilities"] == []

    missing_cookie = WechatKeywordSearchService(lambda: None, enabled=True).health()
    assert missing_cookie["authMode"] == "authorized_first_page"
    assert missing_cookie["credentialStatus"] == "unavailable"

    ready = WechatKeywordSearchService(lambda: None, enabled=True, credential_configured=True).health()
    assert ready["credentialStatus"] == "configured"
    assert ready["capabilities"] == ["search"]


class FakeResponse:
    def __init__(self, text="", payload=None, status_code=200):
        self.text = text
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self.payload


def test_wechat_authorized_list_first_page_uses_three_requests_only():
    calls = []
    responses = [
        FakeResponse("token=123"),
        FakeResponse(payload={"list": [{"fakeid": "fake", "nickname": "Demo Account"}]}),
        FakeResponse(payload={"app_msg_list": [{
            "aid": "article-1", "title": "<em>AI</em> news", "digest": "summary",
            "link": "https://mp.weixin.qq.com/s?mid=article-1", "author": "Author", "create_time": 1,
        }]}),
    ]

    def http_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    adapter = WechatAuthorizedListAdapter(http_get=http_get, cookie_header="session=fake", max_items=3)
    result = adapter.search(ContentSearchRequest(query="Demo Account", platforms=["wechat"], limit=10))

    assert result.status is SourceStatus.SUCCESS
    assert len(result.items) == 1
    assert result.items[0].payload["content"] is None
    assert result.items[0].payload["method"] == "authorized_session"
    assert len(calls) == 3
    assert calls[2][1]["params"]["begin"] == "0"
    assert calls[2][1]["params"]["count"] == "3"
    assert calls[2][0].endswith("/cgi-bin/appmsgpublish")
    assert calls[2][1]["params"]["sub"] == "list"
    assert calls[2][1]["params"]["sub_action"] == "list_ex"
    assert all(call[1]["follow_redirects"] is False for call in calls)


def test_wechat_authorized_list_without_session_does_not_request_network():
    calls = []
    adapter = WechatAuthorizedListAdapter(http_get=lambda *args, **kwargs: calls.append(1), cookie_header="")
    result = adapter.search(ContentSearchRequest(query="Demo", platforms=["wechat"]))

    assert result.status is SourceStatus.SOURCE_UNAVAILABLE
    assert result.error_code is SourceErrorCode.SOURCE_UNAVAILABLE
    assert calls == []


def test_wechat_listing_empty_and_schema_change_are_distinct():
    adapter = WechatAuthorizedListAdapter(cookie_header="session=fake")
    assert adapter.parse_listing({"app_msg_list": []}, account_name="Demo").status is SourceStatus.EMPTY
    assert adapter.parse_listing({}, account_name="Demo").status is SourceStatus.PARSER_CHANGED
