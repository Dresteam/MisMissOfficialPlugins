"""定时消息插件。

注册多条定时消息，每条可指定目标直播间，
由 Bot 的定时消息队列统一轮转发送（间隔由服务器 bot.timer_interval 控制）。

配置格式（_conf_schema.json → timer_messages）::

    "140216322:欢迎来到直播间～"
    "869144824:每晚 8 点准时开播，不见不散 💕"
"""

from __future__ import annotations

import re

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent

_log = get_logger(__name__)


class TimerMessagesPlugin(Plugin):
    """定时消息插件——多条定时消息，每条指定直播间。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        self._registered_ids: list[str] = []
        self._registered: bool = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        if not config.get_bool("enabled", True):
            _log.info("[TimerMessages] 已禁用，跳过注册")
            return
        self._register_all()

    async def terminate(self) -> None:
        """卸载时取消所有定时消息。"""
        bot = self._resolve_bot()
        if bot is not None and self._registered_ids:
            bot.unregister_timer_messages(self._registered_ids)
            _log.info("[TimerMessages] 已取消 {} 条定时消息", len(self._registered_ids))
        self._registered_ids.clear()
        self._registered = False  # 重置标志，重新启用时可再次注册

    async def on_enable(self) -> None:
        """禁用后重新启用 → 重新注册定时消息。"""
        self._registered = False
        self._register_all()

    # ------------------------------------------------------------------ #
    # 事件处理器（兜底：bot 不可用时延迟注册）
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_message(self, event: LiveMessageEvent) -> None:
        if not self._registered:
            self._register_all()

    # ------------------------------------------------------------------ #
    # 注册逻辑
    # ------------------------------------------------------------------ #

    def _register_all(self) -> None:
        """解析配置并注册所有定时消息。"""
        if self._registered:
            return  # 已注册，不重复注册
        cfg = self._config
        if cfg is None:
            return
        bot = self._resolve_bot()
        if bot is None:
            return  # 等 on_message 兜底重试

        entries = self._parse_entries(cfg.get_str("timer_messages_text", ""))
        if not entries:
            _log.info("[TimerMessages] 未配置任何定时消息")
            self._registered = True
            return

        count = 0
        for live_id, message in entries:
            mid = bot.register_timer_message(live_id, message)
            self._registered_ids.append(mid)
            count += 1
            first_line = message.split("\n")[0]
            _log.info("[TimerMessages] 已注册: live={} msg={}", live_id, first_line[:30])

        self._registered = True
        _log.info("[TimerMessages] 注册完成: {} 条", count)

    @staticmethod
    def _parse_entries(text: str) -> list[tuple[int, str]]:
        """解析多行文本为 (live_id, message) 列表。

        规则：
        - 以 ``数字:`` 开头的行 → 新条目（: 后为消息首行）
        - 其他非空行 → 追加到上一条消息（以换行连接）
        - 空行忽略
        """
        entries: list[tuple[int, str]] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            # 检查是否为 "数字:" 开头的新条目
            m = re.match(r"^(\d+):(.*)$", stripped)
            if m:
                live_id = int(m.group(1))
                message = m.group(2).strip()
                if live_id > 0 and message:
                    entries.append((live_id, message))
                else:
                    _log.warning("[TimerMessages] 忽略无效条目: {}", stripped[:50])
            elif entries:
                # 续行：追加到上一条消息
                live_id, prev = entries[-1]
                entries[-1] = (live_id, f"{prev}\n{stripped}")
            else:
                _log.warning("[TimerMessages] 忽略孤立续行: {}", stripped[:50])
        return entries

    def _resolve_bot(self):
        """获取 bot：优先 server 引用，其次事件捕获。"""
        srv = getattr(self, '_server', None)
        if srv is not None and srv.bot_available:
            return srv.bot
        return None
