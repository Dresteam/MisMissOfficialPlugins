"""歌单插件。

发送「歌单」指令（指令名可自定义），输出配置好的歌单列表，带装饰边框。
歌单较长时按 ``page_size`` 分页，支持 ``歌单 2`` / ``歌单2`` 翻页；
单条消息仍超过 ``max_chars`` 时按行拆成多条发送，避免平台拒收。

消息格式示例::

    ★--☁✨☁ 本场歌单☁✨☁--★
    ❀ ✣(1/3页)  发「歌单 2」翻页
    ❥•1 晴天 - 周杰伦
    ❥•2 夜曲 - 周杰伦
    ══════ ᶫᵒᵛᵉᵧₒᵤ ══════
"""

from __future__ import annotations

from core.logging import get_logger
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent
from interfaces.plugin import Plugin
from interfaces.plugin.miss_config import MissConfig

_log = get_logger(__name__)


class SongListPlugin(Plugin):
    """发送歌单指令时输出配置好的歌单。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        self._room_id: int = 0

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        self._room_id = self._resolve_room_id()
        songs = config.get_list("song_list")
        _log.info(
            "[SongList] 就绪 (plugin_id={})  房间={}  歌曲={}首",
            self.plugin_id, self._room_id or "?", len(songs),
        )

    async def on_livestream_bound(self, livestream) -> None:
        """账户后绑定直播间时同步房间号，避免初始化时没房间导致指令一直静默。"""
        self._room_id = int(getattr(livestream, "live_id", 0) or 0)
        _log.debug("[SongList] 直播间已绑定: {}", self._room_id)

    def _resolve_room_id(self) -> int:
        """取当前账户的直播间；缓存失效时从 server 重新解析。"""
        srv = getattr(self, "_server", None)
        if srv is None:
            return 0
        try:
            rooms = srv.livestreams
        except Exception:
            return 0
        if self._room_id in rooms:
            return self._room_id
        self._room_id = next(iter(rooms.keys()), 0)
        return self._room_id

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #

    @event_handler
    async def on_message(self, event: LiveMessageEvent) -> None:
        """匹配自定义歌单指令；``歌单 2`` 表示第二页。"""
        cfg = self._config
        if cfg is None:
            return
        page = self._parse_page(cfg, event.message.strip())
        if page is None:
            return

        room_id = self._resolve_room_id()
        if room_id <= 0:
            return
        srv = getattr(self, "_server", None)
        if srv is None:
            return
        room = srv.livestreams.get(room_id)
        if room is None:
            _log.warning("[SongList] 直播间 {} 已不存在，无法发送歌单", room_id)
            return
        await self._send_page(cfg, room, page)

    @staticmethod
    def _parse_page(cfg: MissConfig, text: str) -> int | None:
        """解析指令文本；不是歌单指令时返回 None。

        同时支持 ``歌单 2`` 与 ``歌单2``，指令本身可含空格（如 ``本场歌单``）。
        """
        if not text:
            return None
        names: list[str] = []
        cmd = cfg.get_str("cmd", "歌单").strip()
        if cmd:
            names.append(cmd)
        for raw in cfg.get_list("cmd_aliases"):
            alias = str(raw).strip()
            if alias and alias not in names:
                names.append(alias)

        # 长指令优先，避免「歌」把「歌单」吃掉
        for name in sorted(names, key=len, reverse=True):
            if text == name:
                return 1
            if not text.startswith(name):
                continue
            rest = text[len(name):]
            if rest and not (rest[0].isspace() or rest[0].isdigit()):
                continue  # 「歌单列表」这类不要误判成翻页参数
            rest = rest.strip()
            if not rest:
                return 1
            if rest.isdigit():
                return max(1, int(rest))
            return None  # 指令后跟了无法识别的参数，静默忽略
        return None

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _send_page(self, cfg: MissConfig, room, page: int) -> None:
        """渲染并发送指定页。

        分页先按 ``max_chars`` 预算装页，正常情况下一页就是一条消息；
        只有单行本身超长（歌名极长）时才会再按行拆成多条。
        """
        songs = [str(item) for item in cfg.get_list("song_list")]
        pages = self._build_pages(cfg, songs)
        total = len(pages)
        page = min(max(page, 1), total)
        chunk = pages[page - 1]

        header = cfg.get_str("header", "★--☁✨☁ 本场歌单☁✨☁--★")
        footer = cfg.get_str("footer", "══════ ᶫᵒᵛᵉᵧₒᵤ ══════")

        lines: list[str] = []
        if header:
            lines.append(header)
        if total > 1:
            page_fmt = cfg.get_str(
                "page_line", "❀ ✣({page}/{total}页)  发「{cmd} {next}」翻页"
            )
            page_line = self._format_page_line(cfg, page_fmt, page, total)
            if page_line.strip():
                lines.append(page_line)

        if chunk:
            lines.extend(line for _idx, line in chunk)
        else:
            empty_text = cfg.get_str("empty_text", "歌单还是空的～")
            if empty_text:
                lines.append(empty_text)
        if footer:
            lines.append(footer)

        limit = max(0, cfg.get_int("max_chars", 180))
        for part in self._split_message("\n".join(lines), limit):
            try:
                await room.send_message(part)
            except Exception as e:
                _log.warning("[SongList] 发送歌单失败: {}", e)
                break

    def _build_pages(
        self, cfg: MissConfig, songs: list[str]
    ) -> list[list[tuple[int, str]]]:
        """按 ``page_size`` 与 ``max_chars`` 预算把歌曲装页。

        每一步都预留头部、页码行、底部和换行符，保证正常页面一条消息发得下；
        页码行按 ``99/99`` 预留，避免总页数从 9 变 10 时反而超长。
        """
        page_size = cfg.get_int("page_size", 6)
        if page_size < 1:
            page_size = 6
        limit = max(0, cfg.get_int("max_chars", 180))
        prefix_fmt = cfg.get_str("item_prefix", "❥•{idx} ")

        def render(idx: int, song: str) -> str:
            return prefix_fmt.replace("{idx}", str(idx)) + song

        if limit <= 0:
            return [
                [
                    (s + i + 1, render(s + i + 1, song))
                    for i, song in enumerate(songs[s:s + page_size])
                ]
                for s in range(0, len(songs), page_size)
            ] or [[]]

        header = cfg.get_str("header", "★--☁✨☁ 本场歌单☁✨☁--★")
        footer = cfg.get_str("footer", "══════ ᶫᵒᵛᵉᵧₒᵤ ══════")
        page_fmt = cfg.get_str(
            "page_line", "❀ ✣({page}/{total}页)  发「{cmd} {next}」翻页"
        )
        reserve_line = 0
        if page_fmt.strip():
            reserve_line = len(self._format_page_line(cfg, page_fmt, 99, 99)) + 1
        base = (len(header) + 1 if header else 0) + (len(footer) + 1 if footer else 0)
        # 至少留一点空间给歌曲行；若配置本身把预算吃光，就一页一首
        budget = max(1, limit - base - reserve_line)

        def pack(cap: int) -> list[list[tuple[int, str]]]:
            pages: list[list[tuple[int, str]]] = []
            current: list[tuple[int, str]] = []
            current_len = 0
            for idx, song in enumerate(songs, start=1):
                line = render(idx, song)
                cost = len(line) + (1 if current else 0)
                if current and (len(current) >= page_size or current_len + cost > cap):
                    pages.append(current)
                    current = []
                    current_len = 0
                    cost = len(line)
                current.append((idx, line))
                current_len += cost
            if current:
                pages.append(current)
            return pages or [[]]

        # 只有一页时不会显示页码行，先按「不预留页码行」装一次；
        # 装得下就单页发完，避免为了页码行把本来一条能发完的歌单拆成两页。
        single = pack(max(1, limit - base))
        if len(single) <= 1:
            return single
        return pack(budget)

    @staticmethod
    def _format_page_line(
        cfg: MissConfig, page_fmt: str, page: int, total: int
    ) -> str:
        return (
            page_fmt
            .replace("{page}", str(page))
            .replace("{total}", str(total))
            .replace("{cmd}", cfg.get_str("cmd", "歌单"))
            .replace("{next}", str(page + 1 if page < total else 1))
        )

    @staticmethod
    def _split_message(text: str, limit: int) -> list[str]:
        """按行拆分；单行本身超长时硬切，保证每块不超过 ``limit``。"""
        if limit <= 0 or len(text) <= limit:
            return [text]

        chunks: list[str] = []
        current = ""
        for raw_line in text.split("\n"):
            line = raw_line
            # 单行就可能超限（比如一首歌名很长），先硬切
            while len(line) > limit:
                room = limit - (len(current) + 1 if current else 0)
                if room <= 0:
                    chunks.append(current)
                    current = ""
                    continue
                head, line = line[:room], line[room:]
                current = f"{current}\n{head}" if current else head
                chunks.append(current)
                current = ""

            if current and len(current) + 1 + len(line) > limit:
                chunks.append(current)
                current = line
            else:
                current = line if not current else f"{current}\n{line}"
        if current:
            chunks.append(current)
        return chunks or [""]

