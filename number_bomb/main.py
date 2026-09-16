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

文案区分：猜错与管理员跳过使用不同提示；区间只剩 1/2/3 个数时
追加特殊提示；踩中炸弹时显示炸弹数字与爆炸提示。

计分榜（不持久化）：未踩中炸弹（幸存）+1 分，踩中炸弹不加分；
仅在每局结束时输出，发送 ``数字炸弹`` 初始化游戏时清零。

与 Java 版的不同：账户级单局（多账户由插件实例隔离），
并提供 Web 前端用于查看对局状态与远程操作。
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
# 游戏状态（账户级单局）
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
        """重置为未开始状态（保留 max_number 供下次准备使用）。"""
        self.ready = False
        self.started = False
        self.target = -1
        self.lower = 1
        self.upper = self.max_number  # 必须重置，否则上局缩小的区间会残留
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
        """一轮结束后自动进入下一轮准备：保留玩家列表，重新随机目标。

        区间一并重置为 [1, max_number]——否则目标可能落在上局
        缩小的区间之外，导致区间被算成空集（如 [35, 34]）。
        """
        self.started = False
        self.ready = True
        self.target = random.randint(1, self.max_number)
        self.lower = 1
        self.upper = self.max_number
        self.current_index = 0


# ------------------------------------------------------------------ #
# 插件
# ------------------------------------------------------------------ #


