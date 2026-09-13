"""点播插件。账户级单点播单（多账户由插件实例隔离）。"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any

from core.logging import get_logger
from interfaces.bot.bot import Bot
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent

_log = get_logger(__name__)

_PLAYLIST_FILE = "playlist.json"


@dataclass
class _SongEntry:
    song_name: str
    user_name: str
    status: str = "pending"


class SongRequestPlugin(Plugin):
    """用户点播系统 —— 账户级单点播单，Web 前端 + 定时消息。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        self._playlist: list[_SongEntry] = []
        self._bot: Bot | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        self._load_playlist()
        _log.info(
            "[SongRequest] 就绪 (plugin_id={})  点播={}条  timer={}",
            self.plugin_id, len(self._playlist),
            "on" if config.get_bool("timer_enabled", False) else "off",
        )
    async def on_livestream_bound(self, livestream) -> None:
        """账户绑定直播间 → 注册定时消息（启用且配置打开时）。"""
        if self._config and self._config.get_bool("timer_enabled", False):
            self._register_timer()

    async def terminate(self) -> None:
        # 即时撤销本插件的定时消息（框架停用时也会强制清理一遍）
        self.unregister_timer_messages()
        self._save_playlist()

    # ------------------------------------------------------------------ #
    # 内部：账户点播单
    # ------------------------------------------------------------------ #

    def _account_room_id(self) -> int:
        """账户直播间 ID（多账户版本下账户仅绑定一个直播间）。"""
        srv = getattr(self, "_server", None)
        lives = getattr(srv, "livestreams", None) or {}
        return next(iter(lives.keys()), 0)

    # ------------------------------------------------------------------ #
    # 事件处理器
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_message(self, event: LiveMessageEvent) -> None:
        cfg = self._config
        if cfg is None:
            return

        if self._bot is None:
            self._bot = event.livestream.bot

        text = event.message.strip()
        if not text:
            return

        cmd_add = cfg.get_str("cmd_add", "点播")
        cmd_list = cfg.get_str("cmd_list", "点播单")
        if self._match(text, cmd_list, cfg.get_list("cmd_list_aliases")):
            await self._cmd_list(event, text)
        elif self._match(text, cmd_add, cfg.get_list("cmd_add_aliases")):
            await self._cmd_add(event, text)
        elif text == cfg.get_str("cmd_clear", "清空") and event.user.is_admin:
            await self._cmd_clear(event)
        else:
            for st, cmd_key in [("done", "cmd_complete"), ("playing", "cmd_playing"), ("working", "cmd_working")]:
                cmd = cfg.get_str(cmd_key, "")
                if cmd and text.startswith(cmd + " ") and event.user.is_admin:
                    await self._cmd_set_status(event, text, st, cmd)
                    return
            cmd_del = cfg.get_str("cmd_delete", "删除")
            if text.startswith(cmd_del + " ") and event.user.is_admin:
                await self._cmd_delete(event, text, cmd_del)

    # ------------------------------------------------------------------ #
    # 命令
    # ------------------------------------------------------------------ #

    async def _cmd_add(self, event: LiveMessageEvent, text: str) -> None:
        cfg = self._config
        if cfg is None:
            return
        cmd = cfg.get_str("cmd_add", "点播")
        for prefix in [cmd] + cfg.get_list("cmd_add_aliases"):
            if text.startswith(prefix + " "):
                name = text.removeprefix(prefix).strip()
                if name:
                    self._playlist.append(_SongEntry(song_name=name, user_name=event.user.name))
                    self._save_playlist()
                    add_msg = cfg.get_str("msg_add_success", "✅ 已收录点播：『{song}』  @{user}")
                    await event.livestream.send_message(add_msg.replace("{song}", name).replace("{user}", event.user.name))
                    await self._show_page(event, "dummy", last=True)
                return

    async def _cmd_list(self, event: LiveMessageEvent, text: str) -> None:
        await self._show_page(event, text, last=False)

    async def _show_page(self, event: LiveMessageEvent, text: str, *, last: bool) -> None:
        cfg = self._config
        if cfg is None:
            return
        items = list(self._playlist)
        ps = cfg.get_int("page_size", 8)
        tp = max(1, (len(items) + ps - 1) // ps)
        if last:
            p = tp
        else:
            cmd_list = cfg.get_str("cmd_list", "点播单")
            a = text
            for pf in [cmd_list] + cfg.get_list("cmd_list_aliases"):
                if a.startswith(pf):
                    a = a.removeprefix(pf).strip()
                    break
            try:
                p = max(1, min(int(a) if a else 1, tp))
            except ValueError:
                p = 1
        s = (p - 1) * ps
        chunk = items[s:s + ps]
        header = cfg.get_str("playlist_header", "★--☁✨☁ 本场点播单☁✨☁--★")
        pfx = cfg.get_str("playlist_item_prefix", "❥•")
        icons = cfg.get("status_icons", {}) or {"pending":" ⏳","playing":" 🎵","working":" 🔧","done":" ✅"}
        page_fmt = cfg.get_str("playlist_page_format", "❀ ✣({page}/{total}页)")
        item_fmt = cfg.get_str("playlist_item_format", "{prefix}{idx} 『{song}』 @{user}{icon}")
        empty_fmt = cfg.get_str("playlist_empty_slot", "{prefix}{idx}")
        footer = cfg.get_str("playlist_footer", "══════ ᶫᵒᵛᵉᵧₒᵤ ══════")

        lines = [header, page_fmt.replace("{page}", str(p)).replace("{total}", str(tp))]
        for i in range(ps):
            idx = s + i + 1
            if i < len(chunk):
                e = chunk[i]
                icon = icons.get(e.status, " ⏳")
                lines.append(item_fmt.replace("{prefix}", pfx).replace("{idx}", str(idx)).replace("{song}", e.song_name).replace("{user}", e.user_name).replace("{icon}", icon))
            else:
                lines.append(empty_fmt.replace("{prefix}", pfx).replace("{idx}", str(idx)))
        lines.append(footer)
        await event.livestream.send_message("\n".join(lines))

    def _pl_cmd(self, event: LiveMessageEvent) -> list[_SongEntry]:
        return self._playlist

    async def _cmd_set_status(self, event: LiveMessageEvent, text: str, status: str, cmd: str) -> None:
        cfg = self._config
        if cfg is None:
            return
        pl = self._pl_cmd(event)
        n = self._parse_n(text.removeprefix(cmd).strip())
        if n is None or n < 1 or n > len(pl):
            invalid_msg = cfg.get_str("msg_invalid_index", "❌ 无效序号（共 {count} 项）")
            await event.livestream.send_message(invalid_msg.replace("{count}", str(len(pl))))
            return
        e = pl[n - 1]
        if e.status == status:
            return  # 同状态不重复输出
        e.status = status
        self._save_playlist()
        msg = self._status_msg(e.song_name, e.user_name, status)
        if msg:
            await event.livestream.send_message(msg)

    async def _cmd_clear(self, event: LiveMessageEvent) -> None:
        """清空点播单（管理员，精确匹配指令名）。"""
        cfg = self._config
        if cfg is None:
            return
        count = len(self._playlist)
        if count == 0:
            empty_msg = cfg.get_str("msg_clear_empty", "📭 点播单已经是空的啦~")
            await event.livestream.send_message(f"@{event.user.name} {empty_msg}")
            return
        self._playlist.clear()
        self._save_playlist()
        clear_msg = cfg.get_str("msg_clear_success", "🗑️ 点播单已清空（{count} 条）")
        await event.livestream.send_message(clear_msg.replace("{count}", str(count)))

    async def _cmd_delete(self, event: LiveMessageEvent, text: str, cmd: str) -> None:
        cfg = self._config
        if cfg is None:
            return
        pl = self._pl_cmd(event)
        n = self._parse_n(text.removeprefix(cmd).strip())
        if n is None or n < 1 or n > len(pl):
            invalid_msg = cfg.get_str("msg_invalid_index", "❌ 无效序号（共 {count} 项）")
            await event.livestream.send_message(invalid_msg.replace("{count}", str(len(pl))))
            return
        e = pl.pop(n - 1)
        self._save_playlist()
        del_msg = cfg.get_str("msg_delete_success", "🗑️ #{n}『{song}』已删除")
        await event.livestream.send_message(del_msg.replace("{n}", str(n)).replace("{song}", e.song_name))

    # ------------------------------------------------------------------ #
    # 定时消息
    # ------------------------------------------------------------------ #

    def _register_timer(self) -> None:
        """注册定时消息（插件消息）到账户直播间队列。"""
        cfg = self._config
        if cfg is None:
            return
        msg = cfg.get_str("timer_message", "").replace(
            "{cmd_add}", cfg.get_str("cmd_add", "点播")
        )
        if not msg:
            return
        # 先清空本插件已有消息：重复触发钩子（如更换绑定直播间）时只保留一份
        self.unregister_timer_messages()
        if self.register_timer_message(msg):
            _log.info("[SongRequest] 定时消息已注册")
        else:
            _log.info("[SongRequest] 账户未绑定直播间，暂不注册")

    # ------------------------------------------------------------------ #
    # 原生 Web API
    # ------------------------------------------------------------------ #

    def register_routes(self, router: Any) -> None:
        from fastapi import Body
        from fastapi.responses import JSONResponse

        @router.get("/rooms")
        async def get_rooms():
            rid = self._account_room_id()
            if rid <= 0:
                return JSONResponse([])
            srv = getattr(self, '_server', None)
            live = srv.livestreams.get(rid) if srv is not None else None
            room_name = (live.room_name if live is not None and live.room_name else f"房间{rid}")
            return JSONResponse([{"room_id": rid, "room_name": room_name, "count": len(self._playlist)}])

        @router.get("/playlist")
        async def get_playlist():
            return JSONResponse([
                {"index": i + 1, "song_name": e.song_name, "user_name": e.user_name, "status": e.status}
                for i, e in enumerate(self._playlist)
            ])

        @router.post("/add")
        async def add_song(body: dict = Body(...)):
            name = str(body.get("song_name", "")).strip()
            if name:
                self._playlist.append(_SongEntry(song_name=name, user_name="web"))
                self._save_playlist()
                cfg = self._config
                add_fmt = cfg.get_str("msg_web_add_success", "✅ [Web] 已收录点播：『{song}』") if cfg else "✅ [Web] 已收录点播：『{song}』"
                self._notify_live(add_fmt.replace("{song}", name))
            return JSONResponse({"ok": True})

        @router.post("/delete")
        async def delete_song(body: dict = Body(...)):
            idx = int(body.get("index", -1))
            pl = self._playlist
            if 0 <= idx < len(pl):
                e = pl.pop(idx)
                self._save_playlist()
                cfg = self._config
                del_fmt = cfg.get_str("msg_web_delete_success", "🗑️ [Web] 点播 #{n}『{song}』已删除") if cfg else "🗑️ [Web] 点播 #{n}『{song}』已删除"
                self._notify_live(del_fmt.replace("{n}", str(idx + 1)).replace("{song}", e.song_name))
            return JSONResponse({"ok": True})

        @router.post("/status")
        async def set_status(body: dict = Body(...)):
            idx = int(body.get("index", -1))
            st = str(body.get("status", ""))
            pl = self._playlist
            if 0 <= idx < len(pl) and st:
                e = pl[idx]
                if e.status == st:
                    return JSONResponse({"ok": True, "skipped": True})  # 同状态不重复输出
                e.status = st
                self._save_playlist()
                msg = self._status_msg(e.song_name, e.user_name, st)
                if msg:
                    self._notify_live(msg)
            return JSONResponse({"ok": True})

        @router.post("/clear")
        async def clear_playlist():
            """清空账户点播单。"""
            pl = self._playlist
            count = len(pl)
            pl.clear()
            self._save_playlist()
            _log.info("[SongRequest] Web 清空点播单: {} 条", count)
            self._notify_live(f"🗑️ [Web] 点播单已清空（{count} 条）")
            return JSONResponse({"ok": True, "cleared": count})

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    def _resolve_bot(self) -> Bot | None:
        """获取 bot 引用：优先已捕获的实例，其次从 server 获取。"""
        if self._bot is not None:
            return self._bot
        srv = getattr(self, '_server', None)
        if srv is not None and srv.bot_available:
            return srv.bot
        return None

    def _notify_live(self, message: str) -> None:
        bot = self._resolve_bot()
        if bot is None:
            return
        lid = self._account_room_id()
        if lid:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(bot.send_livestream_message(lid, message))
            except RuntimeError:
                pass

    @staticmethod
    def _match(text: str, cmd: str, aliases: list[str]) -> bool:
        pfx = [cmd + " ", cmd] + [a + " " for a in aliases] + aliases
        return any(text.startswith(p) for p in pfx)

    def _status_msg(self, song_name: str, user_name: str, status: str) -> str:
        """生成带音乐播放器装饰的状态变更消息（格式从 config 读取）。"""
        cfg = self._config
        border = cfg.get_str("msg_player_border", "•────⋆⁺₊⋆☾⋆⁺₊⋆────•") if cfg else "•────⋆⁺₊⋆☾⋆⁺₊⋆────•"
        progress = cfg.get_str("msg_player_progress", " ●━━━━━────── 5:20") if cfg else " ●━━━━━────── 5:20"
        controls = cfg.get_str("msg_player_controls", "⇆        ᐊ    ■    ᐅ        ♥︎") if cfg else "⇆        ᐊ    ■    ᐅ        ♥︎"

        if status == "pending":
            return ""  # pending 状态不输出通知
        elif status == "playing" and cfg:
            emoji = cfg.get_str("msg_playing_emoji", "·  .⋆ 🎵✨ 正在播放 ✨🎵·  .⋆")
        elif status == "working" and cfg:
            emoji = cfg.get_str("msg_working_emoji", "·  .⋆ 🪐✨ 正在操作 ✨🪐·  .⋆")
        elif status == "done" and cfg:
            done_fmt = cfg.get_str("msg_done_text", "💗主播大大已经完成🎤『{song}』了喔")
            return done_fmt.replace("{song}", song_name)
        else:
            return f"✅ 『{song_name}』"

        return (
            f"{border}\n"
            f"{emoji}\n\n"
            f"        《{song_name}》\n\n"
            f"    @{user_name}\n\n"
            f"{progress}\n"
            f"{controls}\n"
            f"{border}"
        )

    @staticmethod
    def _parse_n(arg: str) -> int | None:
        m = re.search(r"\d+", arg)
        return int(m.group()) if m else None

    def _playlist_path(self) -> str:
        """[deprecated] 保留用于向后兼容，新代码应直接使用 self.data。"""
        return os.path.join(self.data_dir, _PLAYLIST_FILE)

    def _load_playlist(self) -> None:
        data = self.data.read_json(_PLAYLIST_FILE) if self.data else None
        self._playlist = []
        if isinstance(data, dict):
            items = data.get("playlist")
            if isinstance(items, list):
                # 新格式：{"playlist": [...]}
                self._playlist = [
                    _SongEntry(song_name=str(d.get("song_name", "")), user_name=str(d.get("user_name", "")), status=str(d.get("status", "pending")))
                    for d in items if isinstance(d, dict)
                ]
            elif isinstance(items, dict):
                # 旧房间分区格式（多账户版本不再使用，合并全部条目）
                for _rid_str, lst in items.items():
                    if isinstance(lst, list):
                        self._playlist.extend(
                            _SongEntry(song_name=str(d.get("song_name", "")), user_name=str(d.get("user_name", "")), status=str(d.get("status", "pending")))
                            for d in lst if isinstance(d, dict)
                        )

    def _save_playlist(self) -> None:
        if self.data is None:
            return
        try:
            self.data.write_json(_PLAYLIST_FILE, {
                "playlist": [
                    {"song_name": s.song_name, "user_name": s.user_name, "status": s.status}
                    for s in self._playlist
                ],
            })
        except OSError as e:
            _log.warning("[SongRequest] 保存失败: {}", e)

    # ------------------------------------------------------------------ #
