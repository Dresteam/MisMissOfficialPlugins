"""欢迎插件。

新用户进入直播间时自动发送随机欢迎消息。
支持拼音模式——在用户名上方附加拼音注音（基于 pypinyin 库）。
支持首次到访专属欢迎语，以及按直播间过滤。

消息格式（拼音模式开启时）::
    欢迎 @睡觉为大 来到直播间～

    ✐[shuì jué wéi dà]
"""

from __future__ import annotations

import os
import random
import time

from pypinyin import pinyin, Style

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveJoinEvent

_log = get_logger(__name__)

# 已访问用户持久化文件名
_SEEN_USERS_FILE = "seen_users.json"


def to_pinyin(text: str) -> str:
    """将中文字符串转为空格分隔的拼音（带声调）。

    :param text: 中文字符串
    :return: 拼音字符串，如 ``"shuì jué wéi dà"``
    """
    # 逐字转拼音（用户名多为非词典词组合，逐字更准确）
    result: list[str] = []
    for ch in text:
        if "一" <= ch <= "鿿":
            py = pinyin(ch, style=Style.TONE, heteronym=False)
            result.append(py[0][0] if py else ch)
        else:
            result.append(ch)
    return " ".join(result)


class WelcomePlugin(Plugin):
    """新用户进入直播间时自动发送随机欢迎消息，支持拼音模式。

    功能：
    - 普通用户：从 ``welcome_phrases`` 中随机选取欢迎语
    - 首次到访：从 ``first_visit_phrases`` 中随机选取（为空则退回普通欢迎语）
    - 直播间过滤：``enabled_rooms`` 限制启用的直播间（空 = 全部启用）
    - 拼音模式：可选的用户名拼音注音
    """

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        self._seen_users: set[int] = set()
        self._last_anonymous_welcome: float = 0.0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        self._last_anonymous_welcome = time.time()

        phrases = config.get_list("welcome_phrases")
        first_phrases = config.get_list("first_visit_phrases")
        pinyin_on = config.get_bool("pinyin_enabled", True)
        enabled = config.get_int_list("enabled_rooms")

        # 加载已访问用户记录
        self._load_seen_users()

        _log.info(
            "[WelcomePlugin] 就绪 (plugin_id={})  欢迎语={}条  首访语={}条  "
            "拼音={}  已见用户={}人  限定房间={}",
            self.plugin_id,
            len(phrases),
            len(first_phrases),
            "开启" if pinyin_on else "关闭",
            len(self._seen_users),
            enabled if enabled else "(全部)",
        )

    async def terminate(self) -> None:
        """插件终止 —— 持久化已访问用户记录。"""
        self._save_seen_users()
        _log.info(
            "[WelcomePlugin] 已终止，已见用户 {} 人已保存", len(self._seen_users)
        )

    # ------------------------------------------------------------------ #
    # 事件处理器
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_join(self, event: LiveJoinEvent) -> None:
        """用户进入直播间 → 按条件发送欢迎消息。"""
        cfg = self._config
        if cfg is None:
            return

        # 1. 检查直播间是否在启用列表中
        live_id = event.livestream.live_id
        enabled_rooms: list = cfg.get_int_list("enabled_rooms")
        if enabled_rooms and live_id not in enabled_rooms:
            return

        user_id = event.user.id

        # 2. 跳过机器人自身
        bot = event.livestream.bot
        if user_id == bot.id:
            return

        # 3. 匿名用户（id == 0）单独处理 —— 不追踪、不计入已见，有冷却
        if user_id == 0:
            # 冷却检查
            cooldown = cfg.get_float("anonymous_cooldown", 30.0)
            now = time.time()
            if now - self._last_anonymous_welcome < cooldown:
                return

            anonymous_phrases: list[str] = cfg.get_list("anonymous_phrases")
            if anonymous_phrases:
                phrase = random.choice(anonymous_phrases)
            else:
                first_phrases = cfg.get_list("first_visit_phrases")
                if first_phrases:
                    phrase = random.choice(first_phrases)
                else:
                    welcome = cfg.get_list("welcome_phrases")
                    if not welcome:
                        return
                    phrase = random.choice(welcome)
            phrase = phrase.replace("{user}", event.user.name)
            await self._send_welcome(event, phrase)
            self._last_anonymous_welcome = time.time()
            return

        # 4. 判断是否首次到访 → 选择欢迎语列表
        is_first_visit = user_id not in self._seen_users

        if is_first_visit:
            first_phrases: list[str] = cfg.get_list("first_visit_phrases")
            if first_phrases:
                phrases = first_phrases
            else:
                phrases = cfg.get_list("welcome_phrases")
        else:
            phrases = cfg.get_list("welcome_phrases")

        if not phrases:
            return

        # 5. 记录该用户并立即持久化（防止异常停止丢失数据）
        if is_first_visit:
            self._seen_users.add(user_id)
            self._save_seen_users()

        # 6. 构建并发送欢迎消息
        user_name = event.user.name
        phrase = random.choice(phrases).replace("{user}", user_name)
        await self._send_welcome(event, phrase)

    # ------------------------------------------------------------------ #
    # 内部：发送欢迎消息
    # ------------------------------------------------------------------ #

    async def _send_welcome(self, event: LiveJoinEvent, phrase: str) -> None:
        """拼接拼音前缀并发送欢迎消息。

        :param event: 加入事件
        :param phrase: 已替换 {user} 占位符的欢迎语
        """
        cfg = self._config
        if cfg is None:
            return

        pinyin_enabled = cfg.get_bool("pinyin_enabled", True)
        # 匿名用户（id == 0）不输出拼音（无用户名可供注音）
        if pinyin_enabled and event.user.id != 0:
            py = to_pinyin(event.user.name)
            prefix = cfg.get_str("pinyin_prefix", "✐[拼音] ")
            message = f"{phrase}\n\n{prefix}[{py}]"
        else:
            message = phrase

        await event.livestream.send_message(message)

    # ------------------------------------------------------------------ #
    # 持久化：已访问用户
    # ------------------------------------------------------------------ #

    def _seen_users_path(self) -> str:
        """[deprecated] 保留用于向后兼容，新代码应直接使用 self.data。"""
        return os.path.join(self.data_dir, _SEEN_USERS_FILE)

    def _load_seen_users(self) -> None:
        """从数据目录加载已访问用户 ID 集合。"""
        data = self.data.read_json(_SEEN_USERS_FILE) if self.data else None
        if isinstance(data, list):
            self._seen_users = set(data)
            _log.debug("[WelcomePlugin] 已加载 {} 个已见用户", len(self._seen_users))
        else:
            self._seen_users = set()
            _log.debug("[WelcomePlugin] 无已见用户记录，从头开始")

    def _save_seen_users(self) -> None:
        """将已访问用户 ID 集合持久化到数据目录。"""
        if self.data is None:
            return
        try:
            self.data.write_json(_SEEN_USERS_FILE, list(self._seen_users))
            _log.debug("[WelcomePlugin] 已保存 {} 个已见用户", len(self._seen_users))
        except OSError as e:
            _log.warning("[WelcomePlugin] 保存已见用户失败: {}", e)
