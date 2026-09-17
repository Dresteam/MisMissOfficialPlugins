"""专属昵称插件。

观众可通过直播间弹幕自助设置专属昵称,设置后**所有**涉及该用户的事件
中其用户名都会被替换为专属昵称——欢迎、礼物感谢、签到等插件的文案
会自动跟着变。

指令(在直播间发送):
    昵称            查看自己的专属昵称
    昵称 xxx        设置专属昵称为 xxx
    重置昵称        取消专属昵称,恢复真实用户名

实现要点:
- ``apply_nickname`` 以**最高优先级**监听 ``LivestreamUserEvent``
  (弹幕/礼物/进入/关注/提问/跨房弹幕/跨房礼物的共同基类),在其余插件
  之前改写 ``event.user.display_name``。事件里的用户对象每次事件都新建,
  故覆盖是单事件作用域、不会泄漏。
- ``on_command`` 以次高优先级监听弹幕,并且是**同步方法**——只有同步
  handler 调 ``event.cancel()`` 才能阻断传播(异步 handler 由事件总线
  并发调度,执行时其他 handler 早已派发完毕)。
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent, LivestreamUserEvent

_log = get_logger(__name__)

_DATA_FILE = "nicknames.json"

# 改写显示名——必须早于所有其他插件(默认优先级 0)
_PRIORITY_REWRITE = 1000
# 处理并消费指令——仍高于其他插件,以便取消传播
_PRIORITY_COMMAND = 900

# 文案默认值(与 _conf_schema.json 保持一致,便于离线运行)
_DEFAULT_NOT_SET = "@{user} 你还没有专属昵称哦"
_DEFAULT_RESET = "@{user} 专属昵称已重置"
_DEFAULT_SHOW = "@{user} 你的专属昵称是「{nick}」"
_DEFAULT_SET = "@{user} 专属昵称已设置为「{nick}」"
_DEFAULT_TOO_LONG = "@{user} 昵称最多 {max} 个字"


def _render(template: str, user: str, **values: Any) -> str:
    """替换文案占位符。

    用 ``str.replace`` 而非 ``str.format``——用户昵称里可能出现 ``{}``,
    format 会抛异常。此为项目既有惯例。
    """
    result = template.replace("{user}", user)
    for key, value in values.items():
        result = result.replace("{" + key + "}", str(value))
    return result


class NicknamePlugin(Plugin):
    """专属昵称插件 —— 昵称映射由 Web UI 管理。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        # {user_id: nickname}
        self._nicks: dict[int, str] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        self._load_nicks()
        _log.info(
            "[Nickname] 就绪 (plugin_id={})  已设置昵称={}人",
            self.plugin_id, len(self._nicks),
        )

    async def terminate(self) -> None:
        self._save_nicks()

    # ------------------------------------------------------------------ #
    # 事件处理
    # ------------------------------------------------------------------ #

    @event_handler(priority=_PRIORITY_REWRITE)
    def apply_nickname(self, event: LivestreamUserEvent) -> None:
        """把所有涉及该用户的事件中的用户名替换为专属昵称。

        **必须保持同步**——同步 handler 在总线内联执行,能保证先于所有
        低优先级 handler 完成改写;若改成 async,它会被 ``create_task``
        并发调度,其他插件可能先看到未改写的用户名。
        """
        if not self._nicks:
            return
        user = event.user
        # 匿名用户 id 为 0,不参与昵称
        uid = user.id
        if not uid:
            return
        nick = self._nicks.get(uid)
        if nick:
            user.display_name = nick

    @event_handler(priority=_PRIORITY_COMMAND)
    def on_command(self, event: LiveMessageEvent) -> None:
        """处理 ``昵称`` / ``昵称 xxx`` / ``重置昵称``。

        指令**不带方括号**——按整条消息精确匹配。

        **必须保持同步**——``event.cancel()`` 只在同步 handler 中有效。
        发送回执是异步操作,改用 ``create_task`` 发起。
        """
        cfg = self._config
        if cfg is None:
            return

        reply = self._handle_command(event, cfg)
        if reply is None:
            return

        if cfg.get_bool("block_command_message", True):
            event.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无事件循环(例如离线测试):仅改状态,不回执
            return
        loop.create_task(self._send(event.livestream, reply))

    def _handle_command(self, event: LiveMessageEvent, cfg: MissConfig) -> str | None:
        """解析指令并更新昵称状态,返回渲染好的回执;非指令返回 None。"""
        text = event.message.strip()

        cmd_show = cfg.get_str("cmd_show", "昵称").strip() or "昵称"
        cmd_reset = cfg.get_str("cmd_reset", "重置昵称").strip() or "重置昵称"
        uid = event.user.id
        if not uid:
            return None

        user = event.user.name

        # 1. 重置——先判定,避免被它的后缀误匹配成设置指令
        if text == cmd_reset:
            if self._nicks.pop(uid, None) is None:
                return _render(cfg.get_str("msg_not_set", _DEFAULT_NOT_SET), user)
            self._save_nicks()
            _log.info("[Nickname] 用户 {} 已重置昵称", uid)
            return _render(cfg.get_str("msg_reset", _DEFAULT_RESET), user)

        # 2. 查看
        if text == cmd_show:
            nick = self._nicks.get(uid)
            if not nick:
                return _render(cfg.get_str("msg_not_set", _DEFAULT_NOT_SET), user)
            return _render(cfg.get_str("msg_show", _DEFAULT_SHOW), user, nick=nick)

        # 3. 设置——要求 cmd_show 后跟空白,避免 "昵称abc" 这类歧义
        prefix = cmd_show + " "
        if not text.startswith(prefix):
            return None
        nick = text[len(prefix):].strip()
        if not nick:
            return None
        max_len = cfg.get_int("max_length", 12)
        if len(nick) > max_len:
            return _render(
                cfg.get_str("msg_too_long", _DEFAULT_TOO_LONG), user, max=max_len
            )
        self._nicks[uid] = nick
        self._save_nicks()
        _log.info("[Nickname] 用户 {} 设置昵称为 {!r}", uid, nick)
        return _render(cfg.get_str("msg_set", _DEFAULT_SET), user, nick=nick)

    async def _send(self, livestream: Any, message: str) -> None:
        """发送回执。"""
        try:
            await livestream.send_message(message)
        except Exception as e:  # noqa: BLE001 — 发送失败不应影响其他插件
            _log.warning("[Nickname] 发送回执失败: {}", e)

    # ------------------------------------------------------------------ #
    # Web API(插件主页)
    # ------------------------------------------------------------------ #

    def register_routes(self, router: Any) -> None:
        from fastapi import Body
        from fastapi.responses import JSONResponse

        @router.get("/list")
        async def list_nicks():
            return JSONResponse(self._to_api_rows())

        @router.post("/delete")
        async def delete_nick(body: dict = Body(...)):
            try:
                uid = int(body.get("user_id", -1))
            except (TypeError, ValueError):
                uid = -1
            self._nicks.pop(uid, None)
            self._save_nicks()
            _log.info("[Nickname] 面板删除用户 {} 的昵称", uid)
            return JSONResponse({"ok": True})

    def _to_api_rows(self) -> list[dict[str, Any]]:
        """序列化为 UI 列表格式(按用户 ID 升序)。"""
        return [
            {"user_id": uid, "nickname": self._nicks[uid]}
            for uid in sorted(self._nicks)
        ]

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #

    def _load_nicks(self) -> None:
        data = self.data.read_json(_DATA_FILE) if self.data else None
        self._nicks = {}
        if isinstance(data, dict):
            for key, value in data.items():
                try:
                    uid = int(key)
                except (TypeError, ValueError):
                    continue  # JSON 键是字符串,非法键跳过
                nick = str(value).strip()
                if uid and nick:
                    self._nicks[uid] = nick

    def _save_nicks(self) -> None:
        if self.data is None:
            return
        try:
            # JSON 的键只能是字符串
            self.data.write_json(
                _DATA_FILE, {str(uid): nick for uid, nick in self._nicks.items()}
            )
        except OSError as e:
            _log.warning("[Nickname] 保存昵称失败: {}", e)
