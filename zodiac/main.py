"""星座运势插件（数据源：xxapi.cn）。

发送 ``星座 天蝎座``（或 ``星座 天蝎``，别名 ``运势``）回复当日运势：

    @用户名
    　　　♏️天蝎座运势♏️

    感情：73%
    健康：70%　幸运颜色：西瓜红
    财运：70%　幸运数字：70
    工作：89%　速配星座：天秤座
    综合：76%

    ┈┈┈┈┈今日运势┈┈┈┈┈
    　　今日整体运势稳中有变，变化中蕴含着成长的机会。……

数据来源：https://v2.xxapi.cn/api/horoscope（GET，免费无需密钥）。

- 查询结果按「日期+星座」缓存到插件数据目录，同一天同一星座
  只请求一次 API（对应原版按天缓存的行为）；
- API 失败时回退本地生成（可配置关闭），保证功能不断线；
- 返回文本中夹带的推广水印（如「星h座h屋」）已自动清洗。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import urllib.request
from datetime import date, timedelta
from typing import Any

from core.logging import get_logger
from interfaces.plugin import Plugin, MissConfig
from interfaces.event import event_handler
from interfaces.event.livestream import LiveMessageEvent

_log = get_logger(__name__)

# ------------------------------------------------------------------ #
# 常量
# ------------------------------------------------------------------ #

_API_URL = "https://v2.xxapi.cn/api/horoscope?type={type}&time={time}"
"""xxapi 星座运势接口地址模板。"""

_CACHE_FILE = "horoscope_cache.json"
"""运势缓存文件名，存储在插件数据目录（data/plugins/zodiac/）。"""

_CACHE_TTL_DAYS = 30
_FETCH_TIMEOUT = 15

_HEADERS = {
    "User-Agent": "Python-urllib/3.14",
    "Accept": "application/json",
}

# 接口返回文本夹带的推广水印（如 星h座h屋 / 星8座8屋），需清洗
_WATERMARK_RE = re.compile(r"星[0-9a-zA-Z]座[0-9a-zA-Z]屋")

# ------------------------------------------------------------------ #
# 星座数据
# ------------------------------------------------------------------ #

_ZODIACS: dict[str, dict[str, Any]] = {
    "白羊座": {"name": "白羊座", "emoji": "♈️", "en": "aries", "colors": ["红色", "珊瑚橙", "石榴红"], "friend": "射手座"},
    "金牛座": {"name": "金牛座", "emoji": "♉️", "en": "taurus", "colors": ["粉色", "藕荷色", "豆沙色"], "friend": "处女座"},
    "双子座": {"name": "双子座", "emoji": "♊️", "en": "gemini", "colors": ["黄色", "柠檬黄", "明黄色"], "friend": "水瓶座"},
    "巨蟹座": {"name": "巨蟹座", "emoji": "♋️", "en": "cancer", "colors": ["银白色", "月光白", "浅蓝色"], "friend": "双鱼座"},
    "狮子座": {"name": "狮子座", "emoji": "♌️", "en": "leo", "colors": ["金色", "橘红色", "琥珀色"], "friend": "白羊座"},
    "处女座": {"name": "处女座", "emoji": "♍️", "en": "virgo", "colors": ["灰色", "米白色", "抹茶绿"], "friend": "金牛座"},
    "天秤座": {"name": "天秤座", "emoji": "♎️", "en": "libra", "colors": ["天蓝色", "淡紫色", "玫瑰粉"], "friend": "双子座"},
    "天蝎座": {"name": "天蝎座", "emoji": "♏️", "en": "scorpio", "colors": ["酒红色", "西瓜红", "深紫色"], "friend": "巨蟹座"},
    "射手座": {"name": "射手座", "emoji": "♐️", "en": "sagittarius", "colors": ["紫色", "克莱因蓝", "天青色"], "friend": "狮子座"},
    "摩羯座": {"name": "摩羯座", "emoji": "♑️", "en": "capricorn", "colors": ["咖啡色", "墨绿色", "深棕色"], "friend": "处女座"},
    "水瓶座": {"name": "水瓶座", "emoji": "♒️", "en": "aquarius", "colors": ["蓝绿色", "青瓷色", "冰蓝色"], "friend": "天秤座"},
    "双鱼座": {"name": "双鱼座", "emoji": "♓️", "en": "pisces", "colors": ["海蓝色", "薰衣草紫", "水粉色"], "friend": "天蝎座"},
}

# 本地兜底用的运势摘要文案池
_SUMMARIES = [
    "今日整体运势稳中有变，变化中蕴含着成长的机会。不要抗拒改变，学会适应和拥抱变化，你会发现新的可能。变化是生活的调味剂，让生活更精彩，让你在变化中不断成长，变得更加强大。把握机会，展现自我，今天的付出将换来明天的收获。",
    "今天你的状态渐入佳境，做什么事情都容易事半功倍。保持专注与耐心，眼前的付出不会白费。贵人运不错，遇到难题不妨主动开口求助，会有意想不到的收获。下班后给自己留一点独处时间，充电后再出发会更有力量。",
    "今日运势整体向好，适合开启新计划或推进搁置已久的事务。你的直觉相当敏锐，关键时刻不妨相信自己的判断。人际方面气氛融洽，一句真诚的问候就能拉近距离。记得劳逸结合，好运会更长久地陪伴你。",
    "今天是个适合沉淀与积累的日子，慢一点反而更快。把注意力放在手头最重要的一件事上，效率会比多线并行更高。财运平稳，不宜冲动消费。傍晚适合运动或散步，让身心都轻盈起来。",
    "今日的你元气满满，正能量感染着身边的人。工作上思路清晰，表达有力，适合开会、汇报或谈判。感情方面多一分耐心，少一分计较，关系会更加和睦。好运已经就位，就等你主动出击啦。",
    "今天整体运势平稳中带着小惊喜，可能会收到期待已久的消息或礼物。保持开放的心态，机会往往藏在日常的小事里。做事留有余地，说话留三分，会让你的好人缘更上一层楼。",
    "今日运势上佳，之前的努力开始显现成果。你会获得他人的认可与支持，自信心也随之提升。财运方面有小惊喜，但别贪心哦。晚上适合与好友小聚，分享快乐会让快乐加倍。",
    "今天适合梳理与规划，为未来铺路。把大目标拆成小步骤，一步一步来，压力自然减轻。感情运不错，多花点时间陪伴重要的人，一句贴心话胜过千言万语。",
    "今日的你灵感迸发，创意十足，非常适合创作与头脑风暴。不妨大胆说出自己的想法，会赢得意想不到的掌声。健康方面注意肩颈放松，久坐记得起身活动。好运偏爱行动派，想到就去做吧。",
    "今天运势稳中带旺，是处理积压事务的好时机。你的细心与耐心会帮大忙，别人搞不定的难题在你手里迎刃而解。财运收支平衡，理性消费更安心。睡前读几页书，会收获好心情。",
    "今日贵人运旺盛，关键时刻总有人拉你一把。多微笑、多感谢，好运会更愿意靠近你。工作按部就班即可，不必急于求成。感情方面顺其自然，给彼此一点空间反而更亲近。",
    "今天整体运势积极向上，适合主动争取机会。你的努力正在被看见，继续保持节奏就好。注意表达方式，同样的意思换个说法效果大不同。幸运的颜色会给你带来好心情，试着穿在身上吧。",
    "今日适合结交新朋友、拓展人脉，你的真诚会打动很多人。工作上合作运佳，团队协作能创造一加一大于二的效果。财运小有起色，但不建议大额投入。保持好心情，好运自然来。",
    "今天你的耐心值满分，适合处理细致复杂的任务。慢工出细活，质量比速度更重要。感情方面多倾听、少反驳，关系会更加融洽。晚上泡个热水澡，把疲惫都赶走。",
    "今日运势如沐春风，心情舒畅，看什么都顺眼。这样好的状态适合挑战一直想做的事。理财方面宜守不宜攻，稳妥为上。给家人或朋友发条问候消息，温暖会双向传递。",
    "今天是个适合出发的日子，勇气与好运都在你这边。犹豫不决的事情可以下决心了，先完成再完美。健康方面多喝水、早休息，身体是革命的本钱。相信自己，你可以的。",
    "今日的你魅力值拉满，走到哪里都是焦点。适合社交、面试、表白等一切需要展示自我的场合。工作上大胆表现，机会稍纵即逝。财运平平，把钱包看紧一点准没错。",
    "今天整体运势温和向上，适合反思与调整。回顾一下近期的得与失，你会更清楚下一步怎么走。感情方面旧日误会可借机化解，主动一点有惊喜。好运藏在日常的坚持里。",
    "今日行动力爆棚，想到就能做到，是高效的一天。列好清单逐项完成，成就感满满。财运方面有进账的可能，但也别忽略小开销。傍晚抬头看看天空，放松一下眼睛和心情。",
    "今天适合播种希望，埋下心愿的种子。也许暂时看不到结果，但方向对了就不怕路远。人际运回暖，误会与隔阂慢慢消散。记得给自己一个小奖励，你值得被温柔对待。",
]


# ------------------------------------------------------------------ #
# 插件
# ------------------------------------------------------------------ #


class ZodiacPlugin(Plugin):
    """星座运势插件——xxapi 数据源 + 按天缓存 + 本地兜底。"""

    def __init__(self, permissions: dict | None = None) -> None:
        super().__init__(permissions=permissions)
        self._config: MissConfig | None = None
        # "YYYY-MM-DD:星座名" -> API data
        self._cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self, config: MissConfig) -> None:
        self._config = config
        self._load_cache()
        _log.info(
            "[Zodiac] 就绪 (plugin_id={})  缓存={}条",
            self.plugin_id, len(self._cache),
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

        # 指令匹配：星座 <名字> 或别名
        text = event.message.strip()
        name = self._match_command(cfg, text)
        if name is None:
            return

        # 无参数 → 用法提示
        if not name:
            await event.livestream.send_message(
                cfg.get_str("help_text", "@{u} 请输入星座名，如：星座 天蝎座").replace(
                    "{u}", event.user.name
                )
            )
            return

        zodiac = self._resolve(name)
        if zodiac is None:
            await event.livestream.send_message(
                cfg.get_str(
                    "unknown_text", "@{u} 没有找到这个星座哦，试试：星座 天蝎座"
                ).replace("{u}", event.user.name)
            )
            return

        data = await self._get_fortune(zodiac, date.today())
        if data is None:
            await event.livestream.send_message(
                cfg.get_str(
                    "fetch_fail_text", "@{u} 运势查询失败啦，稍后再试试吧~"
                ).replace("{u}", event.user.name)
            )
            return

        message = self._build_message(cfg, event.user.name, zodiac, data)
        await event.livestream.send_message(message)

    # ------------------------------------------------------------------ #
    # 指令解析
    # ------------------------------------------------------------------ #

    @staticmethod
    def _match_command(cfg: MissConfig, text: str) -> str | None:
        """匹配指令，返回指令后的参数；参数为空返回 ""；不匹配返回 None。"""
        cmd = cfg.get_str("cmd_zodiac", "星座")
        aliases: list[str] = cfg.get_list("cmd_zodiac_aliases")
        for key in [cmd] + [a for a in aliases if a]:
            if text == key:
                return ""
            if text.startswith(key + " "):
                return text[len(key) + 1:].strip()
        return None

    @staticmethod
    def _resolve(name: str) -> dict[str, Any] | None:
        """把用户输入规范化为星座名并查表。

        支持 ``天蝎`` / ``天蝎座``（"座"可省略），
        括号、空格等噪声自动剥离；未命中返回 None。
        """
        raw = (
            name.replace("(", "").replace(")", "")
            .replace("（", "").replace("）", "")
            .replace(" ", "").replace("　", "")
            .strip()
        )
        if not raw:
            return None
        if raw.endswith("座"):
            return _ZODIACS.get(raw)
        return _ZODIACS.get(raw + "座")

    # ------------------------------------------------------------------ #
    # 数据获取（API → 当日缓存 → 本地兜底）
    # ------------------------------------------------------------------ #

    async def _get_fortune(
        self, zodiac: dict[str, Any], today: date
    ) -> dict[str, Any] | None:
        """获取星座运势：优先当日缓存，其次实时调用 API，最后本地兜底。

        :return: 运势数据；全部失败返回 None
        """
        cfg = self._config
        key = f"{today.isoformat()}:{zodiac['name']}"

        if key in self._cache:
            return self._cache[key]

        # 实时调用 API
        data = await self._fetch_api(zodiac)
        if data is not None:
            self._cache[key] = data
            self._prune_cache(today)
            self._save_cache()
            return data

        # API 失败 → 本地兜底（可配置关闭）
        if cfg is not None and cfg.get_bool("fallback_local", True):
            return self._local_fortune(zodiac, today)

        return None

    async def _fetch_api(self, zodiac: dict[str, Any]) -> dict[str, Any] | None:
        """调用 xxapi 星座运势接口并清洗返回数据。"""
        cfg = self._config
        time_type = cfg.get_str("time_type", "today") if cfg else "today"
        api_base = cfg.get_str("api_base", _API_URL) if cfg else _API_URL
        url = api_base.replace("{type}", zodiac["en"]).replace("{time}", time_type)

        try:
            raw = await asyncio.to_thread(self._http_get, url)
            if raw is None:
                return None
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError) as e:
            _log.warning("[Zodiac] API 响应解析失败: {}", e)
            return None

        if not isinstance(obj, dict) or obj.get("code") != 200:
            _log.warning("[Zodiac] API 返回异常: code={}", obj.get("code") if isinstance(obj, dict) else obj)
            return None

        data = obj.get("data")
        if not isinstance(data, dict):
            return None

        # 清洗文本水印（如 星h座h屋）
        fortunetext = data.get("fortunetext")
        if isinstance(fortunetext, dict):
            for section in ("all", "health", "love", "money", "work"):
                value = fortunetext.get(section)
                if isinstance(value, str):
                    fortunetext[section] = _WATERMARK_RE.sub("", value).strip()
        return data

    @staticmethod
    def _http_get(url: str) -> bytes | None:
        """同步 HTTP GET（运行在线程池中）。"""
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            return resp.read()

    def _local_fortune(
        self, zodiac: dict[str, Any], today: date
    ) -> dict[str, Any]:
        """本地兜底：按「星座+日期」种子生成好运区间运势。"""
        cfg = self._config
        score_min = cfg.get_int("score_min", 70) if cfg else 70
        score_max = cfg.get_int("score_max", 99) if cfg else 99
        if score_max < score_min:
            score_max = score_min

        rng = random.Random(f"{today.isoformat()}:{zodiac['name']}")
        index = {
            k: str(rng.randint(score_min, score_max))
            for k in ("love", "health", "money", "work", "all")
        }
        return {
            "index": index,
            "luckycolor": rng.choice(zodiac["colors"]),
            "luckynumber": str(rng.randint(0, 99)),
            "luckyconstellation": zodiac["friend"],
            "fortunetext": {"all": rng.choice(_SUMMARIES)},
            "type": "今日运势",
        }

    # ------------------------------------------------------------------ #
    # 消息构建
    # ------------------------------------------------------------------ #

    def _build_message(
        self, cfg: MissConfig, user_name: str, zodiac: dict[str, Any],
        data: dict[str, Any],
    ) -> str:
        """按配置模板组装运势回复。"""
        index = data.get("index") if isinstance(data.get("index"), dict) else {}
        fortunetext = (
            data.get("fortunetext")
            if isinstance(data.get("fortunetext"), dict)
            else {}
        )

        def score_of(key: str) -> str:
            """取百分比数值（去掉接口自带的 %）。"""
            value = index.get(key, "")
            return str(value).rstrip("%") if isinstance(value, str) else str(value)

        emoji = zodiac["emoji"]
        name = zodiac["name"]
        title_line = cfg.get_str("title_line", "　　　{e}{n}运势{e}").replace(
            "{e}", emoji
        ).replace("{n}", name)
        love_line = cfg.get_str("love_line", "感情：{v}%").replace(
            "{v}", score_of("love")
        )
        health_line = cfg.get_str(
            "health_line", "健康：{v}%　幸运颜色：{c}"
        ).replace("{v}", score_of("health")).replace(
            "{c}", str(data.get("luckycolor", ""))
        )
        money_line = cfg.get_str(
            "money_line", "财运：{v}%　幸运数字：{n}"
        ).replace("{v}", score_of("money")).replace(
            "{n}", str(data.get("luckynumber", ""))
        )
        work_line = cfg.get_str(
            "work_line", "工作：{v}%　速配星座：{f}"
        ).replace("{v}", score_of("work")).replace(
            "{f}", str(data.get("luckyconstellation", ""))
        )
        all_line = cfg.get_str("all_line", "综合：{v}%").replace(
            "{v}", score_of("all")
        )
        separator = cfg.get_str("separator", "┈┈┈┈┈{t}┈┈┈┈┈").replace(
            "{t}", str(data.get("type") or "今日运势")
        )
        summary_prefix = cfg.get_str("summary_prefix", "　　")
        summary = str(fortunetext.get("all", ""))

        lines = [
            f"@{user_name}",
            title_line,
            "",
            love_line,
            health_line,
            money_line,
            work_line,
            all_line,
            "",
            separator,
        ]
        if summary:
            lines.append(f"{summary_prefix}{summary}")

        # 可选：宜/忌
        if cfg.get_bool("show_todo", False):
            todo = data.get("todo") if isinstance(data.get("todo"), dict) else {}
            if todo.get("yi") or todo.get("ji"):
                todo_line = cfg.get_str("todo_line", "宜：{yi}　忌：{ji}").replace(
                    "{yi}", str(todo.get("yi", ""))
                ).replace("{ji}", str(todo.get("ji", "")))
                lines += ["", todo_line]

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 缓存持久化
    # ------------------------------------------------------------------ #

    def _load_cache(self) -> None:
        """加载插件数据目录中的运势缓存。"""
        if self.data is None:
            return
        data = self.data.read_json(_CACHE_FILE)
        if isinstance(data, dict):
            self._cache = {
                str(k): v for k, v in data.items() if isinstance(v, dict)
            }

    def _save_cache(self) -> None:
        """运势缓存持久化到插件数据目录。"""
        if self.data is None:
            return
        try:
            self.data.write_json(_CACHE_FILE, self._cache)
        except OSError as e:
            _log.warning("[Zodiac] 运势缓存保存失败: {}", e)

    def _prune_cache(self, today: date) -> None:
        """清理过期缓存条目。"""
        limit = (today - timedelta(days=_CACHE_TTL_DAYS)).isoformat()
        self._cache = {
            k: v for k, v in self._cache.items() if k[:10] >= limit
        }
