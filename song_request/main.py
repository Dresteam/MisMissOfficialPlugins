"""点播插件。每个直播间的点播单独立管理。"""

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
    """用户点播系统 —— 按直播间隔离，Web 前端 + 定时消息。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        self._playlists: dict[int, list[_SongEntry]] = {}
        self._room_names: dict[int, str] = {}  # live_id → room_name for web display
        self._timer_ids: dict[int, list[str]] = {}  # live_id → [msg_ids]
        self._timer_registered: set[int] = set()
        self._bot: Bot | None = None
        self._standalone_server: asyncio.AbstractServer | None = None
        self._standalone_html_cache: bytes | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        self._load_playlist()
        total = sum(len(v) for v in self._playlists.values())
        _log.info(
            "[SongRequest] 就绪 (plugin_id={})  直播间={}个  点播={}条  timer={}  standalone={}",
            self.plugin_id, len(self._playlists), total,
            "on" if config.get_bool("timer_enabled", False) else "off",
            "on" if config.get_bool("standalone_enabled", False) else "off",
        )
        # 定时消息：启动时主动注册（bot 可用时），否则由首次消息事件兜底
        if config.get_bool("timer_enabled", False):
            self._register_timer()
        # 独立前端
        if config.get_bool("standalone_enabled", False):
            await self._start_standalone()

    async def terminate(self) -> None:
        if self._standalone_server:
            self._standalone_server.close()
            await self._standalone_server.wait_closed()
            self._standalone_server = None
            _log.info("[SongRequest] 独立前端已关闭")
        bot = self._resolve_bot()
        if bot is not None:
            for mids in self._timer_ids.values():
                for mid in mids:
                    bot.unregister_timer_message(mid)
        self._timer_ids.clear()
        self._timer_registered.clear()  # 重置标志，重新启用时可再次注册
        self._save_playlist()

    async def on_enable(self) -> None:
        """禁用后重新启用 → 重新注册定时消息。"""
        if self._config and self._config.get_bool("timer_enabled", False):
            self._timer_registered.clear()
            self._register_timer()

    # ------------------------------------------------------------------ #
    # 内部：按直播间获取点播单
    # ------------------------------------------------------------------ #

    def _pl(self, live_id: int) -> list[_SongEntry]:
        """获取指定直播间的点播单，不存在则创建。"""
        if live_id not in self._playlists:
            self._playlists[live_id] = []
        return self._playlists[live_id]

    # ------------------------------------------------------------------ #
    # 事件处理器
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_message(self, event: LiveMessageEvent) -> None:
        cfg = self._config
        if cfg is None:
            return

        live_id = event.livestream.live_id
        enabled_rooms: list = cfg.get_int_list("enabled_rooms")
        if enabled_rooms and live_id not in enabled_rooms:
            return

        # 记录房间名
        if live_id not in self._room_names:
            self._room_names[live_id] = event.livestream.room_name or f"房间{live_id}"

        if self._bot is None:
            self._bot = event.livestream.bot

        if live_id not in self._timer_registered and cfg.get_bool("timer_enabled", False):
            self._register_timer(event)

        text = event.message.strip()
        if not text:
            return

        cmd_add = cfg.get_str("cmd_add", "点播")
        cmd_list = cfg.get_str("cmd_list", "点播单")
        if self._match(text, cmd_list, cfg.get_list("cmd_list_aliases")):
            await self._cmd_list(event, text)
        elif self._match(text, cmd_add, cfg.get_list("cmd_add_aliases")):
            await self._cmd_add(event, text)
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
                    self._pl(event.livestream.live_id).append(_SongEntry(song_name=name, user_name=event.user.name))
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
        items = list(self._pl(event.livestream.live_id))
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
        return self._pl(event.livestream.live_id)

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

    def _register_timer(self, event: LiveMessageEvent | None = None) -> None:
        """注册定时消息——使用标准 Bot 接口，成功后才标记已注册。

        :param event: 消息事件；为 ``None`` 时对所有已知直播间批量注册
        """
        cfg = self._config
        if cfg is None:
            return

        if event is not None:
            live_ids = [event.livestream.live_id]
        else:
            # 主动注册：服务器已连接的直播间 + 持久化中有数据的直播间
            live_ids = list(self._playlists.keys())
            srv = getattr(self, '_server', None)
            if srv is not None:
                for rid in srv.livestreams:
                    if rid not in live_ids:
                        live_ids.append(rid)
            if not live_ids:
                return

        bot = self._resolve_bot()
        if bot is None:
            return  # bot 不可用，下次事件重试

        msg = cfg.get_str("timer_message", "").replace("{cmd_add}", cfg.get_str("cmd_add", "点播"))
        for live_id in live_ids:
            if live_id in self._timer_registered:
                continue
            try:
                mid = bot.register_timer_message(live_id, msg)
                self._timer_registered.add(live_id)  # 成功后才标记
                self._timer_ids.setdefault(live_id, []).append(mid)
                _log.info("[SongRequest] 定时消息已注册: live={}", live_id)
            except Exception as e:
                _log.warning("[SongRequest] 定时消息注册失败 live={}: {}", live_id, e)

    # ------------------------------------------------------------------ #
    # 原生 Web API
    # ------------------------------------------------------------------ #

    def register_routes(self, router: Any) -> None:
        from fastapi import Body, Query
        from fastapi.responses import JSONResponse

        def _pl_web(room_id: int) -> list[_SongEntry]:
            return self._pl(room_id)

        @router.get("/rooms")
        async def get_rooms():
            result: dict[int, dict] = {}
            # 1) 服务器已连接的直播间（优先，含真实名称）
            srv = getattr(self, '_server', None)
            if srv is not None:
                for rid, live in srv.livestreams.items():
                    if rid > 0:
                        result[rid] = {"room_id": rid, "room_name": live.room_name or f"房间{rid}", "count": len(self._pl(rid))}
            # 2) 事件中已见过的房间（补充）
            for rid, name in self._room_names.items():
                if rid > 0 and rid not in result:
                    result[rid] = {"room_id": rid, "room_name": name, "count": len(self._pl(rid))}
            # 3) 持久化中有数据的房间（补充）
            for rid in self._playlists:
                if rid > 0 and rid not in result:
                    result[rid] = {"room_id": rid, "room_name": f"房间{rid}", "count": len(self._pl(rid))}
            return JSONResponse(list(result.values()))

        @router.get("/playlist")
        async def get_playlist(room_id: int = Query(0)):
            return JSONResponse([
                {"index": i + 1, "song_name": e.song_name, "user_name": e.user_name, "status": e.status}
                for i, e in enumerate(_pl_web(room_id))
            ])

        @router.post("/add")
        async def add_song(body: dict = Body(...), room_id: int = Query(0)):
            name = str(body.get("song_name", "")).strip()
            if name:
                _pl_web(room_id).append(_SongEntry(song_name=name, user_name="web"))
                self._save_playlist()
                cfg = self._config
                add_fmt = cfg.get_str("msg_web_add_success", "✅ [Web] 已收录点播：『{song}』") if cfg else "✅ [Web] 已收录点播：『{song}』"
                self._notify_live(add_fmt.replace("{song}", name), room_id)
            return JSONResponse({"ok": True})

        @router.post("/delete")
        async def delete_song(body: dict = Body(...), room_id: int = Query(0)):
            idx = int(body.get("index", -1))
            pl = _pl_web(room_id)
            if 0 <= idx < len(pl):
                e = pl.pop(idx)
                self._save_playlist()
                cfg = self._config
                del_fmt = cfg.get_str("msg_web_delete_success", "🗑️ [Web] 点播 #{n}『{song}』已删除") if cfg else "🗑️ [Web] 点播 #{n}『{song}』已删除"
                self._notify_live(del_fmt.replace("{n}", str(idx + 1)).replace("{song}", e.song_name), room_id)
            return JSONResponse({"ok": True})

        @router.post("/status")
        async def set_status(body: dict = Body(...), room_id: int = Query(0)):
            idx = int(body.get("index", -1))
            st = str(body.get("status", ""))
            pl = _pl_web(room_id)
            if 0 <= idx < len(pl) and st:
                e = pl[idx]
                if e.status == st:
                    return JSONResponse({"ok": True, "skipped": True})  # 同状态不重复输出
                e.status = st
                self._save_playlist()
                msg = self._status_msg(e.song_name, e.user_name, st)
                if msg:
                    self._notify_live(msg, room_id)
            return JSONResponse({"ok": True})

        @router.post("/clear")
        async def clear_playlist(room_id: int = Query(0)):
            """清空指定直播间的点播单。"""
            pl = _pl_web(room_id)
            count = len(pl)
            pl.clear()
            self._save_playlist()
            _log.info("[SongRequest] Web 清空点播单: room_id={}, {} 条", room_id, count)
            self._notify_live(f"🗑️ [Web] 点播单已清空（{count} 条）", room_id)
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

    def _notify_live(self, message: str, live_id: int | None = None) -> None:
        bot = self._resolve_bot()
        if bot is None:
            return
        lid = live_id or next(iter(self._playlists), 0)
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
        if isinstance(data, dict) and "playlists" in data:
            for rid_str, items in data["playlists"].items():
                rid = int(rid_str)
                self._playlists[rid] = [
                    _SongEntry(song_name=str(d.get("song_name", "")), user_name=str(d.get("user_name", "")), status=str(d.get("status", "pending")))
                    for d in items if isinstance(d, dict)
                ]
            rooms_data = data.get("rooms", {})
            if isinstance(rooms_data, dict):
                for rid_str, name in rooms_data.items():
                    self._room_names[int(rid_str)] = str(name)
        else:
            self._playlists = {}

    def _save_playlist(self) -> None:
        if self.data is None:
            return
        try:
            self.data.write_json(_PLAYLIST_FILE, {
                "rooms": {str(rid): name for rid, name in self._room_names.items()},
                "playlists": {str(rid): [
                    {"song_name": s.song_name, "user_name": s.user_name, "status": s.status}
                    for s in pl
                ] for rid, pl in self._playlists.items()},
            })
        except OSError as e:
            _log.warning("[SongRequest] 保存失败: {}", e)

    # ------------------------------------------------------------------ #
    # 独立前端（无需登录，每直播间独立面板）
    # ------------------------------------------------------------------ #

    async def _start_standalone(self) -> None:
        """启动独立前端 HTTP 服务器。"""
        cfg = self._config
        if cfg is None:
            return
        port = cfg.get_int("standalone_port", 18888)
        host = cfg.get_str("standalone_host", "0.0.0.0")
        try:
            self._standalone_server = await asyncio.start_server(
                self._handle_standalone, host, port
            )
            _log.info("[SongRequest] 独立前端已启动: http://{}:{}", host, port)
        except OSError as e:
            _log.warning("[SongRequest] 独立前端启动失败: {} (端口 {} 被占用?)", e, port)

    def _load_standalone_html(self) -> bytes:
        """加载独立前端 HTML 模板（带缓存）。"""
        if self._standalone_html_cache is None:
            html_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "standalone.html"
            )
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    self._standalone_html_cache = f.read().encode("utf-8")
            except OSError as e:
                _log.warning("[SongRequest] 加载 standalone.html 失败: {}", e)
                self._standalone_html_cache = b"<!DOCTYPE html><html><body>standalone.html missing</body></html>"
        return self._standalone_html_cache

    async def _handle_standalone(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """独立前端 HTTP 请求处理（无认证）。"""
        try:
            request_line = ""
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if not line or line in (b"\r\n", b"\n"):
                    break
                decoded = line.decode("utf-8", errors="replace")
                if not request_line:
                    request_line = decoded
                elif ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            if not request_line:
                return

            parts = request_line.strip().split(" ")
            method = parts[0] if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"

            body_raw = b""
            cl = int(headers.get("content-length", 0))
            if cl > 0:
                body_raw = await asyncio.wait_for(reader.readexactly(cl), timeout=5)

            status, ctype, rbody = self._route_standalone(method, path, body_raw)
            response = (
                f"HTTP/1.1 {status} OK\r\nContent-Type: {ctype}\r\n"
                f"Access-Control-Allow-Origin: *\r\nConnection: close\r\n"
                f"Content-Length: {len(rbody)}\r\n\r\n"
            ).encode() + rbody
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _route_standalone(
        self, method: str, path: str, body_raw: bytes
    ) -> tuple[int, str, bytes]:
        """路由独立前端请求。"""
        import json as _json
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(path)
        route = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        def room_id() -> int:
            try:
                return int(query.get("room_id", "0"))
            except ValueError:
                return 0

        # 静态页面：/（带房间选择器）或 /{room_id}（绑定指定直播间）
        if method == "GET" and (route == "/" or route.startswith("/room/") or re.fullmatch(r"/\d+", route)):
            return 200, "text/html; charset=utf-8", self._load_standalone_html()

        # API: /api/rooms
        if method == "GET" and route == "/api/rooms":
            rooms = []
            srv = getattr(self, '_server', None)
            if srv is not None:
                for rid, live in srv.livestreams.items():
                    if rid > 0:
                        rooms.append({"room_id": rid, "room_name": live.room_name or f"房间{rid}", "count": len(self._pl(rid))})
            for rid, name in self._room_names.items():
                if rid > 0 and not any(r["room_id"] == rid for r in rooms):
                    rooms.append({"room_id": rid, "room_name": name, "count": len(self._pl(rid))})
            for rid in self._playlists:
                if rid > 0 and not any(r["room_id"] == rid for r in rooms):
                    rooms.append({"room_id": rid, "room_name": f"房间{rid}", "count": len(self._pl(rid))})
            return 200, "application/json; charset=utf-8", _json.dumps(rooms, ensure_ascii=False).encode("utf-8")

        # API: /api/playlist?room_id=X
        if method == "GET" and route == "/api/playlist":
            rid = room_id()
            data = [
                {"index": i + 1, "song_name": e.song_name, "user_name": e.user_name, "status": e.status}
                for i, e in enumerate(self._pl(rid))
            ]
            return 200, "application/json; charset=utf-8", _json.dumps(data, ensure_ascii=False).encode("utf-8")

        if method == "POST":
            try:
                req = _json.loads(body_raw) if body_raw else {}
            except _json.JSONDecodeError:
                req = {}
            rid = room_id()

            if route == "/api/add":
                name = str(req.get("song_name", "")).strip()
                if name and rid > 0:
                    self._pl(rid).append(_SongEntry(song_name=name, user_name="web"))
                    self._save_playlist()
                    self._notify_live(f"✅ [Web] 已收录点播：『{name}』", rid)
                return 200, "application/json", b'{"ok":true}'

            if route == "/api/delete":
                idx = int(req.get("index", -1))
                pl = self._pl(rid)
                if 0 <= idx < len(pl):
                    e = pl.pop(idx)
                    self._save_playlist()
                    self._notify_live(f"🗑️ [Web] 点播 #{idx + 1}『{e.song_name}』已删除", rid)
                return 200, "application/json", b'{"ok":true}'

            if route == "/api/status":
                idx = int(req.get("index", -1))
                st = str(req.get("status", ""))
                pl = self._pl(rid)
                if 0 <= idx < len(pl) and st:
                    e = pl[idx]
                    if e.status != st:
                        e.status = st
                        self._save_playlist()
                        msg = self._status_msg(e.song_name, e.user_name, st)
                        if msg:
                            self._notify_live(msg, rid)
                return 200, "application/json", b'{"ok":true}'

        return 404, "text/plain; charset=utf-8", b"Not Found"
