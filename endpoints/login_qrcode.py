from __future__ import annotations

import base64
import html
import json
import secrets
import time
from collections.abc import Generator, Mapping
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
import segno
from dify_plugin import Endpoint
from werkzeug import Request, Response

DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_BOT_TYPE = "3"
CHANNEL_VERSION = "weixin-clawbot-endpoint/0.1.0"


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


def _random_wechat_uin() -> str:
    value = secrets.randbits(32)
    return base64.b64encode(str(value).encode("utf-8")).decode("utf-8")


def _build_qrcode_data_uri(qrcode_url: str) -> str:
    qr = segno.make(qrcode_url, error="m")
    return qr.svg_data_uri(scale=8, border=2)


def _extract_text_from_item_list(item_list: Any) -> str:
    if not isinstance(item_list, list):
        return ""
    for item in item_list:
        if not isinstance(item, Mapping):
            continue
        text_item = item.get("text_item")
        if isinstance(text_item, Mapping):
            text = text_item.get("text")
            if text is not None:
                return str(text)
    return ""


def _pick_answer(result: Mapping[str, Any]) -> str:
    answer = result.get("answer")
    if answer is None or answer == "":
        answer = result.get("output_text")
    if answer is None or answer == "":
        answer = result.get("message")
    return str(answer or "")


def _extract_app_id(app_value: Any) -> str:
    if isinstance(app_value, Mapping):
        return str(app_value.get("app_id") or app_value.get("id") or "").strip()
    if isinstance(app_value, str):
        return app_value.strip()
    return ""


