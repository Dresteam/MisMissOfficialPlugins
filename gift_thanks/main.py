"""礼物感谢插件。

收到礼物时自动发送个性化感谢消息，支持：
- 猫粮/猫罐头特殊 emoji 处理
- 非猫粮类礼物额外显示礼物价值
- 幸运礼物显示本轮幸运值% + 累计幸运值%（持久化记录，按直播间隔离）
- 礼物聚合：同一用户在同一直播间延迟时间内的连续送礼合并为一条消息
- 白榜/黑榜/黑白榜指令：幸运值排行榜（可配置仅管理员可用）

消息格式示例::

    @{user} 感谢小可爱的投喂订单：
    ᕱ ⑅ ᕱ
    (,,>᎑<,,) 谢谢礼物！
    ╭♡★-- ℒ ℴ 𝓋 ℯ --★ʚ♡ɞ╮
    ❀
    ┆　• 猫粮：🐱*4个
    ┆
    ╰ʚ♡ɞ┈┈┈┈┈┈┈♡╯
    价值：520💎

榜单格式示例::

    ★--☁✨☁ 黑白榜☁✨☁--★
    ❀ ✣(1/1页)
    ❥•1 @xxxx 幸运值：xxx
    ❥•2 @xxxx 幸运值：xxx
    ══════ ᶫᵒᵛᵉᵧₒᵤ ══════
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.logging import get_logger
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveGiftEvent, LiveMessageEvent

if TYPE_CHECKING:
    from interfaces.livestream.livestream import Livestream

_log = get_logger(__name__)

_LUCKY_STATS_FILE = "lucky_stats.json"


def _fmt_percent(value: float) -> str:
    """格式化百分比，保留一位小数，去除无意义的尾零。"""
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def _percent(actual: int, original: int) -> float:
    """计算幸运值百分比（原始值为 0 时返回 0）。"""
    return actual / original * 100 if original > 0 else 0.0


# ------------------------------------------------------------------ #
# 聚合数据结构
# ------------------------------------------------------------------ #


@dataclass
class _GiftItem:
    """单条待聚合的礼物记录。"""

    name: str
    num: int
    price: int  # 单价
    is_lucky: bool = False
    lucky_original_price: int = 0  # 幸运礼物原价（非幸运时为 0）


@dataclass
class _UserBatch:
    """单个用户在单个直播间的待聚合礼物批次。"""

    gifts: list[_GiftItem] = field(default_factory=list)
    user_name: str = ""
    livestream: "Livestream | None" = None
    timer: "asyncio.Task[None] | None" = None


class GiftThanksPlugin(Plugin):
    """收到礼物时自动发送个性化感谢消息。

    配置项（来自 ``_conf_schema.json``）：
        - ``batch_enabled`` / ``batch_delay`` — 聚合开关与延迟
        - ``enabled_rooms`` — 直播间过滤
        - ``thank_prefix`` / ``header_art`` / ``footer_art`` — 消息装饰
        - ``gift_line_format`` / ``value_line_format`` / ``lucky_line_format`` — 格式模板
        - ``cat_food_names`` / ``cat_food_emoji`` / ``default_gift_emoji`` — emoji
        - ``gift_emoji_map`` — 特定礼物 → emoji
        - ``board_cmd_white`` / ``board_cmd_black`` / ``board_cmd_both`` — 榜单指令
        - ``board_admin_only`` — 榜单是否仅管理员可用
    """

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None

        # 幸运礼物累计统计（按直播间隔离）：
        #   live_id → user_id → {"actual": int, "original": int, "name": str}
        self._lucky_stats: dict[int, dict[int, dict[str, int | str]]] = {}

        # 聚合状态：(live_id, user_id) → _UserBatch
        self._batches: dict[tuple[int, int], _UserBatch] = defaultdict(_UserBatch)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config

        cat_food = config.get_list("cat_food_names")
        emoji_map = config.get("gift_emoji_map", {})
        batch_on = config.get_bool("batch_enabled", True)
        batch_delay = config.get_float("batch_delay", 3.0)

        self._load_lucky_stats()

        _log.info(
            "[GiftThanks] 就绪 (plugin_id={})  猫粮={}  定制emoji={}种  "
            "聚合={} 延迟={}s  已记录直播间={}个",
            self.plugin_id,
            cat_food,
            len(emoji_map) if isinstance(emoji_map, dict) else 0,
            "开启" if batch_on else "关闭",
            batch_delay,
            len(self._lucky_stats),
        )

    async def terminate(self) -> None:
        """插件终止 —— 先清空所有聚合中的消息，再保存幸运值统计。"""
        await self._flush_all()
        self._save_lucky_stats()
        _log.info(
            "[GiftThanks] 已终止，{} 个直播间的幸运值统计已保存",
            len(self._lucky_stats),
        )

    # ------------------------------------------------------------------ #
    # 事件处理器：礼物感谢
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_gift(self, event: LiveGiftEvent) -> None:
        """收到礼物 → 聚合或即时发送感谢消息。"""
        cfg = self._config
        if cfg is None:
            return

        gift = event.gift

        # 1. 直播间过滤
        live_id = event.livestream.live_id
        enabled_rooms: list = cfg.get_int_list("enabled_rooms")
        if enabled_rooms and live_id not in enabled_rooms:
            return

        # 2. 构造礼物条目
        item = _GiftItem(
            name=gift.name,
            num=gift.num,
            price=gift.price,
            is_lucky=gift.is_lucky_gift,
            lucky_original_price=(
                gift.lucky_gift.price * gift.lucky_gift.num
                if gift.is_lucky_gift and gift.lucky_gift is not None
                else 0
            ),
        )
        if gift.is_lucky_gift:
            _log.debug("检测到幸运礼物，价格：{} 数量：{}",
                       gift.lucky_gift.price, gift.lucky_gift.num)

        # 3. 聚合模式
        if cfg.get_bool("batch_enabled", True):
            await self._enqueue(event, item)
        else:
            message = self._build_message(
                cfg, event.user.name, live_id, event.user.id, [item]
            )
            await event.livestream.send_message(message)

    # ------------------------------------------------------------------ #
    # 事件处理器：白榜/黑榜/黑白榜指令
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_message(self, event: LiveMessageEvent) -> None:
        """处理白榜/黑榜/黑白榜指令（可按页浏览）。"""
        cfg = self._config
        if cfg is None:
            return

        # 房间过滤
        live_id = event.livestream.live_id
        enabled_rooms: list = cfg.get_int_list("enabled_rooms")
        if enabled_rooms and live_id not in enabled_rooms:
            return

        # 指令匹配：白榜 [页] / 黑榜 [页] / 黑白榜 [页]
        board_type = self._match_board_command(cfg, event.message.strip())
        if board_type is None:
            return

        # 可配置仅管理员可用
        if cfg.get_bool("board_admin_only", False) and not event.user.is_admin:
            return

        page = self._parse_page(cfg, event.message.strip())
        message = self._build_board(cfg, event.user.name, live_id, board_type, page)
        await event.livestream.send_message(message)

    # ------------------------------------------------------------------ #
    # 聚合：入队 + 计时器
    # ------------------------------------------------------------------ #

    async def _enqueue(self, event: LiveGiftEvent, item: _GiftItem) -> None:
        """将礼物加入对应用户的聚合批次，重置延迟计时器。

        批次按 (直播间, 用户) 隔离——同一用户在不同直播间的
        送礼不会互相合并。
        """
        cfg = self._config
        if cfg is None:
            return

        key = (event.livestream.live_id, event.user.id)
        batch = self._batches[key]
        batch.gifts.append(item)
        batch.user_name = event.user.name
        batch.livestream = event.livestream

        # 取消旧计时器
        if batch.timer is not None:
            batch.timer.cancel()
            batch.timer = None

        # 启动新计时器
        delay = cfg.get_float("batch_delay", 3.0)
        batch.timer = asyncio.create_task(self._flush_after(key, delay))

    async def _flush_after(self, key: tuple[int, int], delay: float) -> None:
        """等待延迟后清空指定批次的聚合礼物并发送消息。

        :param key: (live_id, user_id) 批次键
        :param delay: 延迟秒数
        """
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # 计时器被新礼物重置

        batch = self._batches.pop(key, None)
        if batch is None or not batch.gifts or batch.livestream is None:
            return

        cfg = self._config
        if cfg is None:
            return

        message = self._build_message(
            cfg, batch.user_name, key[0], key[1], batch.gifts
        )
        await batch.livestream.send_message(message)

    async def _flush_all(self) -> None:
        """清空所有用户的聚合批次（插件终止时调用）。"""
        for key in list(self._batches.keys()):
            batch = self._batches.pop(key)
            if batch.timer is not None:
                batch.timer.cancel()
            if batch.gifts and batch.livestream is not None:
                cfg = self._config
                if cfg is None:
                    continue
                message = self._build_message(
                    cfg, batch.user_name, key[0], key[1], batch.gifts
                )
                await batch.livestream.send_message(message)

    # ------------------------------------------------------------------ #
    # 消息构建
    # ------------------------------------------------------------------ #

    def _build_message(
        self,
        cfg: MissConfig,
        user_name: str,
        live_id: int,
        user_id: int,
        items: list[_GiftItem],
    ) -> str:
        """将礼物列表构建为完整的感谢消息。

        同名单个礼物：直接使用原始行格式
        同名多个礼物：合并数量后输出一行
        不同名礼物：每类一行

        幸运值按 (直播间, 用户) 独立累计。

        :param cfg: 插件配置
        :param user_name: 赠送者用户名
        :param live_id: 直播间 ID（幸运值按直播间隔离）
        :param user_id: 赠送者用户 ID
        :param items: 待输出的礼物条目列表
        :return: 完整的感谢消息文本
        """
        # 合并同名礼物
        merged = self._merge_items(items)

        lines: list[str] = []

        # @用户 感谢语前缀
        thank_prefix = cfg.get_str("thank_prefix", "感谢小可爱的投喂订单：")
        lines.append(f"@{user_name} {thank_prefix}")

        # 头部装饰画
        header = cfg.get_str("header_art", "")
        if header:
            lines.append(header)

        # 礼物行
        gift_line_fmt = cfg.get_str(
            "gift_line_format", "┆　• {gift_name}：{gift_emoji}*{gift_num}个"
        )
        cat_food_names: list[str] = cfg.get_list("cat_food_names")
        has_non_cat_food = False

        for m in merged:
            emoji = self._resolve_emoji(cfg, m.name)
            gift_line = (
                gift_line_fmt.replace("{gift_name}", m.name)
                .replace("{gift_emoji}", emoji)
                .replace("{gift_num}", str(m.num))
            )
            lines.append(gift_line)
            if m.name not in cat_food_names:
                has_non_cat_food = True

        # 底部装饰画
        footer = cfg.get_str("footer_art", "")
        if footer:
            lines.append(footer)

        # frame 外：价值 → 本轮幸运值 → 累计幸运值
        if has_non_cat_food:
            total_value = sum(m.price * m.num for m in merged if m.name not in cat_food_names)
            if total_value > 0:  # 总价值为 0 时不输出价值行
                value_line_fmt = cfg.get_str(
                    "value_line_format", "价值：{gift_value}💗"
                )
                value_line = value_line_fmt.replace("{gift_value}", str(total_value))
                lines.append(value_line)

        lucky_actual = sum(
            m.price * m.num for m in merged if m.is_lucky and m.lucky_original_price > 0
        )
        lucky_original = sum(
            m.lucky_original_price for m in merged if m.is_lucky and m.lucky_original_price > 0
        )
        if lucky_original > 0:
            round_percent = lucky_actual / lucky_original * 100

            # 更新该用户在该直播间的独立累计幸运值
            user_stats = self._get_user_stats(live_id, user_id, user_name)
            user_stats["actual"] = int(user_stats["actual"]) + lucky_actual
            user_stats["original"] = int(user_stats["original"]) + lucky_original
            user_stats["name"] = user_name

            total_percent = _percent(
                int(user_stats["actual"]), int(user_stats["original"])
            )

            # 立即持久化，防止异常停止丢失数据
            self._save_lucky_stats()

            round_fmt = cfg.get_str(
                "lucky_round_format", "🍀 本轮幸运值：{lucky_percent}%"
            )
            lines.append(round_fmt.replace("{lucky_percent}", _fmt_percent(round_percent)))

            total_fmt = cfg.get_str(
                "lucky_total_format", "📊 累计幸运值：{total_lucky_percent}%"
            )
            lines.append(total_fmt.replace("{total_lucky_percent}", _fmt_percent(total_percent)))

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 榜单构建
    # ------------------------------------------------------------------ #

    def _match_board_command(self, cfg: MissConfig, text: str) -> str | None:
        """匹配榜单指令，返回榜单类型（"white"/"black"/"both"）。

        指令格式：``白榜`` / ``白榜 2``（页码可选）。

        :param cfg: 插件配置
        :param text: 消息文本（已 strip）
        :return: 榜单类型；不匹配返回 None
        """
        mapping = {
            "white": cfg.get_str("board_cmd_white", "白榜"),
            "black": cfg.get_str("board_cmd_black", "黑榜"),
            "both": cfg.get_str("board_cmd_both", "黑白榜"),
        }
        for board_type, cmd in mapping.items():
            if not cmd:
                continue
            if text == cmd or text.startswith(cmd + " "):
                return board_type
        return None

    @staticmethod
    def _parse_page(cfg: MissConfig, text: str) -> int:
        """从指令文本解析页码（无页码或非法时返回 1）。"""
        parts = text.split()
        if len(parts) < 2:
            return 1
        try:
            page = int(parts[1])
        except ValueError:
            return 1
        return max(page, 1)

    def _build_board(
        self,
        cfg: MissConfig,
        user_name: str,
        live_id: int,
        board_type: str,
        page: int,
    ) -> str:
        """构建榜单消息（黑榜使用独立的小煤球样式）。

        :param cfg: 插件配置
        :param user_name: 查询者用户名（用于空榜提示）
        :param live_id: 直播间 ID（榜单按直播间隔离）
        :param board_type: "white" / "black" / "both"
        :param page: 页码（从 1 开始）
        :return: 榜单消息文本
        """
        if board_type == "black":
            return self._build_black_board(cfg, user_name, live_id, page)
        if board_type == "both":
            return self._build_both_board(cfg, user_name, live_id, page)
        return self._build_white_board(cfg, user_name, live_id, page)

    def _collect_board_entries(
        self, cfg: MissConfig, live_id: int, board_type: str
    ) -> list[tuple[float, str, int]]:
        """收集并排序榜单条目（按直播间隔离，幸运值从高到低）。

        白榜：仅幸运值 ≥ 阈值；
        黑榜：仅幸运值 < 阈值（阈值为 0 时不筛选）；
        黑白榜：全部用户混合，降序排列。

        :return: [(幸运值, 用户名, user_id), ...] 已排序（降序）
        """
        threshold = cfg.get_float("board_threshold", 100.0)
        room_stats = self._lucky_stats.get(live_id, {})
        entries = [
            (
                _percent(int(s["actual"]), int(s["original"])),
                str(s.get("name", "")),
                uid,
            )
            for uid, s in room_stats.items()
            if isinstance(s, dict) and int(s.get("original", 0)) > 0
        ]
        if board_type == "black":
            if threshold > 0:
                entries = [e for e in entries if e[0] < threshold]
        elif board_type == "white":
            entries = [e for e in entries if e[0] >= threshold]
        # both：不过滤；全部从高到低
        entries.sort(key=lambda e: (-e[0], e[2]))
        return entries

    @staticmethod
    def _paginate(
        cfg: MissConfig, entries: list, page: int
    ) -> tuple[list, int, int]:
        """分页计算，返回（当前页条目, 页码, 总页数）。"""
        page_size = cfg.get_int("board_page_size", 8)
        if page_size < 1:
            page_size = 8
        total_pages = max((len(entries) + page_size - 1) // page_size, 1)
        page = min(max(page, 1), total_pages)
        start = (page - 1) * page_size
        return entries[start:start + page_size], page, total_pages

    def _build_white_board(
        self,
        cfg: MissConfig,
        user_name: str,
        live_id: int,
        page: int,
    ) -> str:
        """构建白榜消息（小福星样式，幸运值 ≥ 阈值，从高到低）。"""
        entries = self._collect_board_entries(cfg, live_id, "white")
        page_entries, page, total_pages = self._paginate(cfg, entries, page)

        lines: list[str] = []

        header = cfg.get_str("board_white_header", "🌕“看看谁是最白的小朋友")
        if header:
            lines.append(header)

        title = cfg.get_str("board_title_white", "白榜")
        title_line = cfg.get_str(
            "board_white_title", "———— ✨{type}✨ ————"
        ).replace("{type}", title)
        lines += ["", title_line]

        teaser = cfg.get_str("board_white_teaser", "")
        if teaser:
            lines += ["", teaser]

        subtitle = cfg.get_str("board_white_subtitle", "——— ̗̀♡ʚ 福星榜 ɞ♡ ̖́———")
        lines += ["", subtitle]

        if not entries:
            empty = cfg.get_str(
                "board_white_empty_text", "@{u} 还没有白白的小朋友上榜哦~"
            ).replace("{u}", user_name)
            lines += ["", empty]
        else:
            item_fmt = cfg.get_str(
                "board_white_item_line", "⭐️小福星：@{name}    {value}%"
            )
            lines.append("")
            for value, name, _uid in page_entries:
                lines.append(
                    item_fmt.replace("{name}", name)
                    .replace("{value}", f"{value:.1f}")
                )

        page_line = cfg.get_str(
            "board_white_page_line", "𓆝𓆟𓆜𓆞𓆡𓆝𓆟𓆜𓆞（{page}/{pages}页）"
        ).replace("{page}", str(page)).replace("{pages}", str(total_pages))
        lines += ["", page_line]
        return "\n".join(lines)

    def _build_both_board(
        self,
        cfg: MissConfig,
        user_name: str,
        live_id: int,
        page: int,
    ) -> str:
        """构建黑白榜消息（福星+煤球混合，从高到低）。"""
        entries = self._collect_board_entries(cfg, live_id, "both")
        page_entries, page, total_pages = self._paginate(cfg, entries, page)
        threshold = cfg.get_float("board_threshold", 100.0)

        lines: list[str] = []

        header = cfg.get_str("board_both_header", "⭐️小福星榜&小煤球榜🌑")
        if header:
            lines.append(header)

        sub_header = cfg.get_str("board_both_sub_header", "🌕“做最白的小耳朵，做小福星")
        if sub_header:
            lines += ["", sub_header]

        title_line = cfg.get_str(
            "board_both_title", "———— ✨小福星✨ ————"
        )
        lines += ["", title_line]

        if not entries:
            empty = cfg.get_str(
                "board_both_empty_text", "@{u} 还没有上榜的小朋友哦~"
            ).replace("{u}", user_name)
            lines += ["", empty]
        else:
            fuxing_fmt = cfg.get_str(
                "board_both_item_fuxing", "⭐️小福星：@{name}    {value}%"
            )
            meiqiu_fmt = cfg.get_str(
                "board_both_item_meiqiu", "🌑小煤球：@{name}    {value}%"
            )
            lines.append("")
            for value, name, _uid in page_entries:
                fmt = fuxing_fmt if value >= threshold else meiqiu_fmt
                lines.append(
                    fmt.replace("{name}", name)
                    .replace("{value}", f"{value:.1f}")
                )

        page_line = cfg.get_str(
            "board_both_page_line", "𓆝𓆟𓆜𓆞𓆡𓆝𓆟𓆜𓆞（{page}/{pages}页）"
        ).replace("{page}", str(page)).replace("{pages}", str(total_pages))
        lines += ["", page_line]
        return "\n".join(lines)

    def _build_black_board(
        self,
        cfg: MissConfig,
        user_name: str,
        live_id: int,
        page: int,
    ) -> str:
        """构建黑榜消息（小煤球样式，幸运值 < 阈值）。"""
        entries = self._collect_board_entries(cfg, live_id, "black")
        page_entries, page, total_pages = self._paginate(cfg, entries, page)

        lines: list[str] = []

        header = cfg.get_str("board_black_header", "🌚“看看谁是最黑的小朋友")
        if header:
            lines.append(header)

        title = cfg.get_str("board_black_title", "———— ✨黑榜✨ ————")
        lines += ["", title]

        teaser = cfg.get_str(
            "board_black_teaser",
            "✰黑黑的小朋友o(o･`з´o)ﾉ!!!，快去让主播洗个手祝你白白哒叭~",
        )
        if teaser:
            lines += ["", teaser]

        subtitle = cfg.get_str("board_black_subtitle", "——— ̗̀♡ʚ 煤球榜 ɞ♡ ̖́———")
        lines += ["", subtitle]

        if not entries:
            empty = cfg.get_str(
                "board_black_empty_text", "@{u} 还没有黑黑的小朋友上榜哦~"
            ).replace("{u}", user_name)
            lines += ["", empty]
        else:
            item_fmt = cfg.get_str(
                "board_black_item_line", "🌑小煤球：@{name}    {value}%"
            )
            lines.append("")
            for value, name, _uid in page_entries:
                # 黑榜数值固定保留一位小数（如 66.7% / 50.0%）
                lines.append(
                    item_fmt.replace("{name}", name)
                    .replace("{value}", f"{value:.1f}")
                )

        page_line = cfg.get_str(
            "board_black_page_line", "𓆝𓆟𓆜𓆞𓆡𓆝𓆟𓆜𓆞（{page}/{pages}页）"
        ).replace("{page}", str(page)).replace("{pages}", str(total_pages))
        lines += ["", page_line]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 内部：合并同名礼物
    # ------------------------------------------------------------------ #

    @staticmethod
    def _merge_items(items: list[_GiftItem]) -> list[_GiftItem]:
        """合并同名礼物——数量累加，幸运值合并。

        保留输入顺序。

        :param items: 原始礼物条目列表
        :return: 合并后的礼物条目列表
        """
        merged: dict[str, _GiftItem] = {}
        order: list[str] = []

        for item in items:
            if item.name in merged:
                existing = merged[item.name]
                existing.num += item.num
                existing.price = item.price  # 单价取最新
                if item.is_lucky:
                    existing.is_lucky = True
                    existing.lucky_original_price += item.lucky_original_price
            else:
                merged[item.name] = _GiftItem(
                    name=item.name,
                    num=item.num,
                    price=item.price,
                    is_lucky=item.is_lucky,
                    lucky_original_price=item.lucky_original_price,
                )
                order.append(item.name)

        return [merged[name] for name in order]

    # ------------------------------------------------------------------ #
    # 内部：emoji 解析
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_emoji(cfg: MissConfig, gift_name: str) -> str:
        """按优先级解析礼物对应的 emoji。

        优先级：gift_emoji_map → cat_food_emoji → default_gift_emoji
        """
        emoji_map = cfg.get("gift_emoji_map", {})
        if isinstance(emoji_map, dict) and gift_name in emoji_map:
            return str(emoji_map[gift_name])

        cat_food_names: list[str] = cfg.get_list("cat_food_names")
        if gift_name in cat_food_names:
            return cfg.get_str("cat_food_emoji", "🐱")

        return cfg.get_str("default_gift_emoji", "🎁")

    # ------------------------------------------------------------------ #
    # 持久化：幸运值累计（按直播间隔离）
    # ------------------------------------------------------------------ #

    def _get_user_stats(
        self, live_id: int, user_id: int, user_name: str
    ) -> dict[str, int | str]:
        """获取（或创建）某用户在指定直播间的幸运值统计。"""
        room = self._lucky_stats.setdefault(live_id, {})
        stats = room.get(user_id)
        if not isinstance(stats, dict):
            stats = {"actual": 0, "original": 0, "name": user_name}
            room[user_id] = stats
        return stats

    def _lucky_stats_path(self) -> str:
        """[deprecated] 保留用于向后兼容，新代码应直接使用 self.data。"""
        return os.path.join(self.data_dir, _LUCKY_STATS_FILE)

    def _load_lucky_stats(self) -> None:
        """从数据目录加载各直播间的幸运值累计记录。

        旧格式（按用户全局累计）因无法归属直播间，加载时重置。
        """
        data = self.data.read_json(_LUCKY_STATS_FILE) if self.data else None
        if isinstance(data, dict):
            # 兼容旧格式：{"total_actual": ...} 或 {user_id: {...}}（全局累计）→ 重置
            if "total_actual" in data or "total_original" in data:
                _log.info("[GiftThanks] 检测到旧格式幸运值数据，已重置")
                self._lucky_stats = {}
                return

            first_value = next(iter(data.values()), None)
            if isinstance(first_value, dict) and (
                "actual" in first_value or "original" in first_value
            ):
                # 旧格式：{user_id: {"actual": ..., "original": ...}}，无法归属直播间
                _log.info("[GiftThanks] 检测到旧格式（全局）幸运值数据，已重置")
                self._lucky_stats = {}
                return

            # 新格式：{live_id: {user_id: {"actual", "original", "name"}}}
            for live_id, room in data.items():
                if not isinstance(room, dict):
                    continue
                try:
                    lid = int(live_id)
                except ValueError:
                    continue
                room_stats: dict[int, dict[str, int | str]] = {}
                for uid, stats in room.items():
                    if not isinstance(stats, dict):
                        continue
                    try:
                        iuid = int(uid)
                    except ValueError:
                        continue
                    room_stats[iuid] = {
                        "actual": int(stats.get("actual", 0)),
                        "original": int(stats.get("original", 0)),
                        "name": str(stats.get("name", "")),
                    }
                if room_stats:
                    self._lucky_stats[lid] = room_stats
        else:
            self._lucky_stats = {}
        _log.debug(
            "[GiftThanks] 幸运值统计已加载: {} 个直播间",
            len(self._lucky_stats),
        )

    def _save_lucky_stats(self) -> None:
        """立即将各直播间的幸运值累计记录持久化到磁盘。"""
        if self.data is None:
            return
        try:
            self.data.write_json(
                _LUCKY_STATS_FILE,
                {
                    str(live_id): {
                        str(uid): dict(stats) for uid, stats in room.items()
                    }
                    for live_id, room in self._lucky_stats.items()
                },
            )
        except OSError as e:
            _log.warning("[GiftThanks] 保存幸运值统计失败: {}", e)
