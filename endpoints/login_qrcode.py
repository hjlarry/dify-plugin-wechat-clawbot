from __future__ import annotations

import html
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
import segno
from dify_plugin import Endpoint
from werkzeug import Request, Response

DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_BOT_TYPE = "3"
STATE_STORAGE_KEY = "weixin_http_bridge:state"


class WeixinApiError(RuntimeError):
    pass


def _ensure_trailing_slash(value: str) -> str:
    return value if value.endswith("/") else f"{value}/"


def _setting_str(settings: Mapping[str, Any], key: str, default: str = "") -> str:
    value = settings.get(key, default)
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _build_qrcode_data_uri(qrcode_url: str) -> str:
    qr = segno.make(qrcode_url, error="m")
    return qr.svg_data_uri(scale=8, border=2)


def _build_qrcode_html(*, qrcode: str, qrcode_url: str, qrcode_data_uri: str, base_url: str) -> str:
    qrcode_safe = html.escape(qrcode)
    qrcode_url_safe = html.escape(qrcode_url)
    qrcode_data_uri_safe = html.escape(qrcode_data_uri)
    base_url_safe = html.escape(base_url)
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Weixin Login QR</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #111; }}
    .card {{ max-width: 560px; border: 1px solid #e5e7eb; border-radius: 12px; padding: 20px; }}
    .title {{ font-size: 20px; font-weight: 700; margin: 0 0 12px; }}
    .meta {{ color: #374151; font-size: 14px; margin-top: 12px; word-break: break-all; }}
    img {{ width: 280px; height: 280px; border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px; background: #fff; }}
    .hint {{ font-size: 12px; color: #6b7280; }}
  </style>
</head>
<body>
  <div class=\"card\">
    <p class=\"title\">微信登录二维码</p>
    <p>请使用微信扫描下面二维码完成登录。</p>
    <img src=\"{qrcode_data_uri_safe}\" alt=\"Weixin QR Code\" />
    <p class=\"meta\">qrcode_url: {qrcode_url_safe}</p>
  </div>
</body>
</html>
"""


class WeixinLoginQrcodeEndpoint(Endpoint):
    def _load_state(self) -> dict[str, Any]:
        try:
            if not self.session.storage.exist(STATE_STORAGE_KEY):
                return {}
            raw = self.session.storage.get(STATE_STORAGE_KEY)
            if not raw:
                return {}
            state = json.loads(raw.decode("utf-8"))
            if isinstance(state, dict):
                return state
        except Exception:
            return {}
        return {}

    def _save_state(self, state: Mapping[str, Any]) -> None:
        self.session.storage.set(STATE_STORAGE_KEY, json.dumps(dict(state), ensure_ascii=False).encode("utf-8"))

    def _weixin_get_json(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict[str, Any],
        route_tag: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        base = _ensure_trailing_slash(base_url)
        query = urlencode(params)
        url = urljoin(base, endpoint)
        if query:
            url = f"{url}?{query}"

        headers: dict[str, str] = {}
        if route_tag:
            headers["SKRouteTag"] = route_tag

        try:
            with httpx.Client(timeout=timeout_ms / 1000.0) as client:
                response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise WeixinApiError(f"weixin_get_failed: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise WeixinApiError(f"weixin_get_http_{response.status_code}: {response.text[:300]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise WeixinApiError(f"weixin_get_invalid_json_response: {response.text[:300]}") from exc

        if not isinstance(data, dict):
            raise WeixinApiError("weixin_get_json_response_must_be_object")

        return data

    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        base_url = _setting_str(settings, "api_base_url", DEFAULT_API_BASE_URL).strip() or DEFAULT_API_BASE_URL
        route_tag = _setting_str(settings, "route_tag", "").strip()
        bot_type = _setting_str(settings, "bot_type", DEFAULT_BOT_TYPE).strip() or DEFAULT_BOT_TYPE

        try:
            data = self._weixin_get_json(
                base_url=base_url,
                endpoint="ilink/bot/get_bot_qrcode",
                params={"bot_type": bot_type},
                route_tag=route_tag,
                timeout_ms=15_000,
            )
        except WeixinApiError as exc:
            return Response(str(exc), status=502, content_type="text/plain; charset=utf-8")

        qrcode = str(data.get("qrcode") or "").strip()
        qrcode_url = str(data.get("qrcode_img_content") or "").strip()
        if not qrcode or not qrcode_url:
            return Response("invalid qrcode payload from upstream", status=502, content_type="text/plain; charset=utf-8")

        try:
            qrcode_data_uri = _build_qrcode_data_uri(qrcode_url)
        except Exception as exc:
            return Response(f"qrcode_render_failed: {exc}", status=500, content_type="text/plain; charset=utf-8")

        state = self._load_state()
        state["qrcode"] = qrcode
        state["qrcode_img_content"] = qrcode_url
        state["login_status"] = "wait"
        state["api_base_url"] = base_url
        state["route_tag"] = route_tag
        state["bot_type"] = bot_type
        self._save_state(state)

        html_page = _build_qrcode_html(
            qrcode=qrcode,
            qrcode_url=qrcode_url,
            qrcode_data_uri=qrcode_data_uri,
            base_url=base_url,
        )
        return Response(html_page, status=200, content_type="text/html; charset=utf-8")
