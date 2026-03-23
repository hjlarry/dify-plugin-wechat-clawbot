from __future__ import annotations

from typing import Any

from dify_plugin import ToolProvider


class WeixinBridgeProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        pass
