from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from copilot_agent.conversation_store import redact_cookie_header
from copilot_agent.settings import settings
from copilot_agent.tools.whitelist import validate_get_path, validate_post_path

log = logging.getLogger(__name__)


def _merge_cookie(stored: str | None, override: str | None) -> str | None:
    if override and override.strip():
        return override.strip()
    return stored


class WatermarkHttpTools:
    """httpx calls to WATERMARK_API_BASE_URL only; paths validated by whitelist."""

    def __init__(self) -> None:
        self._base = settings.watermark_api_base_url.rstrip("/")

    async def http_get(
        self, path: str, cookie_header: str | None = None, stored_cookie: str | None = None
    ) -> dict[str, Any]:
        err = validate_get_path(path)
        if err:
            return {"ok": False, "error": err}
        cookie = _merge_cookie(stored_cookie, cookie_header)
        log.info("http_get path=%s cookie=%s", path.split("?", 1)[0], redact_cookie_header(cookie))
        async with httpx.AsyncClient(base_url=self._base, timeout=60.0) as client:
            r = await client.get(path, headers=self._headers_get(cookie))
        return await self._response_payload(r)

    async def http_post(
        self,
        path: str,
        json_body: dict[str, Any],
        cookie_header: str | None = None,
        stored_cookie: str | None = None,
        idempotency_key: str | None = None,
        *,
        allow_job_post: bool,
        user_confirmed_dangerous: bool,
    ) -> dict[str, Any]:
        err = validate_post_path(path)
        if err:
            return {"ok": False, "error": err}
        base = path.split("?", 1)[0]
        if base == "/api/v1/jobs/watermark":
            if not allow_job_post:
                return {
                    "ok": False,
                    "error": "POST /api/v1/jobs/watermark disabled (set COPILOT_ALLOW_JOB_POST=true and confirm_dangerous on chat request).",
                }
            if not user_confirmed_dangerous:
                return {
                    "ok": False,
                    "error": "Dangerous POST requires confirm_dangerous=true on the chat API request.",
                }
        cookie = _merge_cookie(stored_cookie, cookie_header)
        log.info("http_post path=%s cookie=%s", path, redact_cookie_header(cookie))
        headers = self._headers_post(cookie)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(base_url=self._base, timeout=120.0) as client:
            r = await client.post(path, json=json_body, headers=headers)
        body = await self._response_payload(r)
        if base == "/api/v1/auth/login" and r.is_success:
            set_cookies = _collect_set_cookie_headers(r)
            if set_cookies:
                joined = ", ".join(set_cookies)
                body["set_cookie_redacted"] = _redact_set_cookie_for_tool_result(joined)
                body["_raw_set_cookie_for_store_only"] = set_cookies
        return body

    def _headers_get(self, cookie: str | None) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        return h

    def _headers_post(self, cookie: str | None) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        return h

    async def _response_payload(self, r: httpx.Response) -> dict[str, Any]:
        ct = (r.headers.get("content-type") or "").lower()
        text = r.text
        parsed: Any
        if "application/json" in ct:
            try:
                parsed = r.json()
            except json.JSONDecodeError:
                parsed = text
        else:
            parsed = text[:8000] if len(text) > 8000 else text
        return {
            "ok": r.is_success,
            "status_code": r.status_code,
            "body": parsed,
        }


def _collect_set_cookie_headers(r: httpx.Response) -> list[str]:
    h = r.headers
    if hasattr(h, "get_list"):
        raw = h.get_list("set-cookie")
        if raw:
            return list(raw)
    sc = r.headers.get("set-cookie")
    return [sc] if sc else []


def _redact_set_cookie_for_tool_result(raw: str) -> str:
    out = []
    for part in raw.split(","):
        p = part.strip()
        pl = p.lower()
        if pl.startswith("wmsessionid=") or "wmsessionid=" in pl:
            out.append("WMSESSIONID=***; ...")
        else:
            out.append("(other Set-Cookie omitted)")
    return "; ".join(out) if out else "***"


def extract_session_cookie_from_set_cookie_headers(headers: list[str]) -> str | None:
    """Return `WMSESSIONID=value` for Cookie header from one or more Set-Cookie header lines."""
    for hdr in headers:
        lower = hdr.lower()
        idx = lower.find("wmsessionid=")
        if idx < 0:
            continue
        rest = hdr[idx:]
        semi = rest.find(";")
        pair = rest[: semi if semi >= 0 else len(rest)].strip()
        if pair:
            return pair
    return None
