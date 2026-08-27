"""求签插件（每次发送实时解签）。

发送 ``求签``（别名 ``抽签``）随机抽取一支诸葛神算签文并回复：

    @用户名
    求签结果：您抽到的是第三百三十四签【上签】

    签诗：自从持守定，功在众人先，别有非常喜，随龙到九天。

    解签：一人做任何事，具有恒心、毅力，则其成功机会较大。

    ♱⋰ ⋱✮⋰ ⋱♱⋰ ⋱✮⋰ ⋱♱⋰ ⋱✮⋰

**每次发送实时解签**：抽中未缓存的签号时，实时从吉运堂
（https://www.jiyuntang.com/zhuge/{n}.html）抓取该签并解签，
同时写入插件数据目录缓存——同号之后秒回、断网也可兜底。
插件自带 ``lots.json`` 种子数据（预抓取的一部分签文，可用
``update_lots.py`` 补齐全部 384 签）；抓取失败且无任何缓存时
才回复失败提示。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import urllib.request
from pathlib import Path
from typing import Any

from core.logging import get_logger
from interfaces.plugin import Plugin, MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent

_log = get_logger(__name__)

# ------------------------------------------------------------------ #
# 常量
# ------------------------------------------------------------------ #

_SEED_FILE = Path(__file__).parent / "lots.json"
"""签文种子数据（与插件代码一同打包，预抓取的部分签文）。"""

_CACHE_FILE = "lots_cache.json"
"""抽签缓存文件名，存储在插件数据目录（data/plugins/qiuqian/）。"""

_URL_TMPL = "https://www.jiyuntang.com/zhuge/{n}.html"
"""签文页面地址模板。"""

_TOTAL_LOTS = 384
_FETCH_RETRIES = 2
_FETCH_TIMEOUT = 15

_HEADERS = {
    # 注意：该站 WAF 会针对浏览器 UA（Chrome 等）限流 429，
    # 普通 UA 不受影响
    "User-Agent": "Python-urllib/3.14",
    "Accept": "text/html,application/xhtml+xml",
}

# 签文页面解析（与 update_lots.py 保持一致）
_RE_FORTUNE = re.compile(r"<h3>诸葛神算[^<]*?签\s*【([^】]+)】</h3>")
_RE_POEM = re.compile(r"<h3>【签文】</h3>\s*<p[^>]*>([^<]+)</p>")
_RE_INT1 = re.compile(r"<h3>【解签】</h3>\s*<p[^>]*>解签一[：:]\s*([^<]*)</p>")
_RE_INT2 = re.compile(r"<p[^>]*>解签二[：:]\s*([^<]*)</p>")

_CN_DIGITS = "零一二三四五六七八九"


# ------------------------------------------------------------------ #
# 工具函数
# ------------------------------------------------------------------ #


def _to_cn_number(n: int) -> str:
    """整数转中文数字（1~999，如 334 → 三百三十四）。"""
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        prefix = "" if tens == 1 else _CN_DIGITS[tens]  # 10 作"十"，不作"一十"
        return f"{prefix}十" + (f"{_CN_DIGITS[ones]}" if ones else "")
    hundreds, rest = divmod(n, 100)
    head = f"{_CN_DIGITS[hundreds]}百"
    if rest == 0:
        return head
    if rest < 10:
        return f"{head}零{_CN_DIGITS[rest]}"
    tens, ones = divmod(rest, 10)
    return head + f"{_CN_DIGITS[tens]}十" + (f"{_CN_DIGITS[ones]}" if ones else "")


def _clean(text: str) -> str:
    """去掉内部标签与首尾空白。"""
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("　", " ").strip()


def _parse_lot_page(html: str, number: int) -> dict[str, Any] | None:
    """从签文页面 HTML 提取签文，缺关键字段返回 None。"""
    fortune = _RE_FORTUNE.search(html)
    poem = _RE_POEM.search(html)
    int1 = _RE_INT1.search(html)
    int2 = _RE_INT2.search(html)
    if not (fortune and poem and int1):
        return None
    return {
        "number": number,
        "fortune": fortune.group(1).strip(),
        "poem": _clean(poem.group(1)),
        "intpn_1": _clean(int1.group(1)),
        "intpn_2": _clean(int2.group(1)) if int2 else "",
    }


# ------------------------------------------------------------------ #
# 插件
# ------------------------------------------------------------------ #


class QiuqianPlugin(Plugin):
    """求签插件——每次发送实时解签，抽过的签缓存本地。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        self._lots: dict[int, dict[str, Any]] = {}  # 签号 -> 签文

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        self._load_seed()
        self._load_cache()
        _log.info(
            "[Qiuqian] 就绪 (plugin_id={})  本地签文={}条  实时抓取={}",
            self.plugin_id, len(self._lots),
            config.get_bool("live_fetch", True),
        )

    # ------------------------------------------------------------------ #
    # 事件处理器
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

        # 指令匹配（精确）
        cmd = cfg.get_str("cmd_qiuqian", "求签")
        aliases: list[str] = cfg.get_list("cmd_qiuqian_aliases")
        if event.message.strip() not in [cmd] + [a for a in aliases if a]:
            return

        lot = await self._draw_lot()
        if lot is None:
            await event.livestream.send_message(
                cfg.get_str("fetch_fail_text", "求签失败啦，稍后再来试试吧~")
            )
            return

        message = self._build_message(cfg, event.user.name, lot)
        await event.livestream.send_message(message)

    # ------------------------------------------------------------------ #
    # 抽签
    # ------------------------------------------------------------------ #

    async def _draw_lot(self) -> dict[str, Any] | None:
        """随机抽取一支签：命中缓存直接返回，未命中实时抓取。

        :return: 签文；抓取失败且无任何缓存时返回 None
        """
        cfg = self._config
        number = random.randint(1, _TOTAL_LOTS)

        # 命中本地缓存（种子 + 历史抽签）
        if number in self._lots:
            return self._lots[number]

        # 未命中 → 实时抓取（可配置关闭）
        if cfg is not None and cfg.get_bool("live_fetch", True):
            lot = await self._fetch_lot(number)
            if lot is not None:
                self._lots[number] = lot
                self._save_cache()
                return lot

        # 抓取失败/关闭实时抓取 → 从已有缓存随机兜底
        if self._lots:
            return random.choice(list(self._lots.values()))
        return None

    async def _fetch_lot(self, number: int) -> dict[str, Any] | None:
        """实时抓取指定签号的签文（在线程池中执行，避免阻塞事件循环）。"""
        url = _URL_TMPL.format(n=number)
        for attempt in range(_FETCH_RETRIES):
            try:
                raw = await asyncio.to_thread(self._http_get, url)
                if raw is None:
                    continue
                lot = _parse_lot_page(raw.decode("utf-8", errors="ignore"), number)
                if lot is not None:
                    return lot
                _log.warning("[Qiuqian] 第{}签 页面解析失败（结构可能变化）", number)
            except Exception as e:  # noqa: BLE001
                _log.warning("[Qiuqian] 第{}签 抓取异常: {}", number, e)
            if attempt < _FETCH_RETRIES - 1:
                await asyncio.sleep(1.5)
        return None

    @staticmethod
    def _http_get(url: str) -> bytes | None:
        """同步 HTTP GET（运行在线程池中）。"""
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            return resp.read()

    # ------------------------------------------------------------------ #
    # 消息构建
    # ------------------------------------------------------------------ #

    def _build_message(
        self, cfg: MissConfig, user_name: str, lot: dict[str, Any]
    ) -> str:
        """按配置模板组装求签回复。"""
        result_line = cfg.get_str(
            "result_line", "求签结果：您抽到的是第{n}签【{f}】"
        ).replace("{n}", _to_cn_number(int(lot["number"]))).replace(
            "{f}", str(lot["fortune"])
        )
        poem_line = cfg.get_str("poem_line", "签诗：{p}").replace(
            "{p}", str(lot["poem"])
        )
        intpn_line = cfg.get_str("intpn_line", "解签：{i}").replace(
            "{i}", str(lot["intpn_1"])
        )
        separator = cfg.get_str(
            "separator", "♱⋰ ⋱✮⋰ ⋱♱⋰ ⋱✮⋰ ⋱♱⋰ ⋱✮⋰"
        )

        lines = [
            f"@{user_name}",
            result_line,
            "",
            poem_line,
            "",
            intpn_line,
        ]

        # 可选：追加解签二
        if cfg.get_bool("show_intpn_2", False) and lot.get("intpn_2"):
            intpn2_line = cfg.get_str("intpn_2_line", "解签二：{i}").replace(
                "{i}", str(lot["intpn_2"])
            )
            lines += ["", intpn2_line]

        lines += ["", separator]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 本地签文加载 / 缓存持久化
    # ------------------------------------------------------------------ #

    def _load_seed(self) -> None:
        """加载打包的种子签文（lots.json）。"""
        try:
            data = json.loads(_SEED_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            _log.warning("[Qiuqian] 种子签文加载失败: {}", e)
            return
        if isinstance(data, list):
            for d in data:
                if isinstance(d, dict) and d.get("number") and d.get("poem"):
                    self._lots[int(d["number"])] = d

    def _load_cache(self) -> None:
        """加载插件数据目录中的抽签缓存（实时抓取过的签）。"""
        if self.data is None:
            return
        data = self.data.read_json(_CACHE_FILE)
        if isinstance(data, list):
            for d in data:
                if isinstance(d, dict) and d.get("number") and d.get("poem"):
                    self._lots[int(d["number"])] = d

    def _save_cache(self) -> None:
        """实时抓取到的签文持久化到插件数据目录。"""
        if self.data is None:
            return
        try:
            ordered = [self._lots[n] for n in sorted(self._lots)]
            self.data.write_json(_CACHE_FILE, ordered)
        except OSError as e:
            _log.warning("[Qiuqian] 抽签缓存保存失败: {}", e)