def _build_page_start(*, qrcode_data_uri: str, qrcode_url: str, app_id: str) -> str:
    qrcode_data_uri_safe = html.escape(qrcode_data_uri)
    qrcode_url_safe = html.escape(qrcode_url)
    app_id_safe = html.escape(app_id)
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Weixin ClawBot Login</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #111; }}
    .card {{ max-width: 760px; border: 1px solid #e5e7eb; border-radius: 12px; padding: 20px; }}
    .title {{ font-size: 20px; font-weight: 700; margin: 0 0 12px; }}
    .meta {{ color: #374151; font-size: 14px; margin-top: 8px; word-break: break-all; }}
    img {{ width: 280px; height: 280px; border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px; background: #fff; }}
    #log {{ margin-top: 16px; background: #0b1220; color: #d1fae5; padding: 12px; border-radius: 8px; font-size: 12px; line-height: 1.5; height: 300px; overflow: auto; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <div class=\"card\">
    <p class=\"title\">微信ClawBot 调试页</p>
    <p>扫码后将自动轮询微信消息，并调用 Dify App 回复。</p>
    <img src=\"{qrcode_data_uri_safe}\" alt=\"Weixin QR Code\" />
    <p class=\"meta\">qrcode_url: {qrcode_url_safe}</p>
    <p class=\"meta\">app_id: {app_id_safe}</p>
    <pre id=\"log\"></pre>
  </div>
  <script>
    function appendLog(msg) {{
      const el = document.getElementById('log');
      const ts = new Date().toLocaleTimeString();
      el.textContent += `[${{ts}}] ${{msg}}\\n`;
      el.scrollTop = el.scrollHeight;
    }}
  </script>
"""


def _log_chunk(message: str) -> str:
    return f"<script>appendLog({json.dumps(message, ensure_ascii=False)});</script>\n"


class WeixinLoginQrcodeEndpoint(Endpoint):
    def _weixin_get_json(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict[str, Any],
        route_tag: str,
        timeout_ms: int,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        base = _ensure_trailing_slash(base_url)
        query = urlencode(params)
        url = urljoin(base, endpoint)
        if query:
            url = f"{url}?{query}"

        headers: dict[str, str] = {}
        if route_tag:
            headers["SKRouteTag"] = route_tag
        if extra_headers:
            headers.update(extra_headers)

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

    def _weixin_post_json(
        self,
        *,
        base_url: str,
        endpoint: str,
        payload: dict[str, Any],
        bot_token: str,
        route_tag: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        url = urljoin(_ensure_trailing_slash(base_url), endpoint)
        headers: dict[str, str] = {
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {bot_token}",
            "X-WECHAT-UIN": _random_wechat_uin(),
            "Content-Type": "application/json",
        }
        if route_tag:
            headers["SKRouteTag"] = route_tag

        try:
            with httpx.Client(timeout=timeout_ms / 1000.0) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise WeixinApiError(f"weixin_post_failed: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise WeixinApiError(f"weixin_post_http_{response.status_code}: {response.text[:300]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise WeixinApiError(f"weixin_post_invalid_json_response: {response.text[:300]}") from exc

        if not isinstance(data, dict):
            raise WeixinApiError("weixin_post_json_response_must_be_object")

        return data

    def _stream_loop(
        self,
        *,
        app_id: str,
        state: dict[str, Any],
        base_url: str,
        route_tag: str,
    ) -> Generator[str, None, None]:
        while True:
            # Step 1: status polling until bot_token exists. Once obtained, this step is skipped.
            bot_token = str(state.get("bot_token") or "").strip()
            if not bot_token:
                qrcode = str(state.get("qrcode") or "").strip()
                if not qrcode:
                    yield _log_chunk("未找到 qrcode，循环结束。")
                    return

                try:
                    status_resp = self._weixin_get_json(
                        base_url=base_url,
                        endpoint="ilink/bot/get_qrcode_status",
                        params={"qrcode": qrcode},
                        route_tag=route_tag,
                        timeout_ms=15_000,
                        extra_headers={"iLink-App-ClientVersion": "1"},
                    )
                except Exception as exc:
                    yield _log_chunk(f"status 请求失败: {exc}; 3 秒后重试")
                    time.sleep(3)
                    continue

                login_status = str(status_resp.get("status") or "")
                state["login_status"] = login_status
                if login_status == "confirmed":
                    new_token = str(status_resp.get("bot_token") or "").strip()
                    if new_token:
                        state["bot_token"] = new_token
                        if status_resp.get("ilink_bot_id"):
                            state["account_id"] = str(status_resp.get("ilink_bot_id") or "")
                        if status_resp.get("ilink_user_id"):
                            state["user_id"] = str(status_resp.get("ilink_user_id") or "")
                        if status_resp.get("baseurl"):
                            base_url = str(status_resp.get("baseurl") or "").strip() or base_url
                            state["api_base_url"] = base_url
                        yield _log_chunk("登录已确认，拿到 bot_token，开始轮询 update。")
                        continue
                yield _log_chunk(f"登录状态: {login_status or 'wait'}，3 秒后重试 status")
                time.sleep(3)
                continue

            # Step 2: getupdates polling
            try:
                updates_resp = self._weixin_post_json(
                    base_url=base_url,
                    endpoint="ilink/bot/getupdates",
                    payload={
                        "get_updates_buf": str(state.get("get_updates_buf") or ""),
                        "base_info": {"channel_version": CHANNEL_VERSION},
                    },
                    bot_token=bot_token,
                    route_tag=route_tag,
                    timeout_ms=35_000,
                )
            except Exception as exc:
                msg = str(exc)
                if "weixin_post_http_401" in msg or "weixin_post_http_403" in msg:
                    state.pop("bot_token", None)
                    yield _log_chunk("bot_token 失效，回到 status 重新获取。")
                    time.sleep(3)
                    continue
                yield _log_chunk(f"getupdates 失败: {msg}; 5 秒后重试")
                time.sleep(5)
                continue

            if isinstance(updates_resp.get("get_updates_buf"), str):
                state["get_updates_buf"] = updates_resp["get_updates_buf"]

            upstream_ret = updates_resp.get("ret")
            if upstream_ret not in (None, 0, "0"):
                yield _log_chunk(f"getupdates ret={upstream_ret}, raw={json.dumps(updates_resp, ensure_ascii=False)[:600]}")

            raw_msgs = updates_resp.get("msgs")
            if not isinstance(raw_msgs, list):
                raw_msgs = []
            yield _log_chunk(f"getupdates 返回消息数: {len(raw_msgs)}")

            text_msgs: list[dict[str, str]] = []
            for msg in raw_msgs:
                if not isinstance(msg, Mapping):
                    continue
                from_user_id = str(msg.get("from_user_id") or "").strip()
                if not from_user_id:
                    continue
                text = _extract_text_from_item_list(msg.get("item_list"))
                if text == "":
                    continue
                text_msgs.append(
                    {
                        "from_user_id": from_user_id,
                        "context_token": str(msg.get("context_token") or "").strip(),
                        "text": text,
                    }
                )

            if not text_msgs:
                yield _log_chunk("update 无新文本消息，5 秒后重试。")
                time.sleep(5)
                continue

            for incoming in text_msgs:
                from_user_id = incoming["from_user_id"]
                question = incoming["text"]
                yield _log_chunk(f"收到消息 from {from_user_id}: {question[:80]}")

                try:
                    app_result = self.session.app.chat.invoke(
                        app_id=app_id,
                        query=question,
                        inputs={},
                        response_mode="blocking",
                    )
                except Exception as exc:
                    yield _log_chunk(f"invoke_app 失败: {exc}")
                    continue

                answer = _pick_answer(app_result)
                if answer == "":
                    yield _log_chunk("invoke_app 返回空内容，跳过发送。")
                    continue

                send_payload = {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": from_user_id,
                        "client_id": f"endpoint-{int(time.time() * 1000)}-{secrets.token_hex(4)}",
                        "message_type": 2,
                        "message_state": 2,
                        "item_list": [{"type": 1, "text_item": {"text": answer}}],
                    },
                    "base_info": {"channel_version": CHANNEL_VERSION},
                }
                if incoming["context_token"]:
                    send_payload["msg"]["context_token"] = incoming["context_token"]

                try:
                    self._weixin_post_json(
                        base_url=base_url,
                        endpoint="ilink/bot/sendmessage",
                        payload=send_payload,
                        bot_token=bot_token,
                        route_tag=route_tag,
                        timeout_ms=15_000,
                    )
                    yield _log_chunk(f"已回复用户 {from_user_id}")
                except Exception as exc:
                    yield _log_chunk(f"sendmessage 失败: {exc}")

    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        app_id = _extract_app_id(settings.get("app"))
        if not app_id:
            return Response(
                "missing endpoint setting: app",
                status=400,
                content_type="text/plain; charset=utf-8",
            )

        base_url = _setting_str(settings, "api_base_url", DEFAULT_API_BASE_URL).strip() or DEFAULT_API_BASE_URL
        route_tag = _setting_str(settings, "route_tag", "").strip()
        bot_type = _setting_str(settings, "bot_type", DEFAULT_BOT_TYPE).strip() or DEFAULT_BOT_TYPE

        try:
            qr_resp = self._weixin_get_json(
                base_url=base_url,
                endpoint="ilink/bot/get_bot_qrcode",
                params={"bot_type": bot_type},
                route_tag=route_tag,
                timeout_ms=15_000,
            )
        except Exception as exc:
            return Response(f"get_bot_qrcode_failed: {exc}", status=502, content_type="text/plain; charset=utf-8")

        qrcode = str(qr_resp.get("qrcode") or "").strip()
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "").strip()
        if not qrcode or not qrcode_url:
            return Response("invalid qrcode payload from upstream", status=502, content_type="text/plain; charset=utf-8")

        try:
            qrcode_data_uri = _build_qrcode_data_uri(qrcode_url)
        except Exception as exc:
            return Response(f"qrcode_render_failed: {exc}", status=500, content_type="text/plain; charset=utf-8")

        # Keep state in memory for this long-running endpoint session.
        state: dict[str, Any] = {
            "qrcode": qrcode,
            "qrcode_img_content": qrcode_url,
            "login_status": "wait",
            "api_base_url": base_url,
            "route_tag": route_tag,
            "bot_type": bot_type,
            "app_id": app_id,
            "bot_token": "",
            "get_updates_buf": "",
        }

        def generator() -> Generator[str, None, None]:
            yield _build_page_start(
                qrcode_data_uri=qrcode_data_uri,
                qrcode_url=qrcode_url,
                app_id=app_id,
            )
            yield _log_chunk("页面已建立，开始后台循环。")
            yield from self._stream_loop(app_id=app_id, state=state, base_url=base_url, route_tag=route_tag)
            yield "</body></html>"

        return Response(generator(), status=200, content_type="text/html; charset=utf-8")
