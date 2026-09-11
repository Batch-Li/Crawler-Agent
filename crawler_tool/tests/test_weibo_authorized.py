from crawler_tool.domain import ContentSearchRequest, SourceErrorCode, SourceStatus
from crawler_tool.sources.weibo_authorized import WeiboAuthorizedAdapter, WeiboAuthorizedGet, WeiboCredentialError, parse_cookie_header


class FakeResponse:
    def __init__(self, text="", status_code=200, content_type="text/html"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_parse_cookie_header():
    assert parse_cookie_header("SUB=abc; SUBP=def") == {"SUB": "abc", "SUBP": "def"}


def test_authorized_get_injects_cookie_without_leaking_it_to_url():
    calls = []

    def http_get(url, **kwargs):
        calls.append((url, kwargs))
        return "ok"

    transport = WeiboAuthorizedGet(http_get, "SUB=secret; SUBP=value")
    assert transport("https://s.weibo.com/weibo", params={"page": 1}) == "ok"
    assert calls[0][0] == "https://s.weibo.com/weibo"
    assert calls[0][1]["cookies"] == {"SUB": "secret", "SUBP": "value"}
    assert calls[0][1]["follow_redirects"] is False
    assert "secret" not in calls[0][0]


def test_authorized_get_rejects_non_weibo_host():
    transport = WeiboAuthorizedGet(lambda *args, **kwargs: None, "SUB=secret")
    try:
        transport("https://example.com/search")
    except WeiboCredentialError:
        pass
    else:
        raise AssertionError("non-Weibo host must be rejected")


def test_authorized_adapter_without_cookie_is_unavailable():
    adapter = WeiboAuthorizedAdapter(http_get=lambda *args, **kwargs: None)
    result = adapter.search(ContentSearchRequest(query="q", platforms=["weibo"]))
    assert result.error_code == SourceErrorCode.SOURCE_UNAVAILABLE
    assert result.retryable is False


def test_authorized_adapter_parses_success_and_redacts_cookie_from_diagnostics():
    page = """
    <div class='card-wrap'><div class='card'>
      <a class='name'>author</a><p class='txt'>content</p>
      <a href='https://weibo.com/123/post-id'>now</a><span>转发 1 评论 2 赞 3</span>
    </div></div>
    """
    adapter = WeiboAuthorizedAdapter(
        http_get=lambda *args, **kwargs: FakeResponse(page),
        cookie_header="SUB=fake-secret",
    )
    result = adapter.search(ContentSearchRequest(query="q", platforms=["weibo"]))

    assert result.status is SourceStatus.SUCCESS
    assert len(result.items) == 1
    assert result.response_metadata["pageDiagnosis"]["classification"] == "success"
    assert "fake-secret" not in str(result.response_metadata)


def test_authorized_adapter_maps_login_challenge_and_rate_limit():
    request = ContentSearchRequest(query="q", platforms=["weibo"])
    for response, expected_status, expected_error, retryable in [
        (FakeResponse("<html>请登录后继续</html>"), SourceStatus.AUTHENTICATION_REQUIRED, SourceErrorCode.AUTH_REQUIRED, False),
        (FakeResponse("<html>请输入验证码</html>"), SourceStatus.AUTHENTICATION_REQUIRED, SourceErrorCode.AUTH_REQUIRED, False),
        (FakeResponse(status_code=429), SourceStatus.RATE_LIMITED, SourceErrorCode.RATE_LIMITED, True),
        (FakeResponse("<html>访问频次过高</html>"), SourceStatus.RATE_LIMITED, SourceErrorCode.RATE_LIMITED, True),
    ]:
        adapter = WeiboAuthorizedAdapter(
            http_get=lambda *args, response=response, **kwargs: response,
            cookie_header="SUB=fake-secret",
        )
        result = adapter.search(request)
        assert result.status is expected_status
        assert result.error_code is expected_error
        assert result.retryable is retryable
        assert "fake-secret" not in str(result.response_metadata)


def test_authorized_redirect_response_is_classified_as_auth_not_network_error():
    # 微博会话失效时返回 302 跳登录；httpx 0.28 的 raise_for_status 对 3xx 也会抛
    # HTTPStatusError，曾被兜底误报成 network_error。必须归类为认证问题。
    request = ContentSearchRequest(query="q", platforms=["weibo"])
    response = FakeResponse(status_code=302)
    response.headers["location"] = "https://passport.weibo.com/sso/login?entry=weibo"
    adapter = WeiboAuthorizedAdapter(
        http_get=lambda *args, **kwargs: response,
        cookie_header="SUB=fake-secret",
    )

    result = adapter.search(request)

    assert result.status is SourceStatus.AUTHENTICATION_REQUIRED
    assert result.error_code is SourceErrorCode.AUTH_REQUIRED
    assert result.retryable is False
    assert "HTTP 302" in result.message
    assert "passport.weibo.com" in result.message
    assert result.response_metadata["redirectStatus"] == 302
    # 脱敏：只留主机名，不落完整跳转 URL（可能含回跳 token）。
    assert "sso/login" not in result.message
    assert "sso/login" not in str(result.response_metadata)


def test_generic_exception_message_keeps_status_code_without_sensitive_data():
    class FakeStatusError(Exception):
        def __init__(self):
            super().__init__("Server error '403 Forbidden' for url 'https://s.weibo.com/weibo?q=secret-query'")
            self.response = FakeResponse(status_code=403)

    request = ContentSearchRequest(query="q", platforms=["weibo"])
    adapter = WeiboAuthorizedAdapter(
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(FakeStatusError()),
        cookie_header="SUB=fake-secret",
    )

    result = adapter.search(request)

    assert result.status.value == "network_error"
    assert "HTTPStatusError" in result.message or "FakeStatusError" in result.message
    assert "HTTP 403" in result.message
    # 兜底消息不含异常全文（其中可能带完整 URL/查询串）。
    assert "secret-query" not in result.message
