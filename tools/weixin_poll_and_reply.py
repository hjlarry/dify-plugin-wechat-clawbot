from __future__ import annotations

import base64
import json
import secrets
import time
from collections.abc import Generator, Mapping
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "dify-weixin-http-bridge/0.1.0"
STATE_STORAGE_KEY = "weixin_http_bridge:state"
STATUS_TIMEOUT_MS = 12_000
UPDATES_TIMEOUT_MS = 35_000
MAX_MESSAGES_PER_RUN = 20


class WeixinApiError(RuntimeError):
    pass


def _random_wechat_uin() -> str:
    value = secrets.randbits(32)
    return base64.b64encode(str(value).encode("utf-8")).decode("utf-8")


def _ensure_trailing_slash(value: str) -> str:
    return value if value.endswith("/") else f"{value}/"


def _normalize_timeout_ms(value: Any, default_ms: int, min_ms: int = 1_000, max_ms: int = 120_000) -> int:
    try:
        timeout_ms = int(value if value is not None else default_ms)
    except (TypeError, ValueError):
        timeout_ms = default_ms
    return max(min_ms, min(max_ms, timeout_ms))


def _extract_app_id(app_value: Any) -> str:
    if isinstance(app_value, Mapping):
        return str(app_value.get("app_id") or app_value.get("id") or "").strip()
    if isinstance(app_value, str):
        return app_value.strip()
    return ""


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


