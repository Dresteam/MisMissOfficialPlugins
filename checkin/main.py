"""签到插件。

发送 ``签到`` / ``打卡`` / ``dd`` 进行每日打卡，统计：
累计 / 本月 / 本周 / 连续签到次数。

数据为**账户级单份**（多账户由插件实例隔离），持久化到插件数据目录。
旧版本按直播间分区的数据在载入时自动扁平化合并，不丢签到记录。
"""

from __future__ import annotations

from datetime import date, timedelta

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent

_log = get_logger(__name__)

_DATA_FILE = "checkin_data.json"


class CheckinPlugin(Plugin):
    """每日签到插件——账户级统计签到数据。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        # user_id(str) -> {"YYYY-MM-DD": count}
        self._checkins: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        self._load_data()
        _log.info(
            "[Checkin] 就绪 (plugin_id={})  已记录 {} 位用户",
            self.plugin_id, len(self._checkins),
        )

    async def terminate(self) -> None:
        self._save_data()

    # ------------------------------------------------------------------ #
    # 事件处理器
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_message(self, event: LiveMessageEvent) -> None:
        cfg = self._config
        if cfg is None:
            return

        # 指令匹配（精确）
        cmd = cfg.get_str("cmd_checkin", "签到")
        aliases: list[str] = cfg.get_list("cmd_checkin_aliases")
        text = event.message.strip()
        if text not in [cmd] + [a for a in aliases if a]:
            return

        user_id = event.user.id
        user_name = event.user.name
        today = date.today()

        user_records = self._checkins.setdefault(str(user_id), {})
        today_str = today.isoformat()

        stats = self._calc_stats(user_records, today)

        if today_str in user_records:
            # 重复打卡 → 统计消息
            message = self._build_stats_message(cfg, user_name, stats)
        else:
            # 首次打卡：先记录今日，再计算统计（今日计入后输出）
            user_records[today_str] = user_records.get(today_str, 0) + 1
            stats = self._calc_stats(user_records, today)
            # 今日第 N 位（含自己）
            rank = sum(1 for recs in self._checkins.values() if today_str in recs)
            self._save_data()
            message = self._build_success_message(cfg, user_name, rank, stats)

        await event.livestream.send_message(message)

    # ------------------------------------------------------------------ #
    # 统计计算
    # ------------------------------------------------------------------ #

    @staticmethod
    def _calc_stats(records: dict[str, int], today: date) -> dict[str, int]:
        """从签到记录计算统计值。

        :param records: {"YYYY-MM-DD": count}
        :param today: 今天日期
        :return: {"total": 累计, "month": 本月, "week": 本周, "streak": 连续}
        """
        total = 0
        month = 0
        week = 0

        # 本周起始（周一）
        week_start = today - timedelta(days=today.weekday())
        # 连续签到：从今天（或昨天，今天未签时）向前数
        streak_base = today if today.isoformat() in records else today - timedelta(days=1)

        streak = 0
        cursor = streak_base
        while cursor.isoformat() in records:
            streak += 1
            cursor -= timedelta(days=1)

        for day_str, count in records.items():
            total += count
            try:
                d = date.fromisoformat(day_str)
            except ValueError:
                continue
            if d.year == today.year and d.month == today.month:
                month += count
            if week_start <= d <= today:
                week += count

        return {"total": total, "month": month, "week": week, "streak": streak}

    # ------------------------------------------------------------------ #
    # 消息构建
    # ------------------------------------------------------------------ #

    def _build_success_message(
        self, cfg: MissConfig, user_name: str, rank: int, stats: dict[str, int]
    ) -> str:
        """首次打卡消息。"""
        header = cfg.get_str("header", "———⋆⁺₊⋆☾⋆⁺₊ ———")
        border = cfg.get_str("border", "•────⋆⁺₊⋆☾⋆⁺₊⋆────•")
        emoji = cfg.get_str("success_emoji", "· .⋆ 🪐✨打卡成功咯✨🪐⋆. ·")
        title = cfg.get_str("success_title", "\"你是今天第{n}位签到的宝贝哦~").replace("{n}", str(rank))

        lines = [
            header,
            f"@{user_name}",
            title,
            border,
            emoji,
            "",
        ]
        lines.extend(self._stats_lines(cfg, stats, prefix=True))
        lines.append("")
        lines.append(border)
        return "\n".join(lines)

    def _build_stats_message(
        self, cfg: MissConfig, user_name: str, stats: dict[str, int]
    ) -> str:
        """重复打卡统计消息。"""
        header = cfg.get_str("header", "———⋆⁺₊⋆☾⋆⁺₊ ———")
        border = cfg.get_str("border", "•────⋆⁺₊⋆☾⋆⁺₊⋆────•")
        emoji = cfg.get_str("stats_emoji", "· .⋆ 🪐✨打卡统计✨🪐⋆. ·")
        repeat = cfg.get_str("repeat_text", "小可爱今天已经打过卡了哦")

        lines = [
            header,
            f"@{user_name}",
            repeat,
            border,
            emoji,
            "",
        ]
        lines.extend(self._stats_lines(cfg, stats, prefix=False))
        lines.append("")
        lines.append(border)
        return "\n".join(lines)

    @staticmethod
    def _stats_lines(cfg: MissConfig, stats: dict[str, int], *, prefix: bool) -> list[str]:
        """生成统计行列表。

        :param prefix: 首次打卡时使用带 ┊ 🎐 前缀的格式
        """
        result: list[str] = []
        for key, cfg_key in [
            ("total", "stat_total"),
            ("month", "stat_month"),
            ("week", "stat_week"),
            ("streak", "stat_streak"),
        ]:
            template = cfg.get_str(cfg_key, "")
            if not template:
                continue
            line = template.replace("{v}", str(stats[key]))
            if not prefix:
                # 重复打卡格式不带 ┊ 前缀
                line = line.replace("┊ 🎐 ", "").replace("┊ ", "")
            result.append(line)
        return result

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #

    def _load_data(self) -> None:
        """载入签到数据，兼容旧版「按直播间分区」的结构。

        - 新结构：``{user_id: {"YYYY-MM-DD": count}}``
        - 旧结构：``{room_id: {user_id: {"YYYY-MM-DD": count}}}``

        两者靠内层值的类型区分（旧结构内层仍是 dict）。旧结构会被扁平化合并
        进账户级记录——不迁移的话，账户更换绑定直播间后旧房间的数据将再也读不到。
        """
        data = self.data.read_json(_DATA_FILE) if self.data else None
        if not isinstance(data, dict):
            return
        checkins = data.get("checkins")
        if not isinstance(checkins, dict):
            return

        for key, value in checkins.items():
            if not isinstance(value, dict) or not value:
                continue
            if any(isinstance(v, dict) for v in value.values()):
                # 旧的房间分区：{room_id: {user_id: {date: count}}}
                for uid, recs in value.items():
                    if isinstance(recs, dict):
                        self._merge_records(str(uid), recs)
            else:
                # 新的账户级记录：{user_id: {date: count}}
                self._merge_records(str(key), value)

    def _merge_records(self, uid: str, recs: dict) -> None:
        """把一份用户签到记录并入内存，同一日期取较大次数。

        兼容旧数据时同一用户可能出现在多个房间分区里，取较大值以免丢打卡。
        """
        target = self._checkins.setdefault(uid, {})
        for day, count in recs.items():
            try:
                n = int(count)
            except (TypeError, ValueError):
                continue
            day_str = str(day)
            if n > target.get(day_str, 0):
                target[day_str] = n

    def _save_data(self) -> None:
        if self.data is None:
            return
        try:
            self.data.write_json(_DATA_FILE, {
                "checkins": {
                    uid: dict(recs) for uid, recs in self._checkins.items()
                },
            })
        except OSError as e:
            _log.warning("[Checkin] 保存签到数据失败: {}", e)
