"""关注感谢插件。

用户关注直播间时自动发送感谢消息（按顺序轮换）。
"""

from __future__ import annotations

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveFollowEvent

_log = get_logger(__name__)


class FollowThanksPlugin(Plugin):
    """用户关注直播间时自动发送感谢消息（轮换）。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        self._phrase_index: int = 0

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        _log.info(
            "[FollowThanks] 就绪 (plugin_id={})  感谢语={}条",
            self.plugin_id,
            len(config.get_list("follow_phrases")),
        )

    @event_handler
    async def on_follow(self, event: LiveFollowEvent) -> None:
        cfg = self._config
        if cfg is None:
            return

        # 房间过滤
        enabled_rooms: list = cfg.get_int_list("enabled_rooms")
        if enabled_rooms and event.livestream.live_id not in enabled_rooms:
            return

        # 跳过机器人自身
        if event.user.id == event.livestream.bot.id:
            return

        # 按顺序轮换选取感谢语
        phrases: list[str] = cfg.get_list("follow_phrases")
        if not phrases:
            return

        phrase = phrases[self._phrase_index % len(phrases)]
        self._phrase_index += 1
        phrase = phrase.replace("{user}", event.user.name)
        await event.livestream.send_message(phrase)
