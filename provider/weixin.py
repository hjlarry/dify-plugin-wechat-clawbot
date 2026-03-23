from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError


class WeixinBridgeProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        api_base_url = str(credentials.get("api_base_url") or "").strip()
        if not api_base_url:
            return

        parsed = urlparse(api_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ToolProviderCredentialValidationError("api_base_url must be a valid HTTP(S) URL")
