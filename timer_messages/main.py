"""定时消息插件。

注册多条定时消息（插件消息）到账户直播间队列,
由 Bot 的定时消息队列统一轮转发送（间隔由服务器 bot.timer_interval 控制）。

消息经 ``self.register_timer_message()`` 注册，属于**插件消息**：
不写入持久化文件、面板不可编辑/删除/移动、在轮转中置顶，
并由框架在插件停用/挂起/卸载/重载时自动清理——因此本插件不保存消息 ID。

注册时机是 ``on_livestream_bound``：账户绑定直播间后（或插件启用时账户已绑定）
框架会调用它，无需再靠弹幕事件兜底重试。

配置格式（_conf_schema.json → timer_messages_text）::

    欢迎来到直播间～
    每晚 8 点准时开播，不见不散 💕
"""

from __future__ import annotations

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig

_log = get_logger(__name__)


class TimerMessagesPlugin(Plugin):
    """定时消息插件——多条定时消息，注册到账户直播间队列。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config

    async def on_livestream_bound(self, livestream) -> None:
        """账户绑定直播间 → 注册全部定时消息。"""
        if self._config is None or not self._config.get_bool("enabled", True):
            return
        self._register_all()

    async def terminate(self) -> None:
        """即时撤销本插件的定时消息。

        框架在插件停用时也会强制清理一遍，这里主动撤销只是让效果立刻可见。
        """
        self.unregister_timer_messages()

    # ------------------------------------------------------------------ #
    # 注册逻辑
    # ------------------------------------------------------------------ #

    def _register_all(self) -> None:
        """解析配置并注册所有定时消息。"""
        cfg = self._config
        if cfg is None:
            return

        # 先清空本插件已有消息：重复触发钩子（如更换绑定直播间）时只保留一份
        self.unregister_timer_messages()

        messages = self._parse_messages(cfg.get_str("timer_messages_text", ""))
        if not messages:
            _log.info("[TimerMessages] 未配置任何定时消息")
            return

        count = 0
        for message in messages:
            if not self.register_timer_message(message):
                _log.info("[TimerMessages] 账户未绑定直播间，暂不注册")
                return
            count += 1
            _log.info("[TimerMessages] 已注册: msg={}", message.split("\n")[0][:30])

        _log.info("[TimerMessages] 注册完成: {} 条", count)

    @staticmethod
    def _parse_messages(text: str) -> list[str]:
        """解析多行文本为消息列表。

        规则：每非空行即一条定时消息（空行忽略）。
        """
        return [line.strip() for line in text.split("\n") if line.strip()]
