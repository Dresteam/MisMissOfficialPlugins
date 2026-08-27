"""提问感谢插件。

用户发起付费提问时自动发送感谢播报消息。
"""

from __future__ import annotations

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveQuestionEvent

_log = get_logger(__name__)


class QuestionThanksPlugin(Plugin):
    """用户提问时自动发送感谢播报消息。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        _log.info(
            "[QuestionThanks] 就绪 (plugin_id={})",
            self.plugin_id,
        )

    @event_handler
    async def on_question(self, event: LiveQuestionEvent) -> None:
        cfg = self._config
        if cfg is None:
            return

        # 房间过滤
        enabled_rooms: list = cfg.get_int_list("enabled_rooms")
        if enabled_rooms and event.livestream.live_id not in enabled_rooms:
            return

        question = event.question
        user_name = event.user.name

        lines: list[str] = []

        header = cfg.get_str("header_format", "🙋♂️{user}提问了哦").replace("{user}", user_name)
        lines.append(header)

        decor = cfg.get_str("decor_line", "✨🌛✨✨✨☀️✨✨✨✨")
        if decor:
            lines += ["", decor, ""]

        content = cfg.get_str("content_format", "提问内容：{text}").replace("{text}", question.text)
        lines.append(content)

        value = cfg.get_str("value_format", "提问价值：{price}钻💎").replace("{price}", str(question.price))
        lines.append(value)

        await event.livestream.send_message("\n".join(lines))
