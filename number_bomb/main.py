"""数字炸弹插件。

玩法：
- 授权用户发送 ``数字炸弹 [最大值]`` 初始化/重置游戏（清空玩家列表，
  默认 100，最小 10；游戏进行中时该指令仅提示，不会打断对局）
- 观众发送 ``加入`` / ``+`` 参与游戏，``退出`` / ``-`` 离开
- 授权用户发送 ``开始`` 随机打乱顺序并开赛
- 当前玩家发送区间内的数字猜炸弹：猜中结束游戏，
  未猜中则缩小区间并轮到下一位
- 一轮游戏结束后（猜中或强制结束）自动进入下一轮准备并
  **保留玩家列表**：上局玩家直接 ``开始`` 即可再开一局，
  新玩家可 ``+`` 加入；需要清空列表重新开局时发送 ``数字炸弹``
- ``结束`` 在准备阶段为强制停止：清空玩家并回到未初始化
  （玩家无法再加入）；Web 端同样可操作
- 授权用户可发送 ``跳过`` 跳过当前玩家

与 Java 版的不同：游戏状态按直播间隔离——多个直播间
可以同时各自进行一局游戏，互不干扰。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent

_log = get_logger(__name__)


# ------------------------------------------------------------------ #
# 游戏状态（每个直播间一局）
# ------------------------------------------------------------------ #


@dataclass
class _GameState:
    """单局数字炸弹游戏状态。"""

    ready: bool = False
    """已准备（等待玩家加入）。"""

    started: bool = False
    """已开始（进行中）。"""

    max_number: int = 100
    target: int = -1
    lower: int = 1
    upper: int = 100
    players: list[tuple[int, str]] = field(default_factory=list)
    """(user_id, name) 按当前顺序排列。"""

    current_index: int = 0

    @property
    def player_ids(self) -> set[int]:
        """参与玩家 ID 集合（用于去重）。"""
        return {uid for uid, _ in self.players}

    def reset(self) -> None:
        """重置为未开始状态（保留 max_number / upper 供下次准备使用）。"""
        self.ready = False
        self.started = False
        self.target = -1
        self.lower = 1
        self.players = []
        self.current_index = 0

    def reinit(self, max_number: int | None = None) -> None:
        """「数字炸弹」指令：清空玩家列表并重新初始化一轮。

        :param max_number: 新的最大数字；None 时沿用当前值
        """
        self.reset()
        if max_number is not None:
            self.max_number = max_number
        self.upper = self.max_number
        self.target = random.randint(1, self.max_number)
        self.ready = True

    def next_round(self) -> None:
        """一轮结束后自动进入下一轮准备：保留玩家列表，重新随机目标。"""
        self.started = False
        self.ready = True
        self.target = random.randint(1, self.max_number)
        self.lower = 1
        self.current_index = 0


# ------------------------------------------------------------------ #
# 插件
# ------------------------------------------------------------------ #


class NumberBombPlugin(Plugin):
    """数字炸弹插件——每直播间独立一局，互不干扰。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        # live_id → 游戏状态
        self._games: dict[int, _GameState] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        _log.info("[NumberBomb] 就绪 (plugin_id={})", self.plugin_id)

    # ------------------------------------------------------------------ #
    # 事件处理器：指令分发
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_message(self, event: LiveMessageEvent) -> None:
        cfg = self._config
        if cfg is None:
            return

        # 房间过滤
        live_id = event.livestream.live_id
        enabled_rooms: list = cfg.get_int_list("enabled_rooms")
        if enabled_rooms and live_id not in enabled_rooms:
            return

        text = event.message.strip()
        if not text:
            return
        parts = text.split()
        cmd = parts[0]

        # 指令分发（与 Java 版顺序一致）
        if cmd == cfg.get_str("cmd_ready", "数字炸弹"):
            await self._handle_ready(event, parts)
        elif cmd in cfg.get_list("cmd_join", ["加入", "+"]):
            await self._handle_join(event)
        elif cmd in cfg.get_list("cmd_quit", ["退出", "-"]):
            await self._handle_quit(event)
        elif cmd == cfg.get_str("cmd_start_game", "开始"):
            await self._handle_start_game(event)
        elif cmd == cfg.get_str("cmd_end_game", "结束"):
            await self._handle_end_game(event)
        elif cmd == cfg.get_str("cmd_skip", "跳过"):
            await self._handle_skip(event)
        else:
            await self._handle_guess(event, text)

    # ------------------------------------------------------------------ #
    # 命令处理器
    # ------------------------------------------------------------------ #

    async def _handle_ready(self, event: LiveMessageEvent, parts: list[str]) -> None:
        """数字炸弹 [最大值] —— 初始化/重置游戏（清空玩家列表并重新开局）。"""
        cfg = self._config
        state = self._games.setdefault(event.livestream.live_id, _GameState())

        if state.started:
            await self._tip(event, cfg.get_str("msg_running", "游戏正在进行中哦~"))
            return
        if not self._has_permission(event.user):
            await self._tip(event, cfg.get_str("msg_no_permission", "没有权限使用该命令~"))
            return

        # 解析可选的最大数字参数，未指定则使用当前/默认值
        max_number = cfg.get_int("default_max_number", 100)
        if len(parts) > 1:
            try:
                max_number = int(parts[1])
            except ValueError:
                await self._tip(event, cfg.get_str("msg_invalid_range", "请输入正确的范围~"))
                return
        min_number = cfg.get_int("min_max_number", 10)
        if max_number < min_number:
            await self._tip(
                event,
                cfg.get_str("msg_range_min", "数字范围不可小于 {min} 哦~").replace(
                    "{min}", str(min_number)
                ),
            )
            return

        ok, err = await self._do_init(event.livestream.live_id, max_number)
        if not ok:
            await self._tip(event, err)

    async def _handle_join(self, event: LiveMessageEvent) -> None:
        """加入 / + —— 加入游戏（仅在准备阶段）。"""
        cfg = self._config
        state = self._games.get(event.livestream.live_id)
        if state is None or not state.ready:
            return

        uid = event.user.id
        if uid in state.player_ids:
            await self._tip(event, cfg.get_str("msg_already_in", "已经在游戏中啦~"))
        else:
            state.players.append((uid, event.user.name))
            await self._tip(event, cfg.get_str("msg_join_ok", "加入成功~"))

    async def _handle_quit(self, event: LiveMessageEvent) -> None:
        """退出 / - —— 退出游戏（仅在准备阶段）。"""
        cfg = self._config
        state = self._games.get(event.livestream.live_id)
        if state is None or not state.ready:
            return

        uid = event.user.id
        before = len(state.players)
        state.players = [p for p in state.players if p[0] != uid]
        if len(state.players) < before:
            await self._tip(event, cfg.get_str("msg_quit_ok", "退出成功~"))
        else:
            await self._tip(event, cfg.get_str("msg_not_in", "未在游戏中哦~"))

    async def _handle_start_game(self, event: LiveMessageEvent) -> None:
        """开始 —— 打乱顺序并开赛（需至少 2 人）。"""
        cfg = self._config
        if not self._has_permission(event.user):
            await self._tip(event, cfg.get_str("msg_no_permission", "没有权限使用该命令~"))
            return
        ok, err = await self._do_start(event.livestream.live_id)
        if not ok:
            await self._tip(event, err)

    async def _handle_end_game(self, event: LiveMessageEvent) -> None:
        """结束 —— 授权用户强制终止游戏。"""
        cfg = self._config
        if not self._has_permission(event.user):
            await self._tip(event, cfg.get_str("msg_no_permission", "没有权限使用该命令~"))
            return
        await self._do_end(event.livestream.live_id)

    async def _handle_skip(self, event: LiveMessageEvent) -> None:
        """跳过 —— 授权用户跳过当前玩家。"""
        cfg = self._config
        state = self._games.get(event.livestream.live_id)
        if state is None or not state.started:
            return
        if not self._has_permission(event.user):
            await self._tip(event, cfg.get_str("msg_no_permission", "没有权限使用该命令~"))
            return
        await self._do_skip(event.livestream.live_id)

    async def _handle_guess(self, event: LiveMessageEvent, text: str) -> None:
        """猜数——当前玩家发送区间内数字，猜中结束，未猜中缩小区间轮转。"""
        state = self._games.get(event.livestream.live_id)
        if state is None or not state.started or not state.players:
            return
        if state.players[state.current_index][0] != event.user.id:
            return

        try:
            guess = int(text)
        except ValueError:
            return  # 非数字输入忽略

        cfg = self._config

        if guess < state.lower or guess > state.upper:
            await self._tip(
                event,
                cfg.get_str(
                    "msg_out_of_range", "请发送位于 [{lower}, {upper}] 区间内的一个数~"
                )
                .replace("{lower}", str(state.lower))
                .replace("{upper}", str(state.upper)),
            )
            return

        if guess == state.target:
            await event.livestream.send_message(
                cfg.get_str(
                    "msg_game_over", "🎉 游戏结束，@{name} 猜中了数字！"
                ).replace("{name}", event.user.name)
            )
            state.next_round()  # 自动进入下一轮准备（保留玩家列表）
            await event.livestream.send_message(
                cfg.get_str(
                    "msg_ready",
                    "[数字炸弹] 已经准备完成啦，可以发送 [加入] 或者 [+] 加入游戏~",
                )
            )
            return

        if guess < state.target:
            state.lower = guess + 1
        else:
            state.upper = guess - 1

        await self._skip_current(event, state)

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #

    async def _skip_current(self, event: LiveMessageEvent, state: _GameState) -> None:
        """轮到下一位玩家，并广播当前范围。"""
        _skipped, message = self._advance_turn(state)
        await event.livestream.send_message(message)

    def _advance_turn(self, state: _GameState) -> tuple[tuple[int, str], str]:
        """推进到下一玩家，返回 (被跳过的玩家, 广播文本)。"""
        cfg = self._config
        skipped = state.players[state.current_index]
        state.current_index = (state.current_index + 1) % len(state.players)
        nxt = state.players[state.current_index]

        message = (
            cfg.get_str(
                "msg_skip",
                "⏭️ 已跳过 @{name}\n👉 轮到 @{next}，当前范围：[{lower}, {upper}]",
            )
            .replace("{name}", skipped[1])
            .replace("{next}", nxt[1])
            .replace("{lower}", str(state.lower))
            .replace("{upper}", str(state.upper))
        )
        return skipped, message

    def _build_notify(self, state: _GameState) -> str:
        """构建当前玩家猜数提示文本。"""
        cfg = self._config
        current = state.players[state.current_index]
        return (
            cfg.get_str(
                "msg_notify", "🎯 @{name} 请发送 [{lower}, {upper}] 的一个数~"
            )
            .replace("{name}", current[1])
            .replace("{lower}", str(state.lower))
            .replace("{upper}", str(state.upper))
        )

    async def _tip(self, event: LiveMessageEvent, tip: str) -> None:
        """@用户名 提示消息。"""
        await event.livestream.send_message(f"@{event.user.name} {tip}")

    # ------------------------------------------------------------------ #
    # 游戏操作（聊天指令与 Web 前端共用）
    # ------------------------------------------------------------------ #

    def _get_livestream(self, live_id: int):
        """获取服务器中的直播间对象（未连接返回 None）。"""
        server = getattr(self, "_server", None)
        lives = getattr(server, "livestreams", None)
        if not lives:
            return None
        return lives.get(live_id)

    async def _send_live(self, live_id: int, message: str) -> bool:
        """向直播间广播消息（直播间未连接时静默失败）。"""
        live = self._get_livestream(live_id)
        if live is None:
            _log.warning("[NumberBomb] 直播间 {} 未连接，消息未发送", live_id)
            return False
        try:
            await live.send_message(message)
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("[NumberBomb] 直播间 {} 发送消息失败: {}", live_id, e)
            return False

    async def _do_init(
        self, live_id: int, max_number: int | None = None
    ) -> tuple[bool, str]:
        """初始化/重置对局（清空玩家列表）。返回 (是否成功, 错误信息)。"""
        cfg = self._config
        state = self._games.setdefault(live_id, _GameState())
        if state.started:
            return False, cfg.get_str("msg_running", "游戏正在进行中哦~")
        if max_number is None:
            max_number = (
                state.max_number
                if state.max_number
                else cfg.get_int("default_max_number", 100)
            )
        state.reinit(max_number)
        await self._send_live(
            live_id,
            cfg.get_str(
                "msg_ready",
                "[数字炸弹] 已经准备完成啦，可以发送 [加入] 或者 [+] 加入游戏~",
            ),
        )
        return True, ""

    async def _do_start(self, live_id: int) -> tuple[bool, str]:
        """打乱顺序并开赛（需至少 2 人）。返回 (是否成功, 错误信息)。"""
        cfg = self._config
        state = self._games.get(live_id)
        if state is None or not state.ready:
            return False, cfg.get_str(
                "msg_not_ready", "还没有初始化游戏哦，请先发送 数字炸弹~"
            )
        if state.started:
            return False, cfg.get_str("msg_running", "游戏正在进行中哦~")
        if len(state.players) <= 1:
            return False, cfg.get_str("msg_not_enough", "游戏人数不足~")

        # 打乱玩家顺序并开始游戏
        random.shuffle(state.players)
        header = cfg.get_str("msg_start_header", "游戏开始！顺序：")
        order_fmt = cfg.get_str("msg_order_line", "{rank} - {name}")
        lines = [header] + [
            order_fmt.replace("{rank}", str(i + 1)).replace("{name}", name)
            for i, (_uid, name) in enumerate(state.players)
        ]
        await self._send_live(live_id, "\n".join(lines))

        state.ready = False
        state.started = True
        state.current_index = 0
        await self._send_live(live_id, self._build_notify(state))
        return True, ""

    async def _do_skip(self, live_id: int) -> tuple[bool, str]:
        """跳过当前玩家。返回 (是否成功, 错误信息)。"""
        cfg = self._config
        state = self._games.get(live_id)
        if state is None or not state.started or not state.players:
            return False, cfg.get_str("msg_not_started", "游戏未在进行中")
        _skipped, message = self._advance_turn(state)
        await self._send_live(live_id, message)
        return True, ""

    async def _do_end(self, live_id: int) -> tuple[bool, str]:
        """结束游戏。

        进行中：强制结束并自动进入下一轮准备（保留玩家列表）；
        准备中：强制停止（清空玩家，回到未初始化，无法再加入）。
        """
        cfg = self._config
        state = self._games.get(live_id)
        if state is None:
            return False, cfg.get_str("msg_not_started", "游戏未在进行中")
        if state.started:
            state.next_round()  # 自动进入下一轮准备（保留玩家列表）
            await self._send_live(live_id, cfg.get_str("msg_force_end", "游戏已强制结束"))
            await self._send_live(
                live_id,
                cfg.get_str(
                    "msg_ready",
                    "[数字炸弹] 已经准备完成啦，可以发送 [加入] 或者 [+] 加入游戏~",
                ),
            )
            return True, ""
        if state.ready:
            state.reset()  # 强制停止：清空玩家，回到未初始化
            await self._send_live(live_id, cfg.get_str("msg_force_stop", "游戏已强制停止"))
            return True, ""
        return False, cfg.get_str("msg_not_started", "游戏未在进行中")

    # ------------------------------------------------------------------ #
    # Web UI：状态查询与远程操作
    # ------------------------------------------------------------------ #

    def _room_rows(self) -> list[dict[str, Any]]:
        """生成所有直播间的对局状态行（供 Web 表格渲染）。"""
        server = getattr(self, "_server", None)
        lives = getattr(server, "livestreams", None) or {}
        rows: list[dict[str, Any]] = []

        for live_id in sorted(set(lives.keys()) | set(self._games.keys())):
            live = lives.get(live_id)
            room_name = getattr(live, "room_name", "") or f"房间{live_id}"
            state = self._games.get(live_id)

            if state is None:
                rows.append({
                    "room_id": live_id,
                    "room_name": room_name,
                    "status": "未初始化",
                    "range": "-",
                    "player_count": 0,
                    "players": "",
                    "current_player": "",
                    "can_init": True,
                    "can_start": False,
                    "can_skip": False,
                    "can_end": False,
                    "can_stop": False,
                })
                continue

            if state.started:
                status = "进行中"
            elif state.ready:
                status = "准备中"
            else:
                status = "未初始化"

            names = [name for _uid, name in state.players]
            players_text = "、".join(
                f"{name}(当前)" if state.started and i == state.current_index else name
                for i, name in enumerate(names)
            )

            rows.append({
                "room_id": live_id,
                "room_name": room_name,
                "status": status,
                "range": (
                    f"{state.lower} ~ {state.upper}"
                    if state.started or state.ready
                    else "-"
                ),
                "player_count": len(names),
                "players": players_text,
                "current_player": names[state.current_index] if state.started and names else "",
                "can_init": not state.started,
                "can_start": state.ready and not state.started and len(names) >= 2,
                "can_skip": state.started,
                "can_end": state.started,
                "can_stop": state.ready and not state.started,
            })
        return rows

    def register_routes(self, router: Any) -> None:
        """注册插件 Web UI API 端点（前缀 /api/plugin/{name}/ui）。"""
        from fastapi import Body
        from fastapi.responses import JSONResponse

        @router.get("/stats")
        async def get_stats():
            running = sum(1 for s in self._games.values() if s.started)
            ready = sum(1 for s in self._games.values() if s.ready and not s.started)
            players = sum(len(s.players) for s in self._games.values())
            return JSONResponse({
                "running": running,
                "ready": ready,
                "players": players,
            })

        @router.get("/rooms")
        async def get_rooms():
            return JSONResponse(self._room_rows())

        @router.post("/action")
        async def do_action(body: dict = Body(...)):
            live_id = int(body.get("room_id", 0) or 0)
            action = str(body.get("action", ""))
            if live_id <= 0:
                return JSONResponse({"ok": False, "msg": "缺少 room_id"})
            if action == "init":
                ok, msg = await self._do_init(live_id)
            elif action == "start":
                ok, msg = await self._do_start(live_id)
            elif action == "skip":
                ok, msg = await self._do_skip(live_id)
            elif action == "end":
                ok, msg = await self._do_end(live_id)
            else:
                ok, msg = False, "未知操作"
            return JSONResponse({"ok": ok, "msg": msg})

        @router.post("/init")
        async def do_init_form(body: dict = Body(...)):
            """自定义范围初始化（表单提交）。"""
            live_id = int(body.get("room_id", 0) or 0)
            if live_id <= 0:
                return JSONResponse({"ok": False, "msg": "缺少 room_id"})

            raw_max = body.get("max_number")
            max_number: int | None = None
            if raw_max not in (None, "", 0, "0"):
                try:
                    max_number = int(raw_max)
                except (TypeError, ValueError):
                    return JSONResponse({"ok": False, "msg": "最大数字无效"})
                min_number = self._config.get_int("min_max_number", 10)
                if max_number < min_number:
                    return JSONResponse({"ok": False, "msg": f"数字范围不可小于 {min_number}"})

            ok, msg = await self._do_init(live_id, max_number)
            return JSONResponse({"ok": ok, "msg": msg})

    def _has_permission(self, user: Any) -> bool:
        """检查用户是否有控制游戏的权限。

        授权用户列表（op_users）中的用户始终有权限；
        配置 ``allow_admins`` 开启时直播间管理员也有权限。
        """
        cfg = self._config
        if user.id in cfg.get_int_list("op_users"):
            return True
        if cfg.get_bool("allow_admins", True) and user.is_admin:
            return True
        return False
