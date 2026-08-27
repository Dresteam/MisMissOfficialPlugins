"""诸葛神算 384 签数据抓取脚本（维护工具，可选）。

插件默认「每次发送实时解签」——抽中未缓存的签号时实时抓取，
无需预先运行本脚本。本脚本仅用于预先补齐全部 384 签的
种子数据 ``lots.json``。

支持两个数据源：

    py update_lots.py                # 默认：吉运堂 jiyuntang.com（权威源）
    py update_lots.py huiyunge       # 备选：灵签汇 lingqian.huiyunge.com
                                       （吉运堂限流时使用，个别字与权威源
                                         存在繁简/异文差异）

仅需标准库，支持断点续抓（已有签号自动跳过），可多次运行
直至全部抓取完成。

输出格式::

    [
        {
            "number": 334,            # 签号（int）
            "fortune": "上签",        # 吉凶（上上签/上签/中上签/中签/下签/下下签）
            "poem": "自从持守定，...",  # 签诗
            "intpn_1": "一人做任何事，...",  # 解签一
            "intpn_2": "此籤以守为战，...",  # 解签二
            "detail": "你的生活将由奔波劳碌..."  # 详解（仅吉运堂）
        },
        ...
    ]
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

_TOTAL = 384
_OUTPUT = Path(__file__).parent / "lots.json"

_HEADERS = {
    # 注意：该站 WAF 会针对浏览器 UA（Chrome 等）限流 429，
    # 普通 UA（Python-urllib 等）不受影响
    "User-Agent": "Python-urllib/3.14",
    "Accept": "text/html,application/xhtml+xml",
}

# ------------------------------------------------------------------ #
# 数据源定义
# ------------------------------------------------------------------ #

_SOURCES = {
    "jiyuntang": {
        "host": "www.jiyuntang.com",
        "path": "/zhuge/{n}.html",
        "delay": 2.5,  # 请求间隔（秒），避免触发目标站点限流
    },
    "huiyunge": {
        "host": "lingqian.huiyunge.com",
        "path": "/zhuge/{n}.html",
        "delay": 0.8,
    },
}
_RETRY_DELAY = 15.0  # 请求失败/限流（HTTP 429）后的重试等待基数（秒）
_FAIL_COOLDOWN = 60.0  # 某签最终失败后的全局冷却（秒），用于度过限流窗口


def _http_get(url: str, retries: int = 3) -> bytes | None:
    """抓取单个页面（每请求新建连接）。

    注意：目标站点的 WAF 会标记单个长连接上的连续请求，
    因此必须每次新建连接，而不能复用连接。
    """
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            print(f"  ! 抓取 {url} 失败: {e}，重试 {attempt + 1}/{retries}")
            time.sleep(_RETRY_DELAY * (attempt + 1))
    return None


# ------------------------------------------------------------------ #
# 解析器
# ------------------------------------------------------------------ #

# 吉运堂：服务器渲染 HTML，字段直接位于标签内
_RE_FORTUNE = re.compile(r"<h3>诸葛神算[^<]*?签\s*【([^】]+)】</h3>")
_RE_POEM = re.compile(r"<h3>【签文】</h3>\s*<p[^>]*>([^<]+)</p>")
_RE_INT1 = re.compile(r"<h3>【解签】</h3>\s*<p[^>]*>解签一[：:]\s*([^<]*)</p>")
_RE_INT2 = re.compile(r"<p[^>]*>解签二[：:]\s*([^<]*)</p>")
_RE_DETAIL = re.compile(r"<h3>【诸葛神算第\d+签详解】</h3>\s*<p[^>]*>([\s\S]*?)</p>")


def _clean(text: str) -> str:
    """去掉内部标签与首尾空白。"""
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("　", " ").strip()


def _parse_jiyuntang(html: str) -> dict[str, object] | None:
    """解析吉运堂页面，缺任一关键字段返回 None。"""
    fortune = _RE_FORTUNE.search(html)
    poem = _RE_POEM.search(html)
    int1 = _RE_INT1.search(html)
    int2 = _RE_INT2.search(html)
    detail = _RE_DETAIL.search(html)

    if not (fortune and poem and int1):
        return None

    return {
        "fortune": fortune.group(1).strip(),
        "poem": _clean(poem.group(1)),
        "intpn_1": _clean(int1.group(1)),
        "intpn_2": _clean(int2.group(1)) if int2 else "",
        "detail": _clean(detail.group(1)) if detail else "",
    }


def _strip_tags(html: str) -> str:
    """去标签转纯文本，去掉 BOM。"""
    text = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    return text.replace("﻿", "")


def _parse_huiyunge(html: str) -> dict[str, object] | None:
    """解析灵签汇页面（纯文本结构），缺任一关键字段返回 None。

    该站签诗按短句分行、以空白分隔，与吉运堂的逗号分隔逐字对应，
    重组为「短句，短句，…。」格式。
    """
    text = _strip_tags(html)

    # 吉凶（如 上签 / 中上签）
    fortune = re.search(r"吉凶\s*([^\s\n]{2,6})", text)

    # 签诗：签文 与 解签 标题之间的内容，按空白切分短句
    poem = None
    section = re.search(r"签文(.*?)解签", text, flags=re.S)
    if section:
        clauses: list[str] = []
        for line in section.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            clauses.extend(p for p in re.split(r"\s{2,}", line) if p)
        if clauses:
            poem = "，".join(clauses) + "。"

    # 解签：以「解签二」或「孔明测字」标题为边界截取，
    # 兼容 ？/！结尾、无标点（如「吉昌」）与跨行文本
    int1 = re.search(r"解签一[：:]\s*(.*?)(?=\s*解签二[：:]|\s*孔明测字|\Z)", text, flags=re.S)
    int2 = re.search(r"解签二[：:]\s*(.*?)(?=\s*孔明测字|\Z)", text, flags=re.S)

    if not (fortune and poem and int1):
        return None

    def _collapse(value: str) -> str:
        """空白折叠为单空格并去首尾。"""
        return re.sub(r"\s+", " ", value).strip()

    return {
        "fortune": fortune.group(1).strip(),
        "poem": poem,
        "intpn_1": _collapse(int1.group(1)),
        "intpn_2": _collapse(int2.group(1)) if int2 else "",
        "detail": "",
    }


# ------------------------------------------------------------------ #
# 主流程
# ------------------------------------------------------------------ #


def _write_output(lots: dict[int, dict[str, object]]) -> None:
    """按签号排序写盘。"""
    ordered = [lots[n] for n in sorted(lots)]
    _OUTPUT.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_existing() -> dict[int, dict[str, object]]:
    """读取已有的 lots.json，用于断点续抓。"""
    try:
        data = json.loads(_OUTPUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    return {
        int(d["number"]): d
        for d in data
        if isinstance(d, dict) and d.get("number") is not None
    }


def main() -> int:
    # Windows 控制台默认 GBK，输出中文/特殊符号时强制 UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    source_name = sys.argv[1] if len(sys.argv) > 1 else "jiyuntang"
    if source_name not in _SOURCES:
        print(f"未知数据源: {source_name}（可选: {', '.join(_SOURCES)}）")
        return 2
    source = _SOURCES[source_name]
    parse = _parse_jiyuntang if source_name == "jiyuntang" else _parse_huiyunge

    lots: dict[int, dict[str, object]] = _load_existing()
    failed: list[int] = []

    for n in range(1, _TOTAL + 1):
        if n in lots:  # 断点续抓：跳过已有签号
            continue
        url = f"https://{source['host']}{source['path'].format(n=n)}"
        raw = _http_get(url)
        if raw is None:
            failed.append(n)
            print(f"  ⏳ 冷却 {_FAIL_COOLDOWN:.0f}s 后继续（可能处于限流窗口）")
            time.sleep(_FAIL_COOLDOWN)
            continue

        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            failed.append(n)
            print(f"  ✗ 第{n}签 编码错误")
            continue

        data = parse(html)
        if data is None:
            failed.append(n)
            print(f"  ✗ 第{n}签 解析失败（页面结构可能变化）")
            continue

        data["number"] = n
        lots[n] = data
        print(f"  ✓ 第{n}签【{data['fortune']}】 {data['poem'][:16]}...")

        if n % 10 == 0:
            sys.stdout.flush()
            _write_output(lots)  # 增量保存，中断后可从断点续抓
        time.sleep(source["delay"])

    # 按签号排序写盘
    _write_output(lots)
    print(f"\n完成：{len(lots)}/{_TOTAL} 签已写入 {_OUTPUT}（数据源: {source_name}）")
    if failed:
        print(f"失败签号：{failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