class NumberBombPlugin(Plugin):
    """数字炸弹插件——账户级单局（多账户由插件实例隔离）。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        # 账户级单局游戏状态
        self._game: _GameState | None = None
        # 计分榜（不持久化）：user_id -> {"name": str, "score": int}
        # 「数字炸弹」初始化游戏时清零；猜错（幸存）+1 分，踩中炸弹不加分
        self._scores: dict[int, dict[str, Any]] = {}

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
        state = self._ensure_game()

        if state.started:
            await self._tip(event, cfg.get_str("msg_running", "游戏正在进行中哦，打完这局再来吧～"))
            return
        if not self._has_permission(event.user):
            await self._tip(event, cfg.get_str("msg_no_permission", "这个指令只有主播和管理员才能用哦～"))
            return

        # 解析可选的最大数字参数，未指定则使用当前/默认值
        max_number = cfg.get_int("default_max_number", 100)
        if len(parts) > 1:
            try:
                max_number = int(parts[1])
            except ValueError:
                await self._tip(event, cfg.get_str("msg_invalid_range", "数字不太对哦，请输入正确的范围～"))
                return
        min_number = cfg.get_int("min_max_number", 10)
        if max_number < min_number:
            await self._tip(
                event,
                cfg.get_str("msg_range_min", "范围太小啦，至少要有 {min} 个数哦～").replace(
                    "{min}", str(min_number)
                ),
            )
            return

        ok, err = await self._do_init(max_number)
        if not ok:
            await self._tip(event, err)

    async def _handle_join(self, event: LiveMessageEvent) -> None:
        """加入 / + —— 加入游戏（仅在准备阶段）。"""
        cfg = self._config
        state = self._game
        if state is None or not state.ready:
            return

        uid = event.user.id
        if uid in state.player_ids:
            await self._tip(event, cfg.get_str("msg_already_in", "你已经在游戏里啦～"))
        else:
            state.players.append((uid, event.user.name))
            await self._tip(event, cfg.get_str("msg_join_ok", "加入成功！祝你好运哟～"))

    async def _handle_quit(self, event: LiveMessageEvent) -> None:
        """退出 / - —— 退出游戏（仅在准备阶段）。"""
        cfg = self._config
        state = self._game
        if state is None or not state.ready:
            return

        uid = event.user.id
        before = len(state.players)
        state.players = [p for p in state.players if p[0] != uid]
        if len(state.players) < before:
            await self._tip(event, cfg.get_str("msg_quit_ok", "已退出游戏，下次再来玩呀～"))
        else:
            await self._tip(event, cfg.get_str("msg_not_in", "你还没有加入这局游戏哦～"))

    async def _handle_start_game(self, event: LiveMessageEvent) -> None:
        """开始 —— 打乱顺序并开赛（需至少 2 人）。"""
        cfg = self._config
        if not self._has_permission(event.user):
            await self._tip(event, cfg.get_str("msg_no_permission", "这个指令只有主播和管理员才能用哦～"))
            return
        ok, err = await self._do_start()
        if not ok:
            await self._tip(event, err)

    async def _handle_end_game(self, event: LiveMessageEvent) -> None:
        """结束 —— 授权用户强制终止游戏。"""
        cfg = self._config
        if not self._has_permission(event.user):
            await self._tip(event, cfg.get_str("msg_no_permission", "这个指令只有主播和管理员才能用哦～"))
            return
        await self._do_end()

    async def _handle_skip(self, event: LiveMessageEvent) -> None:
        """跳过 —— 授权用户跳过当前玩家。"""
        cfg = self._config
        state = self._game
        if state is None or not state.started:
            return
        if not self._has_permission(event.user):
            await self._tip(event, cfg.get_str("msg_no_permission", "这个指令只有主播和管理员才能用哦～"))
            return
        await self._do_skip()

    async def _handle_guess(self, event: LiveMessageEvent, text: str) -> None:
        """猜数——当前玩家发送区间内数字，猜中结束，未猜中缩小区间轮转。"""
        state = self._game
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
                    "msg_out_of_range", "🥺 这个数不在范围里哦，请在 [{lower}, {upper}] 中重新选一个～"
                )
                .replace("{lower}", str(state.lower))
                .replace("{upper}", str(state.upper)),
            )
            return

        if guess == state.target:
            # 踩中炸弹 = 出局（游戏规则：猜中炸弹的人输），不加分
            boom = (
                self._pick_text(
                    "msg_game_over",
                    [
                        "💥 BOOM！@{name} 踩中了炸弹 {target}，当场出局～",
                        "💥 轰隆——@{name} 撞上了炸弹 {target}，光荣牺牲！",
                        "💥 BOOM！炸弹 {target} 被 @{name} 引爆了，其他玩家成功苟活～",
                    ],
                )
                .replace("{name}", event.user.name)
                .replace("{target}", str(state.target))
                .replace("{lower}", str(state.lower))
                .replace("{upper}", str(state.upper))
            )
            await event.livestream.send_message(boom)

            # 本局结束 → 输出计分榜（其余时间不输出）
            scoreboard = self._build_scoreboard()
            if scoreboard:
                await event.livestream.send_message(scoreboard)

            state.next_round()  # 自动进入下一轮准备（保留玩家列表）
            await event.livestream.send_message(
                cfg.get_str(
                    "msg_ready",
                    "[数字炸弹] 新的一局已就绪，发送 [加入] 或 [+] 即可参与，"
                    "上局玩家可直接发送 [开始] 再战～",
                )
            )
            return

        # 未猜中 = 幸存，+1 分；随后缩小范围并轮转（与管理员「跳过」使用不同文案）
        self._add_score(event.user.id, event.user.name)
        if guess < state.target:
            state.lower = guess + 1
        else:
            state.upper = guess - 1

        _skipped, message = self._advance_turn(state, "guess", guess)
        await event.livestream.send_message(message)

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #

    def _advance_turn(
        self, state: _GameState, reason: str = "skip", guess: int | None = None
    ) -> tuple[tuple[int, str], str]:
        """推进到下一玩家，返回 (被轮转的玩家, 广播文本)。

        猜错（reason="guess"）与管理员跳过（reason="skip"）使用不同文案；
        区间只剩 1~3 个数时追加特殊提示。

        :param reason: "guess"=猜错轮转 / "skip"=管理员跳过
        :param guess: 猜错的数字（reason="guess" 时提供）
        """
        cfg = self._config
        skipped = state.players[state.current_index]
        state.current_index = (state.current_index + 1) % len(state.players)
        nxt = state.players[state.current_index]

        if reason == "guess":
            template = cfg.get_str(
                "msg_wrong_guess",
                "😮💨 @{name} 猜的 {guess} 不是炸弹，安全幸存～\n"
                "👉 轮到 @{next} 啦，当前范围：[{lower}, {upper}]",
            )
        else:
            template = cfg.get_str(
                "msg_skip",
                "⏭️ 已跳过 @{name}\n👉 轮到 @{next} 啦，当前范围：[{lower}, {upper}]",
            )

        message = (
            template.replace("{name}", skipped[1])
            .replace("{next}", nxt[1])
            .replace("{guess}", str(guess) if guess is not None else "")
            .replace("{lower}", str(state.lower))
            .replace("{upper}", str(state.upper))
        )

        hint = self._remain_hint(state)
        if hint:
            message = f"{message}\n{hint}"
        return skipped, message

    def _remain_hint(self, state: _GameState) -> str:
        """区间只剩 1/2/3 个数时的气氛文案（否则返回空串）。

        每档为文案池，随机取一条；配置写成字符串时也兼容。
        """
        remaining = state.upper - state.lower + 1
        if remaining == 1:
            # 只剩一个数 → 炸弹必然在其中，下一位猜的人必出局
            text = self._pick_text(
                "msg_remain_1",
                [
                    "😏 就剩 {a} 一个数了，下一个张口的人必踩雷，要不先写好遗言？",
                    "💀 只剩 {a} 了，炸弹稳稳地等在那里——轮到谁谁倒霉～",
                    "🙏 最后一个数是 {a}，兄弟，该你上路了……",
                    "🍵 只剩 {a} 啦，躲是躲不掉了，勇敢一点吧～",
                ],
            )
            return text.replace("{a}", str(state.lower))
        if remaining == 2:
            text = self._pick_text(
                "msg_remain_2",
                [
                    "🎉 欢乐二选一！{a} 还是 {b}？一半是天堂，一半是炸弹～",
                    "⚖️ {a} 与 {b}，猜错了活着，猜对了……就没了。",
                    "🎲 二选一时刻：{a} 或 {b}，赌上你的运气吧！",
                    "😆 激动人心的二选一来啦：{a} 和 {b}，哪个才是炸弹呢？",
                ],
            )
            return text.replace("{a}", str(state.lower)).replace("{b}", str(state.upper))
        if remaining == 3:
            text = self._pick_text(
                "msg_remain_3",
                [
                    "🍿 事情开始变得有趣起来了……{a}、{b}、{c}，中奖率三分之一哦",
                    "😏 有意思起来了呢，{a}、{b}、{c} 挑一个吧，小心脚下～",
                    "🎪 气氛突然紧张：{a}、{b}、{c} 三选一，步步惊心！",
                    "☕ 好戏开场，三选一：{a}、{b}、{c}，愿好运与你同在",
                ],
            )
            return (
                text.replace("{a}", str(state.lower))
                .replace("{b}", str(state.lower + 1))
                .replace("{c}", str(state.upper))
            )
        return ""

    def _pick_text(self, key: str, default_pool: list[str]) -> str:
        """从配置的文案池中随机取一条（兼容字符串与数组两种配置）。"""
        cfg = self._config
        value = cfg.get(key) if cfg is not None else None
        if isinstance(value, list) and value:
            return str(random.choice(value))
        if isinstance(value, str) and value:
            return value
        return random.choice(default_pool)

    # ------------------------------------------------------------------ #
    # 计分榜（不持久化，随游戏初始化重置）
    # ------------------------------------------------------------------ #

    def _add_score(self, user_id: int, user_name: str) -> None:
        """未踩中炸弹（幸存者）+1 分。"""
        entry = self._scores.get(user_id)
        if entry is None:
            self._scores[user_id] = {"name": user_name, "score": 1}
        else:
            entry["score"] = int(entry["score"]) + 1
            entry["name"] = user_name  # 更新昵称

    def _reset_scores(self) -> None:
        """重置计分榜（「数字炸弹」初始化游戏时调用）。"""
        self._scores.clear()

    def _build_scoreboard(self) -> str | None:
        """构建计分榜文本（无记录时返回 None，即不输出）。

        纯文本样式::

            ★--☁✨☁ 积分榜☁✨☁--★
            ❥•1 @小明 3分
            ❥•2 @小红 2分
        """
        cfg = self._config
        if cfg is None or not self._scores:
            return None

        # 分数降序，同分按昵称排序，保证输出稳定
        entries = sorted(
            ((int(v["score"]), str(v["name"]), uid) for uid, v in self._scores.items()),
            key=lambda e: (-e[0], e[1]),
        )
        lines = [
            cfg.get_str("msg_scoreboard_header", "★--☁✨☁ 积分榜☁✨☁--★").replace(
                "{count}", str(len(entries))
            )
        ]
        item_fmt = cfg.get_str("msg_scoreboard_item", "❥•{rank} @{name} {score}分")
        for i, (score, name, _uid) in enumerate(entries):
            lines.append(
                item_fmt.replace("{rank}", str(i + 1))
                .replace("{name}", name)
                .replace("{score}", str(score))
            )
        return "\n".join(lines)

    def _build_notify(self, state: _GameState) -> str:
        """构建当前玩家猜数提示文本。"""
        cfg = self._config
        current = state.players[state.current_index]
        message = (
            cfg.get_str(
                "msg_notify", "🎯 @{name} 轮到你啦，请发送 [{lower}, {upper}] 中的一个数～"
            )
            .replace("{name}", current[1])
            .replace("{lower}", str(state.lower))
            .replace("{upper}", str(state.upper))
        )
        hint = self._remain_hint(state)
        if hint:
            message = f"{message}\n{hint}"
        return message

    async def _tip(self, event: LiveMessageEvent, tip: str) -> None:
        """@用户名 提示消息。"""
        await event.livestream.send_message(f"@{event.user.name} {tip}")

    # ------------------------------------------------------------------ #
    # 游戏操作（聊天指令与 Web 前端共用）
    # ------------------------------------------------------------------ #

    def _account_livestream(self):
        """获取账户直播间对象（多账户版本下账户仅绑定一个直播间）。"""
        server = getattr(self, "_server", None)
        lives = getattr(server, "livestreams", None)
        if not lives:
            return None
        return next(iter(lives.values()), None)

    async def _send_live(self, message: str) -> bool:
        """向账户直播间广播消息（未连接时静默失败）。"""
        live = self._account_livestream()
        if live is None:
            _log.warning("[NumberBomb] 直播间未连接，消息未发送")
            return False
        try:
            await live.send_message(message)
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("[NumberBomb] 发送消息失败: {}", e)
            return False

    async def _do_init(
        self, max_number: int | None = None
    ) -> tuple[bool, str]:
        """初始化/重置对局（清空玩家列表）。返回 (是否成功, 错误信息)。"""
        cfg = self._config
        state = self._ensure_game()
        if state.started:
            return False, cfg.get_str("msg_running", "游戏正在进行中哦，打完这局再来吧～")
        if max_number is None:
            max_number = (
                state.max_number
                if state.max_number
                else cfg.get_int("default_max_number", 100)
            )
        state.reinit(max_number)
        self._reset_scores()  # 初始化游戏 → 重置计分榜
        await self._send_live(
            cfg.get_str(
                "msg_ready",
                "[数字炸弹] 新的一局已就绪，发送 [加入] 或 [+] 即可参与，上局玩家可直接发送 [开始] 再战～",
            ),
        )
        return True, ""

    async def _do_start(self) -> tuple[bool, str]:
        """打乱顺序并开赛（需至少 2 人）。返回 (是否成功, 错误信息)。"""
        cfg = self._config
        state = self._game
        if state is None or not state.ready:
            return False, cfg.get_str(
                "msg_not_ready", "还没有开局哦，先发送「数字炸弹」准备一下吧～"
            )
        if state.started:
            return False, cfg.get_str("msg_running", "游戏正在进行中哦，打完这局再来吧～")
        if len(state.players) <= 1:
            return False, cfg.get_str("msg_not_enough", "人太少啦，至少要有 2 位玩家才能开始哦～")

        # 打乱玩家顺序并开始游戏
        random.shuffle(state.players)
        header = cfg.get_str("msg_start_header", "💣 数字炸弹开局！本轮顺序：")
        order_fmt = cfg.get_str("msg_order_line", "{rank}. {name}")
        lines = [header] + [
            order_fmt.replace("{rank}", str(i + 1)).replace("{name}", name)
            for i, (_uid, name) in enumerate(state.players)
        ]
        rule = cfg.get_str(
            "msg_start_rule", "（轮流猜数，踩中炸弹的人出局哦～）"
        )
        if rule:
            lines.append(rule)
        await self._send_live("\n".join(lines))

        state.ready = False
        state.started = True
        state.current_index = 0
        await self._send_live(self._build_notify(state))
        return True, ""

    async def _do_skip(self) -> tuple[bool, str]:
        """跳过当前玩家。返回 (是否成功, 错误信息)。"""
        cfg = self._config
        state = self._game
        if state is None or not state.started or not state.players:
            return False, cfg.get_str("msg_not_started", "现在没有正在进行的对局哦")
        _skipped, message = self._advance_turn(state, "skip")
        await self._send_live(message)
        return True, ""

    async def _do_end(self) -> tuple[bool, str]:
        """结束游戏。

        进行中：强制结束并自动进入下一轮准备（保留玩家列表）；
        准备中：强制停止（清空玩家，回到未初始化，无法再加入）。
        """
        cfg = self._config
        state = self._game
        if state is None:
            return False, cfg.get_str("msg_not_started", "现在没有正在进行的对局哦")
        if state.started:
            state.next_round()  # 自动进入下一轮准备（保留玩家列表）
            await self._send_live(cfg.get_str("msg_force_end", "🛑 本局已被强制结束"))
            await self._send_live(
                cfg.get_str(
                    "msg_ready",
                    "[数字炸弹] 新的一局已就绪，发送 [加入] 或 [+] 即可参与，上局玩家可直接发送 [开始] 再战～",
                ),
            )
            return True, ""
        if state.ready:
            state.reset()  # 强制停止：清空玩家，回到未初始化
            await self._send_live(cfg.get_str("msg_force_stop", "🛑 游戏已强制停止，玩家列表已清空"))
            return True, ""
        return False, cfg.get_str("msg_not_started", "现在没有正在进行的对局哦")

    # ------------------------------------------------------------------ #
    # Web UI：状态查询与远程操作
    # ------------------------------------------------------------------ #

    def _scores_text(self) -> str:
        """计分榜摘要文本（供 Web 表格渲染，如「小明 3分、小红 2分」）。"""
        if not self._scores:
            return ""
        entries = sorted(
            ((int(v["score"]), str(v["name"])) for v in self._scores.values()),
            key=lambda e: (-e[0], e[1]),
        )
        return "、".join(f"{name} {score}分" for score, name in entries)

    def _status_rows(self) -> list[dict[str, Any]]:
        """生成账户直播间的对局状态行（单行,供 Web 表格渲染）。"""
        server = getattr(self, "_server", None)
        lives = getattr(server, "livestreams", None) or {}
        live = next(iter(lives.values()), None)
        live_id = getattr(live, "live_id", 0)
        room_name = getattr(live, "room_name", "") or f"房间{live_id}"
        state = self._game
        rows: list[dict[str, Any]] = []

        if state is None:
            rows.append({
                "room_id": live_id, "room_name": room_name, "status": "未初始化",
                "range": "-", "player_count": 0, "players": "", "current_player": "",
                "scores": self._scores_text(),
                "can_init": True, "can_start": False, "can_skip": False,
                "can_end": False, "can_stop": False,
            })
            return rows

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
            "room_id": live_id, "room_name": room_name, "status": status,
            "range": (f"{state.lower} ~ {state.upper}" if state.started or state.ready else "-"),
            "player_count": len(names), "players": players_text,
            "current_player": names[state.current_index] if state.started and names else "",
            "scores": self._scores_text(),
            "can_init": not state.started,
            "can_start": state.ready and not state.started and len(names) >= 2,
            "can_skip": state.started, "can_end": state.started,
            "can_stop": state.ready and not state.started,
        })
        return rows

    def register_routes(self, router: Any) -> None:
        """注册插件 Web UI API 端点（前缀 /api/plugin/{name}/ui）。"""
        from fastapi import Body
        from fastapi.responses import JSONResponse

        @router.get("/stats")
        async def get_stats():
            running = 1 if (self._game and self._game.started) else 0
            ready = 1 if (self._game and self._game.ready and not self._game.started) else 0
            players = len(self._game.players) if self._game else 0
            return JSONResponse({
                "running": running,
                "ready": ready,
                "players": players,
            })

        @router.get("/rooms")
        async def get_rooms():
            return JSONResponse(self._status_rows())

        @router.post("/action")
        async def do_action(body: dict = Body(...)):
            action = str(body.get("action", ""))
            if action == "init":
                ok, msg = await self._do_init()
            elif action == "start":
                ok, msg = await self._do_start()
            elif action == "skip":
                ok, msg = await self._do_skip()
            elif action == "end":
                ok, msg = await self._do_end()
            else:
                ok, msg = False, "未知操作"
            return JSONResponse({"ok": ok, "msg": msg})

        @router.post("/init")
        async def do_init_form(body: dict = Body(...)):
            """自定义范围初始化（表单提交）。"""
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

            ok, msg = await self._do_init(max_number)
            return JSONResponse({"ok": ok, "msg": msg})

    def _ensure_game(self) -> _GameState:
        """获取（或创建）账户级单局游戏状态。"""
        if self._game is None:
            self._game = _GameState()
        return self._game

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
