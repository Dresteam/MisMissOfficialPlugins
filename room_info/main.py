"""直播间信息插件。

发送「直播间」后，输出当前直播间的分区、热度、时长、榜单等实时数据。

数据来源全部是猫耳公开接口（不需要登录态）：
- ``/api/v2/live/{room_id}``                  房间信息与统计
- ``/api/v2/chatroom/meta?room_id=``          小时榜位次
- ``/api/v2/chatroom/vip/list?room_id=``      贵宾人数
- ``/api/v2/chatroom/superfan/rank?room_id=`` 超粉人数
- ``/api/v2/meta/data``                       分区 / 标签名称（缓存 1 小时）

猫耳公开接口没有提供的字段（分区排名、热度排名、Banner 位、推荐位、
福星榜、PK 胜率）直接不输出。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from core.logging import get_logger
from core.network.client import HTTPClient
from core.network.throttle import Lane
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig

_log = get_logger(__name__)

_API_BASE = "https://fm.missevan.com/api/v2"
_URL_ROOM = _API_BASE + "/live/{room_id}"
_URL_CHATROOM_META = _API_BASE + "/chatroom/meta?room_id={room_id}"
_URL_VIP = _API_BASE + "/chatroom/vip/list?room_id={room_id}"
_URL_SUPERFAN = _API_BASE + "/chatroom/superfan/rank?room_id={room_id}"
_URL_CATALOG = _API_BASE + "/meta/data"

_CATALOG_TTL = 3600.0
_UNKNOWN = "暂无"


class RoomInfoPlugin(Plugin):
    """直播间信息——房管指令，输出实时数据卡片。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        # 分区名称缓存：(取到的时间, /meta/data 响应)
        self._catalog_cache: tuple[float, dict[str, Any]] = (0.0, {})
        # 房间快照缓存：room_id -> (monotonic, snapshot)
        self._snapshot_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        # 冷却：room_id -> monotonic
        self._last_reply: dict[int, float] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        _log.info("[RoomInfo] 就绪 (plugin_id={})", self.plugin_id)

    async def terminate(self) -> None:
        self._snapshot_cache.clear()
        self._last_reply.clear()

    # ------------------------------------------------------------------ #
    # 事件入口
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_message(self, event: LiveMessageEvent) -> None:
        cfg = self._config
        if cfg is None:
            return

        text = event.message.strip()
        commands = [cfg.get_str("cmd", "直播间")]
        commands += [str(x).strip() for x in cfg.get_list("cmd_aliases")]
        if text not in [c for c in commands if c]:
            return

        if not self._can_use(event, cfg):
            reply = cfg.get_str("deny_reply", "").strip()
            if reply:
                await self._send_text(
                    event.livestream,
                    self._safe_format(reply, user=event.user.name),
                    cfg,
                )
            return

        room_id = int(getattr(event.livestream, "live_id", 0) or 0)
        if room_id <= 0:
            return

        now = time.monotonic()
        cooldown = max(0, cfg.get_int("cooldown_seconds", 5))
        if cooldown and now - self._last_reply.get(room_id, 0.0) < cooldown:
            return
        self._last_reply[room_id] = now

        snapshot = dict(await self._snapshot(room_id, cfg))
        # REST 的 statistics.online 对非主播身份通常恒为 0（公开接口拿不到
        # 房间实时在线数）。命令本身由直播间 WS 事件触发，此时 livestream
        # 上的 online_count 来自 room:statistics，是真正的实时在线人数。
        online = self._live_online_count(event.livestream)
        if online is not None and online > 0:
            snapshot["online"] = online
        message = self._format(snapshot, event.user.name, cfg)
        await self._send_text(event.livestream, message, cfg)

    @staticmethod
    def _live_online_count(livestream: Any) -> int | None:
        """取直播间 WS 已推送的在线人数；拿不到返回 None。"""
        try:
            value = int(getattr(livestream, "online_count", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return None
        return value

    # ------------------------------------------------------------------ #
    # 权限与配置
    # ------------------------------------------------------------------ #

    @staticmethod
    def _can_use(event: LiveMessageEvent, cfg: MissConfig) -> bool:
        """房管 / 主播默认可用；可在配置里放开或追加白名单。"""
        if not cfg.get_bool("admin_only", True):
            return True
        try:
            if event.user.is_admin:
                return True
        except Exception:
            pass
        allowed = {
            str(x).strip()
            for x in cfg.get_list("allowed_users")
            if str(x).strip()
        }
        return event.user.name in allowed or str(event.user.id) in allowed

    @staticmethod
    def _safe_format(template: str, **kwargs: Any) -> str:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return template

    # ------------------------------------------------------------------ #
    # 数据获取
    # ------------------------------------------------------------------ #

    async def _snapshot(self, room_id: int, cfg: MissConfig) -> dict[str, Any]:
        """取房间快照；同一房间在 ``cache_seconds`` 内复用。"""
        ttl = max(0, cfg.get_int("cache_seconds", 10))
        cached = self._snapshot_cache.get(room_id)
        if cached is not None and time.monotonic() - cached[0] < ttl:
            return cached[1]

        http = HTTPClient("", lane=Lane.BACKGROUND)
        try:
            room_info, chatroom_meta, vip_info, superfan_info, catalog_info = (
                await asyncio.gather(
                    self._get_json(http, _URL_ROOM.format(room_id=room_id)),
                    self._get_json(http, _URL_CHATROOM_META.format(room_id=room_id)),
                    self._get_json(http, _URL_VIP.format(room_id=room_id)),
                    self._get_json(http, _URL_SUPERFAN.format(room_id=room_id)),
                    self._catalog_data(http),
                )
            )
        except Exception as e:  # pragma: no cover - 兜底，正常不会走到
            _log.warning("[RoomInfo] 采集数据异常: {}", e)
            room_info = chatroom_meta = vip_info = superfan_info = catalog_info = None
        snapshot = self._parse_snapshot(
            room_info, chatroom_meta, vip_info, superfan_info, catalog_info
        )
        self._snapshot_cache[room_id] = (time.monotonic(), snapshot)
        return snapshot

    @staticmethod
    async def _get_json(http: HTTPClient, url: str) -> dict[str, Any] | None:
        try:
            data = await http.get(url)
        except Exception as e:
            _log.debug("[RoomInfo] 请求失败 {}: {}", url.split("?")[0], e)
            return None
        return data if isinstance(data, dict) else None

    async def _catalog_data(self, http: HTTPClient) -> dict[str, Any] | None:
        ts, cached = self._catalog_cache
        if cached and time.time() - ts < _CATALOG_TTL:
            return cached
        data = await self._get_json(http, _URL_CATALOG)
        if data:
            self._catalog_cache = (time.time(), data)
        return data

    # ------------------------------------------------------------------ #
    # 解析
    # ------------------------------------------------------------------ #

    @classmethod
    def _parse_snapshot(
        cls,
        room_info: dict[str, Any] | None,
        chatroom_meta: dict[str, Any] | None,
        vip_info: dict[str, Any] | None,
        superfan_info: dict[str, Any] | None,
        catalog_info: dict[str, Any] | None,
        now: float | None = None,
    ) -> dict[str, Any]:
        room = ((room_info or {}).get("info") or {}).get("room") or {}
        stats = room.get("statistics") or {}
        status = room.get("status") or {}
        meta = (chatroom_meta or {}).get("info") or {}
        hour = meta.get("hour_rank") or {}

        rank = cls._to_int(hour.get("rank"))
        gap = cls._to_int(hour.get("rank_up"))

        return {
            "room_ok": bool(room),
            "catalog": cls._catalog_label(
                catalog_info, room.get("catalog_id"), room.get("custom_tag_id")
            ),
            "duration": cls._format_duration(
                status.get("open_time"), bool(status.get("open")), now
            ),
            "accumulation": cls._to_int(stats.get("accumulation")),
            "attention": cls._to_int(stats.get("attention_count")),
            "online": cls._to_int(stats.get("online")),
            "score": cls._to_int(stats.get("score")),
            "superfan": cls._superfan_count(superfan_info),
            "vip": cls._vip_count(vip_info),
            "hour_rank": rank if rank and rank > 0 else None,
            "hour_gap": gap if gap and gap > 0 else None,
            "medal": ((room.get("medal") or {}).get("name") or "").strip() or None,
        }

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _vip_count(cls, vip_info: dict[str, Any] | None) -> int | None:
        if vip_info is None:
            return None
        info = vip_info.get("info") or {}
        return cls._to_int(info.get("vip_num")) or 0

    @classmethod
    def _superfan_count(cls, superfan_info: dict[str, Any] | None) -> int | None:
        if superfan_info is None:
            return None
        info = superfan_info.get("info") or {}
        pagination = info.get("pagination") or {}
        count = cls._to_int(pagination.get("count"))
        if count is not None:
            return count
        data = info.get("data")
        return len(data) if isinstance(data, list) else 0

    @classmethod
    def _catalog_label(
        cls,
        catalog_info: dict[str, Any] | None,
        catalog_id: Any,
        _tag_id: Any = None,
    ) -> str | None:
        """把 catalog_id 翻译成「一级分区-二级分区」。"""
        cid = cls._to_int(catalog_id)
        if cid is None or not catalog_info:
            return None
        info = catalog_info.get("info") or {}
        for group in info.get("catalogs") or []:
            if not isinstance(group, dict):
                continue
            for sub in group.get("sub_catalogs") or []:
                if cls._to_int(sub.get("catalog_id")) == cid:
                    parent = str(group.get("catalog_name") or "").strip()
                    child = str(sub.get("catalog_name") or "").strip()
                    return f"{parent}-{child}" if parent and child else (child or parent or None)
            if cls._to_int(group.get("catalog_id")) == cid:
                return str(group.get("catalog_name") or "").strip() or None
        return None

    @staticmethod
    def _format_duration(
        open_time: Any, is_open: bool, now: float | None = None
    ) -> str:
        if not is_open:
            return "未开播"
        try:
            start = int(open_time) / 1000.0
        except (TypeError, ValueError):
            return _UNKNOWN
        seconds = max(0, int((now if now is not None else time.time()) - start))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #

    @classmethod
    def _format(
        cls, snapshot: dict[str, Any], user_name: str, cfg: MissConfig
    ) -> str:
        mention = cfg.get_bool("mention", True)
        lines: list[str] = []
        if mention and user_name:
            lines.append(f"@{user_name}")

        if not snapshot.get("room_ok"):
            lines.append("暂时获取不到直播间数据，请稍后再试")
            return "\n".join(lines)

        show_unknown = cfg.get_bool("show_unknown", True)

        def add(label: str, value: Any, suffix: str = "") -> None:
            if value is None and not show_unknown:
                return
            text = _UNKNOWN if value is None else f"{value}{suffix}"
            lines.append(f"{label}：{text}")

        lines.append("直播间数据：")
        add("🎤分区", snapshot.get("catalog"))
        add("⏰直播时长", snapshot.get("duration"))
        add("👪累计人数", snapshot.get("accumulation"))
        add("👥在线人数", snapshot.get("online"))
        add("💗关注数", snapshot.get("attention"))
        add("☀️热度", snapshot.get("score"))
        add("👩超粉", snapshot.get("superfan"))
        add("🍁贵宾", snapshot.get("vip"))
        add("🏅️小时榜", snapshot.get("hour_rank"))
        add("💰小时榜差值/距离上榜", snapshot.get("hour_gap"))
        add("📛勋章", snapshot.get("medal"))
        return "\n".join(lines)

    @classmethod
    def _split_message(cls, text: str, limit: int) -> list[str]:
        if limit <= 0 or len(text) <= limit:
            return [text]
        chunks: list[str] = []
        current = ""
        for line in text.split("\n"):
            if current and len(current) + 1 + len(line) > limit:
                chunks.append(current)
                current = line
            else:
                current = line if not current else f"{current}\n{line}"
        if current:
            chunks.append(current)
        return chunks

    async def _send_text(self, livestream: Any, text: str, cfg: MissConfig) -> None:
        if not text:
            return
        limit = max(0, cfg.get_int("max_chars", 180))
        for chunk in self._split_message(text, limit):
            try:
                await livestream.send_message(chunk)
            except Exception as e:
                _log.warning("[RoomInfo] 发送失败: {}", e)
                break
