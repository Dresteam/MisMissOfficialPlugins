"""关键词回复插件。

收到与规则匹配的直播间消息时,自动回复指定内容。

规则(关键词 / 匹配方式 / 回复内容)通过 Web UI(插件主页)管理,
而非 config —— 持久化到插件数据目录,多账户实例间数据隔离。

匹配方式:
    contains —— 包含(关键词为消息子串)
    exact    —— 完整匹配(消息与关键词完全相同)
    regex    —— 正则表达式(re.search)
"""

from __future__ import annotations

import re
from typing import Any

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent

_log = get_logger(__name__)

_DATA_FILE = "keyword_rules.json"
_MATCH_TYPES = ("contains", "exact", "regex")
_MATCH_LABELS = {"contains": "包含", "exact": "完整匹配", "regex": "正则表达式"}


class KeywordReplyPlugin(Plugin):
    """关键词回复插件 —— 规则由 Web UI 管理。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        # 规则列表:{"id": int, "keyword": str, "match_type": str, "reply": str, "enabled": bool}
        self._rules: list[dict[str, Any]] = []
        self._next_id: int = 1
        self._regex_cache: dict[int, re.Pattern] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        self._load_rules()
        _log.info(
            "[KeywordReply] 就绪 (plugin_id={})  规则={}条",
            self.plugin_id, len(self._rules),
        )

    async def terminate(self) -> None:
        self._save_rules()

    # ------------------------------------------------------------------ #
    # 事件处理
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_message(self, event: LiveMessageEvent) -> None:
        text = event.message.strip()
        if not text or not self._rules:
            return
        # 所有命中的启用规则依次回复
        for rule in self._rules:
            if rule.get("enabled", True) and self._match(rule, text):
                await event.livestream.send_message(str(rule.get("reply", "")))

    def _match(self, rule: dict[str, Any], text: str) -> bool:
        """按规则匹配方式判断消息是否命中。"""
        keyword = str(rule.get("keyword", ""))
        match_type = rule.get("match_type", "contains")
        if match_type == "exact":
            return text == keyword
        if match_type == "regex":
            rid = int(rule["id"])
            pattern = self._regex_cache.get(rid)
            if pattern is None:
                try:
                    pattern = re.compile(keyword)
                except re.error:
                    return False
                self._regex_cache[rid] = pattern
            return pattern.search(text) is not None
        return keyword in text  # contains

    # ------------------------------------------------------------------ #
    # Web API(插件主页)
    # ------------------------------------------------------------------ #

    def register_routes(self, router: Any) -> None:
        from fastapi import Body
        from fastapi.responses import JSONResponse

        @router.get("/list")
        async def list_rules():
            return JSONResponse(self._to_api_rules())

        @router.post("/add")
        async def add_rule(body: dict = Body(...)):
            keyword = str(body.get("keyword", "")).strip()
            reply = str(body.get("reply", "")).strip()
            match_type = str(body.get("match_type", "contains"))
            if not keyword:
                return JSONResponse({"ok": False, "error": "关键词不能为空"}, status_code=400)
            if not reply:
                return JSONResponse({"ok": False, "error": "回复内容不能为空"}, status_code=400)
            if match_type not in _MATCH_TYPES:
                return JSONResponse({"ok": False, "error": "匹配方式无效"}, status_code=400)
            if match_type == "regex":
                try:
                    re.compile(keyword)
                except re.error as e:
                    return JSONResponse(
                        {"ok": False, "error": f"正则表达式无效: {e}"}, status_code=400
                    )
            rule = {
                "id": self._next_id,
                "keyword": keyword,
                "match_type": match_type,
                "reply": reply,
                "enabled": True,
            }
            self._next_id += 1
            self._rules.append(rule)
            self._save_rules()
            _log.info("[KeywordReply] 已添加规则 #{} '{}' ({})", rule["id"], keyword, match_type)
            return JSONResponse({"ok": True, "id": rule["id"]})

        @router.post("/delete")
        async def delete_rule(body: dict = Body(...)):
            try:
                rid = int(body.get("id", -1))
            except (TypeError, ValueError):
                rid = -1
            self._regex_cache.pop(rid, None)
            self._rules = [r for r in self._rules if r["id"] != rid]
            self._save_rules()
            return JSONResponse({"ok": True})

        @router.post("/toggle")
        async def toggle_rule(body: dict = Body(...)):
            try:
                rid = int(body.get("id", -1))
            except (TypeError, ValueError):
                rid = -1
            enabled = bool(body.get("enabled", True))
            for r in self._rules:
                if r["id"] == rid:
                    r["enabled"] = enabled
                    self._save_rules()
                    break
            return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #

    def _to_api_rules(self) -> list[dict[str, Any]]:
        """序列化为 UI 列表格式(附带中文匹配方式标签)。"""
        return [
            {
                "id": r["id"],
                "keyword": r["keyword"],
                "match_type": r["match_type"],
                "match_label": _MATCH_LABELS.get(r["match_type"], r["match_type"]),
                "reply": r["reply"],
                "enabled": bool(r.get("enabled", True)),
            }
            for r in self._rules
        ]

    def _load_rules(self) -> None:
        data = self.data.read_json(_DATA_FILE) if self.data else None
        self._rules = []
        self._regex_cache = {}
        if isinstance(data, dict) and isinstance(data.get("rules"), list):
            for d in data["rules"]:
                if not isinstance(d, dict):
                    continue
                match_type = d.get("match_type")
                self._rules.append({
                    "id": int(d.get("id", 0)),
                    "keyword": str(d.get("keyword", "")),
                    "match_type": match_type if match_type in _MATCH_TYPES else "contains",
                    "reply": str(d.get("reply", "")),
                    "enabled": bool(d.get("enabled", True)),
                })
        self._next_id = max((r["id"] for r in self._rules), default=0) + 1

    def _save_rules(self) -> None:
        if self.data is None:
            return
        try:
            self.data.write_json(_DATA_FILE, {"rules": self._rules})
        except OSError as e:
            _log.warning("[KeywordReply] 保存规则失败: {}", e)