class WeixinPollAndReplyTool(Tool):
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

    def _build_headers(self, *, token: str = "", route_tag: str = "", extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_wechat_uin(),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if route_tag:
            headers["SKRouteTag"] = route_tag
        if extra:
            headers.update(extra)
        return headers

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

        headers = self._build_headers(route_tag=route_tag, extra=extra_headers)
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
        headers = self._build_headers(
            token=bot_token,
            route_tag=route_tag,
            extra={"Content-Type": "application/json"},
        )

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

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        app_id = _extract_app_id(tool_parameters.get("app"))
        if not app_id:
            raise ValueError("missing app")

        state = self._load_state()
        base_url = (
            str(state.get("api_base_url") or DEFAULT_API_BASE_URL).strip()
            or DEFAULT_API_BASE_URL
        )
        route_tag = str(state.get("route_tag") or "").strip()
        status_timeout_ms = _normalize_timeout_ms(STATUS_TIMEOUT_MS, default_ms=STATUS_TIMEOUT_MS)
        updates_timeout_ms = _normalize_timeout_ms(UPDATES_TIMEOUT_MS, default_ms=UPDATES_TIMEOUT_MS)

        qrcode = str(state.get("qrcode") or "").strip()
        status_result: dict[str, Any] = {}
        if qrcode:
            try:
                status_result = self._weixin_get_json(
                    base_url=base_url,
                    endpoint="ilink/bot/get_qrcode_status",
                    params={"qrcode": qrcode},
                    route_tag=route_tag,
                    timeout_ms=status_timeout_ms,
                    extra_headers={"iLink-App-ClientVersion": "1"},
                )
            except WeixinApiError as exc:
                yield self.create_json_message(
                    {
                        "ok": False,
                        "result": "status_check_failed",
                        "error": str(exc),
                    }
                )
                return

            login_status = str(status_result.get("status") or "")
            state["login_status"] = login_status
            if login_status == "confirmed":
                bot_token = str(status_result.get("bot_token") or "").strip()
                if bot_token:
                    state["bot_token"] = bot_token
                if status_result.get("ilink_bot_id"):
                    state["account_id"] = str(status_result.get("ilink_bot_id") or "").strip()
                if status_result.get("ilink_user_id"):
                    state["user_id"] = str(status_result.get("ilink_user_id") or "").strip()
                if status_result.get("baseurl"):
                    state["api_base_url"] = str(status_result.get("baseurl") or "").strip()
                # Persist the refreshed bot_token immediately, so a later failure
                # in getupdates/sendmessage won't lose the new token.
                self._save_state(state)

        bot_token = str(state.get("bot_token") or "").strip()
        if not bot_token:
            state["api_base_url"] = base_url
            state["route_tag"] = route_tag
            self._save_state(state)
            yield self.create_json_message(
                {
                    "ok": True,
                    "result": "waiting_login",
                    "has_qrcode": bool(qrcode),
                    "login_status": state.get("login_status") or "",
                    "received_text_messages": 0,
                    "processed_messages": 0,
                    "replied_messages": 0,
                    "errors": [],
                }
            )
            return

        get_updates_buf = str(state.get("get_updates_buf") or "")
        conversation_map = state.get("conversation_map")
        if not isinstance(conversation_map, dict):
            conversation_map = {}

        updates_payload = {
            "get_updates_buf": get_updates_buf,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        try:
            updates = self._weixin_post_json(
                base_url=base_url,
                endpoint="ilink/bot/getupdates",
                payload=updates_payload,
                bot_token=bot_token,
                route_tag=route_tag,
                timeout_ms=updates_timeout_ms,
            )
        except WeixinApiError as exc:
            error_text = str(exc)
            if "weixin_post_http_401" in error_text or "weixin_post_http_403" in error_text:
                state.pop("bot_token", None)
                self._save_state(state)
                yield self.create_json_message(
                    {
                        "ok": True,
                        "result": "waiting_login",
                        "login_status": state.get("login_status") or "",
                        "error": "bot_token_expired_or_invalid",
                    }
                )
                return
            yield self.create_json_message(
                {
                    "ok": False,
                    "result": "getupdates_failed",
                    "error": error_text,
                }
            )
            return

        if isinstance(updates.get("get_updates_buf"), str):
            state["get_updates_buf"] = updates["get_updates_buf"]

        raw_messages = updates.get("msgs")
        if not isinstance(raw_messages, list):
            raw_messages = []

        text_messages: list[dict[str, str]] = []
        for message in raw_messages:
            if not isinstance(message, Mapping):
                continue
            from_user_id = str(message.get("from_user_id") or "").strip()
            if not from_user_id:
                continue
            text = _extract_text_from_item_list(message.get("item_list"))
            if text == "":
                continue
            text_messages.append(
                {
                    "from_user_id": from_user_id,
                    "context_token": str(message.get("context_token") or "").strip(),
                    "text": text,
                }
            )

        processed_count = 0
        replied_count = 0
        errors: list[str] = []

        for incoming in text_messages[:MAX_MESSAGES_PER_RUN]:
            processed_count += 1
            from_user_id = incoming["from_user_id"]
            conversation_key = f"{app_id}:{from_user_id}"
            conversation_id = str(conversation_map.get(conversation_key) or "").strip()

            invoke_params: dict[str, Any] = {
                "app_id": app_id,
                "query": incoming["text"],
                "inputs": {},
                "response_mode": "blocking",
            }

            try:
                app_result = self.session.app.chat.invoke(**invoke_params)
            except Exception as exc:
                errors.append(f"app_invoke_failed[{from_user_id}]: {exc}")
                continue

            new_conversation_id = str(app_result.get("conversation_id") or "").strip()
            if new_conversation_id:
                conversation_map[conversation_key] = new_conversation_id

            answer = _pick_answer(app_result)
            if answer == "":
                errors.append(f"empty_app_answer[{from_user_id}]")
                continue

            send_msg: dict[str, Any] = {
                "from_user_id": "",
                "to_user_id": from_user_id,
                "client_id": f"dify-weixin-{int(time.time() * 1000)}-{secrets.token_hex(4)}",
                "message_type": 2,
                "message_state": 2,
                "item_list": [
                    {
                        "type": 1,
                        "text_item": {"text": answer},
                    }
                ],
            }
            if incoming["context_token"]:
                send_msg["context_token"] = incoming["context_token"]

            send_payload = {
                "msg": send_msg,
                "base_info": {"channel_version": CHANNEL_VERSION},
            }

            try:
                self._weixin_post_json(
                    base_url=base_url,
                    endpoint="ilink/bot/sendmessage",
                    payload=send_payload,
                    bot_token=bot_token,
                    route_tag=route_tag,
                    timeout_ms=15_000,
                )
            except WeixinApiError as exc:
                errors.append(f"sendmessage_failed[{from_user_id}]: {exc}")
                continue

            replied_count += 1

        state["conversation_map"] = conversation_map
        state["bot_token"] = bot_token
        state["api_base_url"] = base_url
        state["route_tag"] = route_tag
        self._save_state(state)

        yield self.create_json_message(
            {
                "ok": True,
                "result": "timeout_or_no_message" if len(text_messages) == 0 else "processed",
                "login_status": state.get("login_status") or "",
                "received_text_messages": len(text_messages),
                "processed_messages": processed_count,
                "replied_messages": replied_count,
                "skipped_messages": max(0, len(text_messages) - processed_count),
                "errors": errors,
            }
        )
