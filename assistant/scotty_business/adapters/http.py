from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class ProviderError(RuntimeError):
    """A provider response was invalid, unsafe, or out of scope."""


class AmbiguousEffectError(ProviderError):
    """A mutation may have reached the provider and must not be retried."""


class RedactedMapping(dict[str, str]):
    def __repr__(self) -> str:
        return "<redacted provider parameters>"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: object


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class HttpTransport:
    """Bounded stdlib HTTP transport with redirects and retries disabled."""

    def __init__(self, *, timeout: float = 20.0, max_response_bytes: int = 1_048_576):
        if timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("transport bounds must be positive")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> HttpResponse:
        upper = method.upper()
        if upper not in {"GET", "POST", "PUT"}:
            raise ProviderError("HTTP method is not permitted")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderError("provider URL must be an absolute HTTPS URL")
        if parsed.fragment:
            raise ProviderError("provider URL fragments are forbidden")
        if query:
            encoded = urllib.parse.urlencode(query, doseq=True)
            url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, encoded, ""))
        body_bytes = None
        request_headers = dict(headers or {})
        if json_body is not None:
            body_bytes = json.dumps(
                json_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            if len(body_bytes) > 65_536:
                raise ProviderError("provider request body exceeds the limit")
            request_headers["Content-Type"] = "application/json"
        request_headers.setdefault("Accept", "application/json")
        # The scheme and authority were parsed and restricted to HTTPS above.
        request = urllib.request.Request(  # noqa: S310
            url, data=body_bytes, headers=request_headers, method=upper
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise ProviderError("provider response exceeds the limit")
                return HttpResponse(
                    status=int(response.status),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=_parse_json(raw),
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise ProviderError(
                    f"provider returned HTTP {exc.code} with oversized body"
                ) from None
            return HttpResponse(int(exc.code), {}, _parse_json(raw))
        except (TimeoutError, urllib.error.URLError):
            if upper in {"POST", "PUT"}:
                raise AmbiguousEffectError(
                    "provider mutation outcome is unknown; reconcile before any retry"
                ) from None
            raise ProviderError("provider read failed") from None


def _parse_json(raw: bytes) -> object:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider returned malformed JSON") from exc


def require_success(response: HttpResponse, *, expected: tuple[int, ...] = (200,)) -> object:
    if response.status not in expected:
        raise ProviderError(f"provider returned HTTP {response.status}")
    return response.body


def fixed_id(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise ProviderError(f"{field} must be a bounded non-empty string")
    if any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for char in value
    ):
        raise ProviderError(f"{field} contains forbidden characters")
    return value
