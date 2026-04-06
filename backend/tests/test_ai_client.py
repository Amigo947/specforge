"""
tests/test_ai_client.py — AI HTTP client tests with mocked httpx transport.

The ai_client module uses a lazy-initialised global httpx.AsyncClient.
Each test patches app.services.ai_client._get_client to return a fresh mock
so tests are fully isolated and the real httpx is never used.

asyncio.sleep is patched throughout to keep retry tests instantaneous.

Coverage:
- _call_with_retry: successful first attempt returns parsed JSON
- _call_with_retry: 5xx response → retries up to the retry limit
- _call_with_retry: 4xx response (e.g. 429) → fails immediately (no retry)
- _call_with_retry: TimeoutException → retries, then raises AppTimeoutError
- _call_with_retry: ConnectError → retries, then raises AIServiceError
- _call_with_retry: all retries exhausted on 5xx → raises AIServiceError
  with detail from response body
- parse_api: wraps _call_with_retry POST /parse correctly
- generate_docs: wraps _call_with_retry POST /generate correctly
- close_ai_client: closes and nils out the global client
- _get_client: reuses the existing client if it is not closed
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx
import pytest

import app.services.ai_client as ai_client_module
from app.core.exceptions import AIServiceError
from app.core.exceptions import TimeoutError as AppTimeoutError


# ── patch targets ─────────────────────────────────────────────────────────────

_GET_CLIENT = "app.services.ai_client._get_client"
_SLEEP      = "app.services.ai_client.asyncio.sleep"


# ── helpers ───────────────────────────────────────────────────────────────────

def _ok_response(payload: dict) -> MagicMock:
    """Build a mock httpx.Response that raises_for_status() passes and returns payload."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _http_error(status_code: int, detail: str = "upstream error") -> httpx.HTTPStatusError:
    """Build a real httpx.HTTPStatusError with the given status code."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = {"detail": detail}
    return httpx.HTTPStatusError(
        message=f"{status_code} error",
        request=MagicMock(),
        response=mock_response,
    )


# ══════════════════════════════════════════════════════════════════════════════
# _call_with_retry — success on first attempt
# ══════════════════════════════════════════════════════════════════════════════

class TestCallWithRetrySuccess:
    @pytest.mark.asyncio
    async def test_successful_response_returned_without_retry(self):
        """
        When the first httpx request succeeds, the parsed JSON dict is
        returned immediately with no retries and no sleep calls.
        """
        payload = {"endpoints": [{"path": "/users", "method": "GET"}]}
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_ok_response(payload))

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()) as mock_sleep,
        ):
            result = await ai_client_module._call_with_retry("POST", "/parse", {})

        assert result == payload
        mock_client.request.assert_awaited_once_with("POST", "/parse", json={})
        mock_sleep.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# _call_with_retry — 5xx retries
# ══════════════════════════════════════════════════════════════════════════════

class TestCallWithRetry5xx:
    @pytest.mark.asyncio
    async def test_5xx_retries_then_succeeds_on_third_attempt(self):
        """
        Two consecutive 503 errors are retried; success on the third attempt
        returns the payload without raising.
        """
        payload = {"result": "ok"}
        err_503 = _http_error(503)

        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            side_effect=[err_503, err_503, _ok_response(payload)]
        )

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()),
        ):
            result = await ai_client_module._call_with_retry("POST", "/parse", {}, retries=2)

        assert result == payload
        assert mock_client.request.await_count == 3

    @pytest.mark.asyncio
    async def test_5xx_all_retries_exhausted_raises_ai_service_error(self):
        """
        Three consecutive 502 errors exhaust the retry budget (retries=2 →
        3 total attempts); AIServiceError is raised with detail from body.
        """
        err_502 = _http_error(502, detail="Gateway Timeout")

        mock_client = MagicMock()
        mock_client.request = AsyncMock(side_effect=[err_502, err_502, err_502])

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()),
        ):
            with pytest.raises(AIServiceError) as exc_info:
                await ai_client_module._call_with_retry("POST", "/parse", {}, retries=2)

        assert "Gateway Timeout" in exc_info.value.message
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_5xx_retry_uses_exponential_backoff_sleep(self):
        """
        Between retries, asyncio.sleep is called with delay = attempt * 2
        (attempt 1 → 2s, attempt 2 → 4s).
        """
        err_503 = _http_error(503)
        payload = {"ok": True}

        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            side_effect=[err_503, err_503, _ok_response(payload)]
        )

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()) as mock_sleep,
        ):
            await ai_client_module._call_with_retry("POST", "/parse", {}, retries=2)

        assert mock_sleep.await_count == 2
        sleep_delays = [c.args[0] for c in mock_sleep.await_args_list]
        assert sleep_delays == [2, 4]  # attempt 1→2s, attempt 2→4s


# ══════════════════════════════════════════════════════════════════════════════
# _call_with_retry — 4xx fails immediately (no retry)
# ══════════════════════════════════════════════════════════════════════════════

class TestCallWithRetry4xx:
    @pytest.mark.asyncio
    async def test_4xx_response_raises_immediately_without_retry(self):
        """
        A 429 (Too Many Requests) response is a 4xx: the condition
        `status_code < 500` causes an immediate break from the retry loop.
        AIServiceError is raised after a single attempt; sleep is not called.
        """
        err_429 = _http_error(429, detail="rate limit exceeded")

        mock_client = MagicMock()
        mock_client.request = AsyncMock(side_effect=err_429)

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()) as mock_sleep,
        ):
            with pytest.raises(AIServiceError) as exc_info:
                await ai_client_module._call_with_retry("POST", "/parse", {}, retries=2)

        assert mock_client.request.await_count == 1
        mock_sleep.assert_not_awaited()
        assert "rate limit exceeded" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_404_response_raises_immediately_without_retry(self):
        """
        404 is also a 4xx: no retry, AIServiceError raised on first attempt.
        """
        err_404 = _http_error(404, detail="endpoint not found")
        mock_client = MagicMock()
        mock_client.request = AsyncMock(side_effect=err_404)

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()) as mock_sleep,
        ):
            with pytest.raises(AIServiceError):
                await ai_client_module._call_with_retry("POST", "/parse", {})

        assert mock_client.request.await_count == 1
        mock_sleep.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# _call_with_retry — TimeoutException → AppTimeoutError
# ══════════════════════════════════════════════════════════════════════════════

class TestCallWithRetryTimeout:
    @pytest.mark.asyncio
    async def test_timeout_retries_then_raises_app_timeout_error(self):
        """
        Three consecutive TimeoutExceptions exhaust the retry budget;
        AppTimeoutError (status 504) is raised.
        """
        timeout_exc = httpx.TimeoutException("timed out")

        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            side_effect=[timeout_exc, timeout_exc, timeout_exc]
        )

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()),
        ):
            with pytest.raises(AppTimeoutError) as exc_info:
                await ai_client_module._call_with_retry("POST", "/parse", {}, retries=2)

        assert exc_info.value.status_code == 504
        assert exc_info.value.code == "TIMEOUT_ERROR"
        assert mock_client.request.await_count == 3

    @pytest.mark.asyncio
    async def test_timeout_then_success_returns_result(self):
        """
        One timeout followed by a success: result is returned after retry.
        """
        payload = {"parsed": True}
        timeout_exc = httpx.TimeoutException("timed out")

        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            side_effect=[timeout_exc, _ok_response(payload)]
        )

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()),
        ):
            result = await ai_client_module._call_with_retry("POST", "/parse", {}, retries=2)

        assert result == payload
        assert mock_client.request.await_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# _call_with_retry — ConnectError → AIServiceError
# ══════════════════════════════════════════════════════════════════════════════

class TestCallWithRetryConnectError:
    @pytest.mark.asyncio
    async def test_connect_error_retries_then_raises_ai_service_error(self):
        """
        Three consecutive ConnectErrors exhaust retries; AIServiceError is
        raised with 'AI service unavailable' message.
        """
        connect_exc = httpx.ConnectError("connection refused")

        mock_client = MagicMock()
        mock_client.request = AsyncMock(side_effect=connect_exc)

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()),
        ):
            with pytest.raises(AIServiceError) as exc_info:
                await ai_client_module._call_with_retry("POST", "/parse", {}, retries=2)

        assert "unavailable" in exc_info.value.message.lower()
        assert mock_client.request.await_count == 3


# ══════════════════════════════════════════════════════════════════════════════
# parse_api and generate_docs — public interface
# ══════════════════════════════════════════════════════════════════════════════

class TestParseApiPublicInterface:
    @pytest.mark.asyncio
    async def test_parse_api_posts_to_correct_url_with_correct_body(self):
        """
        parse_api sends POST to /parse with source_code, source_type,
        and framework in the JSON body.
        """
        payload = {"endpoints": []}
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_ok_response(payload))

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()),
        ):
            result = await ai_client_module.parse_api(
                source_code="from fastapi import FastAPI",
                source_type="RAW_CODE",
                framework="fastapi",
            )

        assert result == payload
        mock_client.request.assert_awaited_once_with(
            "POST",
            "/parse",
            json={
                "source_code": "from fastapi import FastAPI",
                "source_type": "RAW_CODE",
                "framework": "fastapi",
            },
        )

    @pytest.mark.asyncio
    async def test_parse_api_passes_none_framework_in_body(self):
        """parse_api passes framework=None when the project has no framework."""
        payload = {"endpoints": []}
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_ok_response(payload))

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()),
        ):
            await ai_client_module.parse_api(
                source_code="code", source_type="RAW_CODE", framework=None
            )

        body = mock_client.request.call_args.kwargs["json"]
        assert body["framework"] is None


class TestGenerateDocsPublicInterface:
    @pytest.mark.asyncio
    async def test_generate_docs_posts_to_correct_url_with_endpoint_payload(self):
        """
        generate_docs sends POST to /generate with the endpoint dict
        nested under the 'endpoint' key in the JSON body.
        """
        endpoint_payload = {
            "path": "/users",
            "method": "GET",
            "summary": "List users",
            "description": None,
            "parameters": [],
        }
        ai_response = {
            "examples": [],
            "documentation": {"title": "GET /users", "content": "Docs"},
        }
        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=_ok_response(ai_response))

        with (
            patch(_GET_CLIENT, return_value=mock_client),
            patch(_SLEEP, new=AsyncMock()),
        ):
            result = await ai_client_module.generate_docs(endpoint_payload)

        assert result == ai_response
        mock_client.request.assert_awaited_once_with(
            "POST",
            "/generate",
            json={"endpoint": endpoint_payload},
        )


# ══════════════════════════════════════════════════════════════════════════════
# _get_client — singleton behaviour
# ══════════════════════════════════════════════════════════════════════════════

class TestGetClientSingleton:
    def test_get_client_creates_new_client_when_global_is_none(self):
        """
        _get_client creates a new httpx.AsyncClient when the global _client
        is None.  The returned client has the AI service base_url.
        """
        # Reset the global client to ensure clean state
        original = ai_client_module._client
        ai_client_module._client = None
        try:
            client = ai_client_module._get_client()
            assert client is not None
            assert isinstance(client, httpx.AsyncClient)
        finally:
            # Don't leave a real client open — restore original state
            ai_client_module._client = original

    def test_get_client_reuses_open_client(self):
        """
        _get_client returns the same object when the client is already open
        (avoids unnecessary connection-pool recreation).
        """
        original = ai_client_module._client
        mock_open_client = MagicMock()
        mock_open_client.is_closed = False
        ai_client_module._client = mock_open_client
        try:
            client = ai_client_module._get_client()
            assert client is mock_open_client
        finally:
            ai_client_module._client = original

    def test_get_client_creates_new_client_when_existing_is_closed(self):
        """
        _get_client creates a new client when the cached client is closed,
        preventing usage of a dead connection pool.
        """
        original = ai_client_module._client
        mock_closed = MagicMock()
        mock_closed.is_closed = True
        ai_client_module._client = mock_closed
        try:
            client = ai_client_module._get_client()
            assert client is not mock_closed
            assert isinstance(client, httpx.AsyncClient)
        finally:
            ai_client_module._client = original


# ══════════════════════════════════════════════════════════════════════════════
# close_ai_client
# ══════════════════════════════════════════════════════════════════════════════

class TestCloseAiClient:
    @pytest.mark.asyncio
    async def test_close_ai_client_closes_and_nils_global_client(self):
        """
        close_ai_client calls aclose() on the open client and sets the
        global _client to None.
        """
        original = ai_client_module._client
        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.aclose = AsyncMock()
        ai_client_module._client = mock_client

        try:
            await ai_client_module.close_ai_client()
            mock_client.aclose.assert_awaited_once()
            assert ai_client_module._client is None
        finally:
            ai_client_module._client = original

    @pytest.mark.asyncio
    async def test_close_ai_client_is_noop_when_no_client_exists(self):
        """
        close_ai_client does nothing (no exception) when _client is already None.
        """
        original = ai_client_module._client
        ai_client_module._client = None
        try:
            await ai_client_module.close_ai_client()  # must not raise
        finally:
            ai_client_module._client = original
