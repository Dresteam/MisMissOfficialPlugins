"""歌单插件。

发送「歌单」指令（可配置），输出配置好的歌单列表，带装饰边框。

消息格式示例::

    ★--☁✨☁ 本场歌单☁✨☁--★
    ❥•1 晴天 - 周杰伦
    ❥•2 夜曲 - 周杰伦
    ❥•3 小幸运 - 田馥甄
    ══════ ᶫᵒᵛᵉᵧₒᵤ ══════
"""

from __future__ import annotations

import asyncio

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.command import command, Scope

_log = get_logger(__name__)


class SongListPlugin(Plugin):
    """发送歌单指令时输出配置好的歌单。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        self._room_id: int = 0

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        # 多账户模型：账户仅绑定一个直播间，从 server 获取
        srv = getattr(self, '_server', None)
        if srv is not None:
            try:
                self._room_id = next(iter(srv.livestreams.keys()), 0)
            except Exception:
                self._room_id = 0

        songs = config.get_list("song_list")
        _log.info(
            "[SongList] 就绪 (plugin_id={})  房间={}  歌曲={}首",
            self.plugin_id, self._room_id or "?", len(songs),
        )

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #

    @command("歌单", alias=["歌曲列表", "点歌单"], scope=Scope.LIVEMESSAGE)
    def cmd_song_list(self) -> None:
        """歌单指令——输出配置好的歌单。"""
        if self._config is None or self._room_id <= 0:
            return
        # @command 方法为同步调用，通过事件循环调度异步发送
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._send_song_list())

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _send_song_list(self) -> None:
        """构建并发送歌单消息。"""
        cfg = self._config
        if cfg is None:
            return

        songs: list[str] = cfg.get_list("song_list")
        header = cfg.get_str("header", "★--☁✨☁ 本场歌单☁✨☁--★")
        prefix_fmt = cfg.get_str("item_prefix", "❥•{idx} ")
        footer = cfg.get_str("footer", "══════ ᶫᵒᵛᵉᵧₒᵤ ══════")

        lines: list[str] = [header]
        for i, song in enumerate(songs, start=1):
            lines.append(prefix_fmt.replace("{idx}", str(i)) + song)
        lines.append(footer)

        message = "\n".join(lines)

        srv = getattr(self, '_server', None)
        if srv is None:
            return
        try:
            await srv.bot.send_livestream_message(self._room_id, message)
        except Exception as e:
            _log.warning("[SongList] 发送歌单失败: {}", e)
