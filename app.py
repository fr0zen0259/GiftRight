import json
import hmac
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
try:
    # Python 3.7+ 自带这个组合好的类。
    from http.server import ThreadingHTTPServer
except ImportError:  # pragma: no cover - 仅为 3.6 兼容（阿里云镜像自带 3.6.8）
    import socketserver

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        """3.6 没有 http.server.ThreadingHTTPServer，用等价的 MixIn 组合。

        行为和 3.7+ 的实现一致：每个请求一个线程、守护线程、地址复用。
        """
        daemon_threads = True
        allow_reuse_address = True

from pathlib import Path

from engine import (
    CATALOG,
    DIY_GIFTS,
    OCCASION_RULES,
    REGIONAL_VARIATIONS,
    TABOOS,
    _gift_meaning,
    _item_taboo_notes,
    _custom_avoid_blocks,
    _has,
    _regional_notes,
    build_taboo_notes,
    evaluate_product,
    normalize_intent,
    recommend,
)
# The rule node that ships to Dify (dify/rule_node_final.py, matching the
# online node verbatim) is also the one used locally, so offline tests run
# the same eligibility logic as production.
from dify.rule_node_final import (
    has_alcohol,
    has_fragrance,
    has_nuts,
    main as dify_rule_node_main,
)


ROOT = Path(__file__).resolve().parent


def _load_env_file():
    candidates = [
        ROOT / ".env",
        ROOT.parent / ".env",
        ROOT.parent.parent / ".env",
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
                break
            except Exception:
                pass


_load_env_file()


def _clean_dify_base_url():
    base = os.getenv("DIFY_BASE_URL", "https://api.dify.ai").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base

WEB_ROOT = ROOT / "web"
EXPERIMENT_PATH = ROOT / "eval" / "experiment_results.json"
TAOBAO_CATALOG_PATH = ROOT / "data" / "real_catalog" / "products_taobao_authorized_v3.json"
TAOBAO_FAMILY_CATALOG_PATH = ROOT / "data" / "real_catalog" / "products_taobao_family_catalog.json"
PORT = int(os.getenv("GIFT_RIGHT_PORT", "8787"))
DIFY_CANDIDATE_LIMIT = int(os.getenv("DIFY_CANDIDATE_LIMIT", "36"))
PREVIEW_CANDIDATE_LIMIT = int(os.getenv("GIFT_RIGHT_PREVIEW_LIMIT", "60"))
DISPLAY_CANDIDATE_LIMIT = int(os.getenv("GIFT_RIGHT_DISPLAY_LIMIT", "30"))
USE_FAMILY_REPRESENTATIVES = os.getenv(
    "GIFT_RIGHT_USE_FAMILY_REPRESENTATIVES", "0"
).strip().lower() in {"1", "true", "yes"}
DIFY_TIMEOUT_SECONDS = int(os.getenv("DIFY_TIMEOUT_SECONDS", "360"))
DIFY_RESPONSE_MODE = os.getenv("DIFY_RESPONSE_MODE", "streaming").strip().lower()
DIFY_MAX_RETRIES = max(0, min(int(os.getenv("DIFY_MAX_RETRIES", "2")), 3))
DIFY_CHAT_API_KEY = os.getenv("DIFY_CHAT_API_KEY", "").strip()
DIFY_CHAT_RESPONSE_MODE = os.getenv("DIFY_CHAT_RESPONSE_MODE", "blocking").strip().lower()
APP_VERSION = "gift-right-2.1"
# Dify 交回空候选时，把上游 outputs 追加到这里，便于定位是哪个节点没给东西。
# 只写 outputs，不写请求头/密钥。可用环境变量关掉。
_EMPTY_DUMP_PATH = os.getenv(
    "GIFT_RIGHT_EMPTY_DUMP",
    str(ROOT / "_dify_empty_dump.jsonl"),
)
# Dify 工作流里 Ranker（LLM 2）的节点 id。它偶尔产出非法 JSON，被下游
# 「代码执行 2」静默清空，服务端需要靠这个 id 从 SSE trace 里把原文取回来。
RANKER_NODE_ID = os.getenv("GIFT_RIGHT_RANKER_NODE_ID", "1792294044029").strip()
DIFY_LAST_ERROR = None
DIFY_JOBS = {}
DIFY_JOBS_LOCK = threading.Lock()
DIFY_JOB_TTL_SECONDS = 900
MAX_REQUEST_BODY_BYTES = 1_048_576

# ── 访问限制（2026-11-18）──────────────────────────────────────────────
# 不设 GIFT_RIGHT_ACCESS_TOKEN 就是本地模式：行为与之前完全一致，不做任何拦截。
# 设了之后，/api/* 必须带正确口令（cookie 或 header），否则 401。
ACCESS_TOKEN = os.getenv("GIFT_RIGHT_ACCESS_TOKEN", "").strip()
ACCESS_COOKIE = "gr_access"
# 每个 IP 每分钟允许的写操作次数（推荐 + 对话）。0 = 不限流。
RATE_LIMIT_PER_MIN = int(os.getenv("GIFT_RIGHT_RATE_LIMIT", "6"))
# /api/research 暴露评测数据，默认关闭。
EXPOSE_RESEARCH = os.getenv("GIFT_RIGHT_EXPOSE_RESEARCH", "0").strip().lower() in {"1", "true", "yes"}
# 口令错误时最多允许几次尝试，防止暴力猜。
ACCESS_MAX_FAILS = 5
_ACCESS_FAILS = {}
_ACCESS_FAILS_LOCK = threading.Lock()
_RATE_BUCKETS = {}
_RATE_LOCK = threading.Lock()


def _parse_api_payload(raw_body):
    if len(raw_body) > MAX_REQUEST_BODY_BYTES:
        raise ValueError("请求体过大")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("请求格式无效") from error
    if not isinstance(payload, dict):
        raise ValueError("请求 JSON 顶层必须是对象")
    # Budget fields are numbers, not "whatever survives float()". A silent
    # downgrade to None used to turn "budget_max": "abc" into "no limit".
    for key in ("budget_min", "budget_max"):
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是数字") from None
        if number < 0:
            raise ValueError(f"{key} 不能为负数")
        payload[key] = number
    if (
        payload.get("budget_min") is not None
        and payload.get("budget_max") is not None
        and payload["budget_min"] > payload["budget_max"]
    ):
        raise ValueError("预算下限不能大于上限，请把两个数对调或修正")
    return payload


_CATALOG_CACHE = {"key": None, "value": None}


_KB_CARD_CACHE = {"key": None, "value": None}
KB_CARD_FILES = (
    ROOT / "data" / "dify_uploads" / "01_商品事实库_v2.md",
    ROOT / "data" / "dify_uploads" / "06_商品事实库_合并重点名版.md",
)


def _load_kb_product_cards():
    """Parse product cards out of the 06 knowledge-base markdown.

    Dify 的知识检索会命中知识库 md 里的商品卡（商品ID=...；标题=...），
    这些 ID 大多不在本地候选池中。这里把 md 卡片解析回 dict，供
    enrich_dify_result 补全事实字段，避免页面出现「未命名候选」。
    mtime 缓存与 _load_real_catalog 同款策略。
    """
    keys = []
    for path in KB_CARD_FILES:
        try:
            stat = path.stat()
            keys.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    cache_key = tuple(keys)
    if _KB_CARD_CACHE["key"] == cache_key:
        return _KB_CARD_CACHE["value"]
    cards = []
    kid = "商品ID"
    seen_pids = set()
    for path in KB_CARD_FILES:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for block in content.split("商品ID=")[1:]:
            fields = {}
            head = block.split("\n", 1)[0]
            fields["商品ID"] = head.split("；", 1)[0].split(";", 1)[0].strip()
            for part in re.split("[;；]", head)[1:]:
                if "=" in part:
                    k, _, v = part.partition("=")
                    k = k.strip()
                    if k:
                        fields[k] = v.strip()
            pid = fields.get(kid) or ""
            if not pid:
                continue
            # 小红书笔记卡的商品 ID 是笔记 ID（note_xxx），一条笔记挂多个商品，
            # 直接用会互相覆盖/重复占 enrich 索引。首卡保留原 ID，其余追加序号
            # （note_xxx#2、#3…）保证索引键唯一；卡片本身都留在候选池里，
            # 不丢商品。
            if pid in seen_pids:
                ordinal = 2
                while f"{pid}#{ordinal}" in seen_pids:
                    ordinal += 1
                pid = f"{pid}#{ordinal}"
            seen_pids.add(pid)
            title = fields.get("标题") or fields.get("商品名称") or ""
            # 重点名：LLM 改写过的短标题（品牌+核心品名），有就用作展示名
            title_key = fields.get("重点名") or ""
            try:
                price = float(str(fields.get("价格") or "").strip())
            except (TypeError, ValueError):
                price = None
            cards.append(
                {
                    "id": pid,
                    "product_id": pid,
                    "name": title_key or title,
                    "title": title_key or title,
                    "title_raw": title,
                    "gift_type": "purchased",
                    "category": fields.get("类别") or "",
                    "price": price,
                    "currency": "CNY",
                    "source_platform": fields.get("来源平台") or "",
                    "source_url": fields.get("详情链接") or "",
                    "source_title": title_key or title,
                    "description": fields.get("备注") or "",
                    "status": "active",
                    "source": f"知识库卡片（{fields.get('来源平台') or '未知平台'}，快照 {fields.get('观察时间') or '日期未记'}）",
                }
            )
    _KB_CARD_CACHE.update(key=cache_key, value=cards)
    return cards


def _load_real_catalog():
    """Load the product catalog once per file version, not once per request.

    The 3.6 MB JSON used to be re-read (and re-parsed) up to two times for
    every API call. An mtime-keyed cache keeps the same semantics — edits to
    the file invalidate the cache — while repeated requests reuse the list.
    """
    paths = []
    if USE_FAMILY_REPRESENTATIVES and TAOBAO_FAMILY_CATALOG_PATH.exists():
        paths.append((TAOBAO_FAMILY_CATALOG_PATH, True))
    paths.append((TAOBAO_CATALOG_PATH, False))
    for path, prefer_representatives in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size, prefer_representatives)
        if _CATALOG_CACHE["key"] == cache_key:
            return _CATALOG_CACHE["value"]
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, list):
            continue
        if prefer_representatives:
            representatives = [
                row for row in value
                if isinstance(row, dict) and row.get("product_family_representative")
            ]
            if representatives:
                _CATALOG_CACHE.update(key=cache_key, value=representatives)
                return representatives
        _CATALOG_CACHE.update(key=cache_key, value=value)
        return value
    return []


def _brand_in_text(brand, text):
    """品牌名是否出现在文本里。

    2026-11-17：原来是裸的 `brand in text`，两个问题：
      · 英文品牌永远匹配不上——目录标题写「Apple」「CHERRY」「Nespresso」，
        表里是小写，子串比较区分大小写。
      · 短的英文品牌会误命中——「we」会命中 watch/week 里的 we。
    改成：中文品牌仍用子串（中文没有词边界），ASCII 品牌要求两侧不是字母数字。
    """
    if not brand:
        return False
    # str.isascii() 是 3.7 才有的。云端镜像可能是 3.6，用等价写法。
    if all(ord(ch) < 128 for ch in brand):
        pattern = r"(?<![a-z0-9])" + re.escape(brand) + r"(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return brand in text


def _candidate_brand(row):
    text = " ".join(
        str(row.get(field) or "")
        for field in ("title", "merchant", "shop_name", "brand")
    ).lower()
    hit = next(
        (brand for brand in PRODUCT_BRANDS if _brand_in_text(brand, text)),
        "其他品牌",
    )
    return BRAND_ALIASES.get(hit, hit)


def _candidate_category(row):
    category = str(row.get("category") or "").strip()
    if category:
        return category
    family_key = _product_family_key(row)
    return family_key.split("|", 1)[-1] or "其他"


def _select_diverse_rows(ranked_rows, target):
    """Select a varied pool while keeping hard-filtered candidates intact."""
    target = max(0, int(target or 0))
    if target == 0:
        return []

    selected = []
    selected_ids = set()
    seen_families = set()
    family_counts = {}
    category_counts = {}
    brand_counts = {}
    brandcat_counts = {}

    def try_add(row, relaxed=False):
        product_id = str(
            row.get("item_id")
            or row.get("product_id")
            or row.get("offer_id")
            or ""
        ).strip()
        # A catalog refresh can contain duplicate rows for the same item.
        # Keep one canonical card per item_id so later Dify merging cannot
        # silently shrink the displayed candidate count.
        if product_id and product_id in selected_ids:
            return False
        family_key = _product_family_key(row)
        if not relaxed and family_key in seen_families:
            return False
        if relaxed and family_counts.get(family_key, 0) >= 3:
            return False
        category = _candidate_category(row)
        brand = _candidate_brand(row)
        if not relaxed and category_counts.get(category, 0) >= 2:
            return False
        # 2026-11-17：品牌上限原来只在第一遍生效，第二遍（放宽补位）完全不限品牌。
        # 后果：三台 Nespresso 胶囊机（C30 意式 / C30 美式 / Inissia）挤进同一批，
        # 用户看到三张「换汤不换药」的卡。它们 family_key 全是「通用|咖啡礼盒」，
        # 靠 family 上限（放宽后 3）根本拦不住，只有品牌能拦。
        #
        # 但「其他品牌」不是一个品牌，是 90.5% 目录（3782/4180 行）共用的兜底桶。
        # 第一版没排除它，结果这个桶也被限到 2 件：33 件池子里 31 件全是识别出的
        # 品牌，正好跟真实分布反过来。手柄（归在这个桶里）就这么被挤出池子。
        # 所以限流只对**真品牌**生效。
        if brand != "其他品牌" and brand_counts.get(brand, 0) >= 2:
            return False
        # 2026-11-17：品牌上限 2 仍然放过「同品牌 + 同品类」的两件。
        # 实测 vivo 只有 2 件商品，都是手表，于是 vivo WATCH 6 和 vivo Watch GT2
        # 一起进了 AI 精选——两张卡讲的是同一件事。
        # 真正该拦的信号是品牌和品类一起看：同品牌同品类最多 1 件。
        # 实测代价 0：五个场景池子大小一件不少，重复对全部消失。
        if brand != "其他品牌" and brandcat_counts.get((brand, category), 0) >= 1:
            return False
        seen_families.add(family_key)
        family_counts[family_key] = family_counts.get(family_key, 0) + 1
        category_counts[category] = category_counts.get(category, 0) + 1
        brand_counts[brand] = brand_counts.get(brand, 0) + 1
        brandcat_counts[(brand, category)] = (
            brandcat_counts.get((brand, category), 0) + 1
        )
        if product_id:
            selected_ids.add(product_id)
        selected.append(row)
        return True

    # First pass maximizes category and brand diversity.
    for item in ranked_rows:
        if len(selected) >= target:
            break
        try_add(item[-1], relaxed=False)

    # Second pass fills the requested pool without repeating a product family.
    if len(selected) < target:
        for item in ranked_rows:
            if len(selected) >= target:
                break
            try_add(item[-1], relaxed=True)
    return selected


def _text_terms(value):
    """把自由文本切成能跟商品标题对上的词。

    2026-11-17：原来用 [\\u3400-\\u9fff]{2,} 直接抓连续汉字串，中文没有空格，
    整句就成了一个 token——「他喜欢打游戏，常喝咖啡，最近想换一把机械键盘」
    切出来是 5 个整句，没有一个能出现在商品标题里。实测后果：用户明说想换
    机械键盘，500–1000 元档唯一那把 CHERRY MX2.0S（999 元）在候选池打分里
    得 0 分，被多样性选择器挤出 36 名之外，一件键盘都没进 Dify。
    free_text 等于完全没生效。

    改法：整句之外，再切出 2–4 字的连续汉字片段当候选词。中文短语的边界
    没法靠正则找，但「机械键盘」「咖啡」「键盘」「游戏」这些都短，覆盖
    2–4 字就够命中标题。保留整句 token 不影响原有行为（它本来就匹配不上）。

    2026-11-17 第二次修：切词解决了「键盘」，但没解决「同一件事两种叫法」。
    用户说「喜欢打游戏」，商品写「八爪鱼6Pro精英手柄礼盒」——标题里既没有
    「游戏」也没有「打」，打分 0，一件该是首选的礼物排到第 72 名，出不了池。
    目录里「游戏」34 行、「手柄」只有 3 行，说明这两个词在商品侧是分开的，
    光靠切用户的句子永远对不上。所以补一张口语→商品用词的映射表。
    """
    text = str(value or "")
    tokens = []
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        # 整段（原行为，几乎匹配不到，但保留以免破坏既有打分）
        if len(run) >= 2:
            tokens.append(run)
        # 2–4 字滑动窗口：这是真正能命中标题的部分
        for size in (2, 3, 4):
            if len(run) < size:
                continue
            for start in range(len(run) - size + 1):
                tokens.append(run[start:start + size])
    tokens.extend(re.findall(r"[a-zA-Z0-9]+", text))

    # 口语 → 商品用词。左列是用户会说的，右列是商品标题里实际写的。
    # 只做单向扩展：用户提到「游戏」时补「手柄/掌机/电竞」，反过来不做，
    # 免得一件手柄把「游戏」这个词反向塞进所有商品。
    for spoken, written in _SPOKEN_TO_CATALOG.items():
        if spoken in text:
            tokens.extend(written)

    stop = {"礼物", "礼品", "送礼", "希望", "不要", "可以", "一个", "朋友",
            "比较", "最近", "喜欢", "想换", "一把", "的人", "东西", "什么"}
    seen = set()
    result = []
    for token in tokens:
        token = token.lower()
        if token in stop or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


# 用户口语与商品用词的对应表。每条都是按目录实测补的，不是凭感觉写的：
# 「游戏」34 行 vs「手柄」3 行、「掌机」6 行、「电竞」11 行——用户说打游戏时，
# 这些才是他真正会喜欢的东西。
_SPOKEN_TO_CATALOG = {
    "游戏": ("手柄", "掌机", "电竞", "游戏机"),
    "打游戏": ("手柄", "掌机", "电竞"),
    "咖啡": ("咖啡", "胶囊", "意式", "手冲"),
    "喝茶": ("茶叶", "普洱", "铁观音", "红茶", "绿茶"),
    "茶叶": ("茶叶", "普洱", "铁观音", "红茶", "绿茶"),
    "喝酒": ("白酒", "红酒", "洋酒", "威士忌"),
    "看书": ("图书", "书籍", "书"),
    "读书": ("图书", "书籍", "书"),
    "写字": ("钢笔", "签字笔", "笔记本"),
    "运动": ("健身", "跑步", "瑜伽", "球"),
    "健身": ("健身", "筋膜枪", "哑铃", "运动"),
    "做饭": ("锅", "厨具", "空气炸锅", "料理"),
    "音乐": ("耳机", "音箱", "音响"),
    "拍照": ("相机", "拍立得", "摄影"),
    "护肤": ("水乳", "面霜", "精华", "面膜"),
    "化妆": ("口红", "粉底", "彩妆", "眼影"),
    "睡觉": ("枕", "床品", "睡衣", "助眠"),
}


PACKAGING_ONLY_TERMS = (
    "空盒",
    "空礼盒",
    "空包装",
    "仅包装",
    "包装盒",
    "彩盒",
    "飞机盒",
    "纸箱",
    "手提袋",
    "礼袋",
    "礼品袋",
    "包装纸",
    "材料包",
    "打包盒",
    "瓦楞",
)


def _is_packaging_only(row):
    """Treat a record as packaging when it is marked so or reads as packaging.

    An explicit `packaging_only` flag wins; otherwise the title is matched
    against strong packaging vocabulary. Titles that only mention a gift box
    around a real product ("礼盒装") are not matched.
    """
    flag = row.get("packaging_only")
    if flag not in (None, "", 0, "0", False):
        value = str(flag).strip().lower()
        if value in {"1", "true", "yes", "y", "是"}:
            return True
    text = str(row.get("title") or "").lower()
    return any(term in text for term in PACKAGING_ONLY_TERMS)


def _price_masked(row):
    """A masked price such as "¥ 2??" is not a price.

    Taobao search cards hide part of the digits with '?' when the price is a
    "starting at" teaser. Importing the leading digits (2.00 out of "2??")
    produces fake 1-9 yuan entries for products that may cost hundreds. Such
    records are filtered out at runtime, on top of the importer-side refusal.
    """
    raw_text = " ".join(
        str(row.get(key) or "")
        for key in ("raw_text", "price", "sales_text")
    )
    return bool(re.search(r"¥\s*[0-9]+[?？*＊]", raw_text)) or bool(
        re.search(r"[0-9]\s*[?？]{2,}", raw_text)
    )


def _price_quality(row, price):
    raw_text = " ".join(
        str(row.get(key) or "")
        for key in ("title", "raw_text", "sales_text")
    ).lower()
    entry_markers = (
        "首单",
        "新客",
        "补贴",
        "入口",
        "免单",
        "抽奖",
        "链接",
        "券后",
    )
    if price is not None and price <= 1:
        return "suspect_low_entry_price"
    if any(marker in raw_text for marker in entry_markers):
        return "possible_promotion_price"
    return "observed_search_card_price"


def _budget_distance(price, budget_min, budget_max):
    """How far a price sits from the middle of the requested range.

    Ordering ties by price put the cheapest items first, which made the budget
    range meaningless: raising the ceiling never changed what the user saw.
    Ranking by distance from the middle of the range keeps the list inside the
    range and still meaningful when the range is wide.
    """
    try:
        value = float(price)
    except (TypeError, ValueError):
        return 0.0
    if budget_min is not None and budget_max is not None and budget_max > 0:
        target = (budget_min + budget_max) / 2
    elif budget_max is not None and budget_max > 0:
        target = budget_max
    elif budget_min is not None and budget_min > 0:
        target = budget_min
    else:
        return 0.0
    return abs(value - target)


# SEO filler that Taobao sellers append to titles to win search ranking.
# Kept in order: the first matching split wins, so head words (送礼/礼物/礼品)
# are stripped before tail filler (insignia-style suffixes).
SEO_TITLE_FILLER = (
    "送爸爸的生日礼物",
    "送妈妈的生日礼物",
    "送女朋友的生日礼物",
    "送男朋友的生日礼物",
    "送女朋友礼物",
    "送男朋友礼物",
    "送长辈的礼物",
    "送老师的礼物",
    "生日礼物女生",
    "生日礼物男生",
    "生日礼物送女友",
    "生日礼物送男友",
    "生日礼物实用",
    "送礼长辈",
    "送长辈",
    "送女友",
    "送男友",
    "见面礼伴手礼",
    "伴手礼回礼",
    "节日送礼",
    "生日送礼",
    "送礼佳品",
    "礼品袋装",
)


def _display_title(raw_title):
    """Strip search-ranking filler from a Taobao title, keeping the head.

    Titles like 「碧螺春茶叶礼盒装送礼长辈绿茶送爸爸的生日礼物见家长老丈人
    礼品」 are keyword salads. The product identity lives in the first
    segment before the first filler phrase; cutting there yields
    「碧螺春茶叶礼盒装」. Falls back to the first 16 characters when no
    filler matches, so the display never exceeds a readable width.
    """
    title = str(raw_title or "").strip()
    if not title:
        return "未命名候选"
    best = None  # earliest filler position wins, not the filler order
    for filler in SEO_TITLE_FILLER:
        index = title.find(filler)
        if index >= 6 and (best is None or index < best):
            best = index
    if best is not None:
        return title[:best]
    # No filler found: keep a bounded head so single-keyword-salad titles
    # still read cleanly.
    # 截断时不要停在括号/书名号中间（「（纯银9」这种半截很难看）：
    # 往前找最后一个自然边界（开括号前、分隔符处），找不到才硬截。
    if len(title) <= 24:
        return title
    head = title[:24]
    cut = max(head.rfind("（"), head.rfind("("), head.rfind("，"), head.rfind(" "))
    if cut >= 8:
        return head[:cut].rstrip("·|｜-— ")
    return head


def _price_band(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return "价格待核实"
    if price <= 1:
        return "价格待核实"
    if price < 30:
        return "30 元以下档"
    if price < 100:
        return "30–100 元档"
    if price < 300:
        return "100–300 元档"
    if price < 500:
        return "300–500 元档"
    if price < 1000:
        return "500–1000 元档"
    return "1000 元以上档"


def _reference_price_range(value, price_quality=None):
    if price_quality == "suspect_low_entry_price":
        return "价格待核实"
    try:
        price = float(value)
    except (TypeError, ValueError):
        return "价格待核实"
    if not math.isfinite(price) or price <= 1:
        return "价格待核实"
    low = math.floor(price * 0.9)
    high = math.ceil(price * 1.1)
    return f"约 {low}–{high} 元"


def _strip_snapshot_warnings(text):
    """去掉数据源写死的"时间快照"免责句——页底提醒里已经讲了，卡片上不重复。"""
    value = str(text or "")
    parts = re.split(r"[；;。]|\s{2,}", value)
    kept = [
        part.strip()
        for part in parts
        if part.strip()
        and "时间快照" not in part
        and "价格、库存和详情页字段" not in part
        and "购买前需回到" not in part
    ]
    return "；".join(kept)


def _redact_exact_price_text(value):
    text = str(value or "")
    return re.sub(
        r"(?:[¥￥$]\s*|(?:价格|成本|售价|报价|成交价|price|cost)\s*(?:/成本)?\s*(?:为|是|约|：|:)?\s*)\d+(?:\.\d+)?\s*(?:元|块|人民币|CNY)?",
        "参考价位",
        text,
        flags=re.IGNORECASE,
    )


def _is_placeholder_text(value):
    """判一条文案是不是模板占位话术。

    不能只做子串命中：模型写「朋友生日送这套摆件，价格在预算内，造型也对
    她的口味」时整句是有信息的，只因为夹了「价格在预算内」就被删，等于把
    真理由当占位。所以先扣掉占位片段，再看剩下还有多少实质内容。
    """
    text = str(value or "").strip()
    if not text:
        return True
    placeholders = (
        "参考价位",
        "价格在预算内",
        "成本在预算内",
        "暂无特别提醒",
        "无特别提醒",
        "dify 未提供",
        "资料未提供",
        "候选通过当前硬约束检查",
    )
    lowered = text.lower()
    if lowered in placeholders:
        return True
    residue = lowered
    for marker in placeholders:
        residue = residue.replace(marker, "")
    # 扣掉占位片段后只剩标点/空白（<6 个字符）才算纯占位。
    residue = re.sub(r"[\s，。、；：！？,.;:!?~—…（）()\[\]【】\"'“”]+", "", residue)
    return len(residue) < 6


def _contains_exact_phrase(text, phrase):
    text = str(text or "")
    phrase = str(phrase or "")
    if not phrase:
        return False
    if len(phrase) == 1:
        return re.search(rf"(?<![\u3400-\u9fff]){re.escape(phrase)}(?![\u3400-\u9fff])", text) is not None
    return phrase in text


def _meaning_for_product(source):
    name = str(source.get("name") or source.get("title") or "")
    category = str(source.get("category") or "")
    text = f"{name} {category}"
    # 花卉先判："鲜花上海"这类地址串会被"海鲜"子串误命中（鲜花|上海 跨词切出"海鲜"），
    # 必须在海鲜/海产之前排除花植类商品。
    flower_keywords = ("鲜花", "花束", "玫瑰花", "康乃馨", "百合", "向日葵", "郁金香", "花植")
    if any(keyword in text for keyword in flower_keywords):
        return "鲜花类礼物表达祝福、纪念和审美；花材、颜色与收礼场合建议先核对（如探病忌香味浓烈的花）。"
    exact_meanings = (
        (("凤梨", "菠萝"), "凤梨或菠萝类食品更适合解释为分享、团聚和节日餐桌心意，不应拆解为“梨”的寓意。"),
        (("花胶",), "花胶属于食材类干货，寓意应以滋养、分享和节日礼赠为主，不应解释为花束或花材。"),
        (("火腿",), "火腿礼盒偏向地方特产、分享和节庆团聚，具体口味、规格和食用方式需要以商品详情为准。"),
        (("鲍鱼", "元贝"), "海产干货礼盒偏向分享、团聚和节日款待，需确认收礼人的饮食限制和过敏情况。"),
        (("海鲜",), "海产类礼物偏向分享、团聚和节日款待，需确认收礼人的饮食限制和过敏情况。"),
        (("水果", "果篮"), "水果类礼物通常传达分享、关照和祝福，具体寓意不应仅按单个水果名称拆解。"),
    )
    for keywords, meaning in exact_meanings:
        if any(keyword in text for keyword in keywords):
            return meaning
    return _gift_meaning(source)


def _local_reason_facts(source, intent, price_display):
    facts = []
    title = str(source.get("name") or source.get("title") or "").strip()
    category = str(source.get("category") or "").strip()
    user_text = " ".join(
        str(intent.get(key) or "")
        for key in (
            "occasion_label",
            "occasion_custom",
            "relation_label",
            "relation_custom",
            "free_text",
        )
    )
    occ = intent.get("occasion_label") or ""
    rel = intent.get("relation_label") or ""
    if occ and occ in title:
        facts.append(f"和{occ}的氛围很贴")
    elif occ and category and category in {"香氛", "护肤", "美妆"}:
        facts.append(f"{occ}送这类{category}礼，体面又不容易出错")
    elif occ:
        facts.append(f"拿来做{occ}礼物说得过去")
    if rel:
        relations = source.get("relations") or []
        if intent.get("recipient_relation") in relations:
            facts.append(f"资料里标了适合送给{rel}")
    # 类别只在与用户需求挂钩时才有信息量，单独一句"属于X类别"是废话，不写。
    tags = source.get("tags") or []
    if isinstance(tags, list):
        tag_labels = {
            "low_key": "相对低调",
            "portable": "便于携带",
            "practical": "偏日常实用",
            "personal": "带有个性化表达",
            "customizable": "可进行个性化调整",
        }
        for tag in tags:
            if tag in tag_labels:
                facts.append(tag_labels[tag])
    description = str(source.get("description") or "").strip()
    if description:
        facts.append(description)
    if user_text and any(term in user_text for term in ("低调", "实用", "健身", "咖啡", "茶")):
        facts.append("是否符合你的具体偏好仍需结合商品详情核对")
    return list(dict.fromkeys(facts))[:3]


def _fallback_reason(source, intent, price_display):
    """LLM 没给理由时的兜底句。措辞尽量像人说的话，不堆系统术语。"""
    facts = _local_reason_facts(source, intent, price_display)
    if not facts:
        return "这件商品的资料比较少，具体合不合适，建议看看商品详情和你自己的判断。"
    return "；".join(facts)


SOURCE_TEXT_REWRITES = (
    ("购买前需回到淘宝/天猫页面复核。", "下单前需自行复核。"),
    ("购买前需回到商品详情页复核。", "下单前需自行复核。"),
    ("请打开商品页确认实际规格和到手价。", "请确认实际规格和到手价。"),
    ("请以商家页面为准。", "请自行核实。"),
)


def _sanitize_source_text(text):
    """Strip platform names out of catalog-supplied copy.

    The catalog stores a fixed reminder that names the platform it came from.
    Product records are treated as plain data here, so any such wording is
    rewritten to a neutral form before it can reach the user.
    """
    if not text:
        return ""
    cleaned = str(text)
    for old, new in SOURCE_TEXT_REWRITES:
        cleaned = cleaned.replace(old, new)
    for word in ("淘宝", "天猫", "Taobao", "Tmall"):
        cleaned = cleaned.replace(word, "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _real_product_candidate(row):
    tags = []
    for field in ("derived_tags", "taboo_flags"):
        value = row.get(field)
        if isinstance(value, list):
            tags.extend(str(tag).strip() for tag in value if str(tag).strip())
        elif value:
            tags.extend(
                tag.strip()
                for tag in re.split(r"[,，;；|]", str(value))
                if tag.strip()
            )
    try:
        observed_price = float(row.get("price"))
    except (TypeError, ValueError):
        observed_price = None
    price_quality = _price_quality(row, observed_price)
    # 重点名（LLM 改写短标题）优先；没有就回退原标题
    _tkey = str(row.get("title_key") or "").strip()
    _traw = str(row.get("title") or "")
    _card_name = _tkey or _traw
    return {
        "id": str(row.get("item_id") or row.get("product_id") or row.get("offer_id") or ""),
        "product_id": str(row.get("item_id") or row.get("product_id") or row.get("offer_id") or ""),
        "name": _card_name,
        "title": _card_name,
        "title_raw": _traw,
        "gift_type": "purchased",
        "category": row.get("category") or "",
        "price": row.get("price"),
        "price_quality": price_quality,
        "currency": row.get("currency") or "",
        "delivery_days": None,
        "delivery_days_status": row.get("delivery_days_status") or "unknown",
        "tags": sorted(set(tags)),
        "description": row.get("description") or "",
        "risk_notes": _sanitize_source_text(row.get("risk_notes") or ""),
        "status": row.get("status") or "unknown",
        "availability": row.get("availability") or "unknown",
        "merchant": row.get("merchant") or "",
        "shipping_from": row.get("shipping_from") or "",
        "source": "",
        "source_url": row.get("source_url") or "",
        "observed_at": row.get("observed_at") or "",
        "data_confidence": row.get("data_confidence") or "",
    }


OCCASION_FAMILIES = {
    "birthday": ("生日", "生日礼物", "生日礼盒"),
    "graduation": ("毕业", "毕业礼物"),
    "thank_you": ("答谢", "谢礼"),
    "spring_festival": ("春节", "年货", "过年"),
    "mid_autumn": ("中秋",),
    "dragon_boat": ("端午", "粽子"),
    "double_ninth": ("重阳",),
    "valentines": ("情人节", "七夕"),
    "wedding": ("婚礼", "结婚", "新婚", "婚庆", "伴娘"),
    "housewarming": ("乔迁", "搬家", "入宅", "新居"),
    "opening": ("开业", "开张", "新店", "公司开业"),
    "anniversary": ("周年", "纪念日", "纪念礼物"),
    "onboarding": ("入职", "欢迎新员工", "新人入职"),
    "business": ("商务", "年会", "会议礼品"),
    "hospital_visit": ("探病", "病人"),
    "children_day": ("六一", "儿童节"),
}

# 2026-11-17：上面这组词是「商品明确绑定了哪个场合」的判据，不是「标题里
# 出现过什么字」。之前混进了一批泛用词，把 4180 件里 3193 件判成了场合冲突：
#   children_day 带「儿童/小学生/幼儿园/开学」→ 学生文具全灭
#   business 带「客户」→ 任何写了客户的商品全灭
#   thank_you 带「感谢/慰问」→ 写了感谢的商品全灭
#   opening 带「店铺」→ 提到店铺的全灭
#   mid_autumn 带「月饼」→ 月饼是品类不是场合（中秋请求仍然命中「中秋」）
# 典型误杀：写了「教师节」的中性笔、「中秋礼品」的护肤品、「送女友」的巧克力。
# 这些商品生日照样能送，砍掉只会让候选池从 51 缩到 20、凑不满卡数。
# 泛用词已移除，只留节日/场合专名。

# 母婴/婴幼儿用品：跟「场合」无关，是「收礼人对不上」。
# 从 children_day 词表里移除「儿童/小学生/开学」之后，这条得单独成立——
# 否则「满月礼盒」「百天宴」「新生儿衣服」会跟着生日请求一起进来。
# 实测漏网：babycare 辅食碗、eoodoo 婴儿礼盒 ×2。
_BABY_CARE_TERMS = (
    "婴儿", "宝宝", "新生儿", "新生宝宝", "母婴", "满月", "百天",
    "奶瓶", "奶嘴", "辅食", "尿不湿", "纸尿裤", "产妇", "月子",
    "孕妈", "孕妇", "婴儿衣服", "连体衣", "抓周",
    # 2026-11-17 补：男性朋友生日场景里混进「万圣节糖果盒0脂棒棒糖（儿童创意散装）」。
    # 这些词只用来判「这是给小孩的东西」，不参与场合判断——「儿童」曾经放在
    # children_day 场合词表里，会误杀 3193/4180 行，所以只能放在这条独立规则里。
    "儿童", "小学生", "幼儿园", "小孩", "小朋友", "少儿", "亲子",
)

# 玩具类：成年朋友生日送消防车、毛绒、盲盒不合适。
# 2026-11-17 实测漏网：「卡威消防车玩具套装」标题里没有母婴词，但目录
# category 字段是「母婴」——只看标题漏了。所以标题和 category 一起判。
_TOY_TERMS = (
    "玩具", "消防车", "警车", "救护车", "毛绒", "公仔", "盲盒", "积木",
    "拼图", "玩偶", "手办", "儿童车", "遥控车", "芭比", "乐高",
)
_TOY_CATEGORIES = ("母婴", "玩具", "童装")


def _is_baby_care(title, category="", payload=None):
    """婴幼儿/产妇/玩具类，成年收礼人场景下应当整体排除。

    category 参数是 2026-11-17 加的：消防车玩具套装标题干净，但目录把它
    归在「母婴」，只查标题拦不住。两个信源一起看。

    payload 也是 2026-11-17 加的：场合本身就是要送给小孩时（儿童节、满月、
    百日、抓周），这条规则必须让路——否则「儿童节给侄子挑玩具」会被滤成空池。

    2026-11-17 第二次修：让路让过头了。原来儿童节场景直接 return False，
    等于整条规则失效，结果给侄子过儿童节的池子里混进「贵州茅台镇酱香型白酒
    53度6瓶整箱」和「唐三彩莱摩智妍护肤套装（洗面奶+水乳+面霜+眼霜+精华）」。
    让路的本意是「允许玩具进来」，不是「允许白酒进来」。

    改法：儿童节场景下，母婴/玩具类照旧放行（那才是给小孩的），但成人专属
    品类单独拦一道。
    """
    payload = payload if isinstance(payload, dict) else {}
    child_occasions = {
        "children_day", "满月", "百天", "百日", "抓周", "周岁", "六一", "儿童节",
    }
    occasion = " ".join(
        str(payload.get(key) or "")
        for key in ("occasion", "occasion_label", "occasion_custom")
    )
    text = str(title or "").lower()
    category_text = str(category or "").strip()

    if any(word in occasion for word in child_occasions):
        # 送给小孩的场合：玩具/母婴正是要的，但成人专属品类必须挡掉。
        return _is_adult_only(text, category_text)

    if any(term in text for term in _BABY_CARE_TERMS):
        return True
    if any(term in text for term in _TOY_TERMS):
        return True
    return category_text in _TOY_CATEGORIES


# 儿童场合下仍然不该出现的成人专属品类。酒、烟、护肤抗老、情趣、保健品，
# 都不属于「送给小孩」的东西——即便场合是儿童节。
_ADULT_ONLY_TERMS = (
    "白酒", "酱香", "浓香", "清香型", "茅台", "五粮液", "郎酒", "泸州老窖",
    "汾酒", "洋河", "剑南春", "红酒", "葡萄酒", "威士忌", "白兰地", "啤酒",
    "香烟", "雪茄", "电子烟", "打火机",
    "抗老", "抗皱", "紧致", "精华", "眼霜", "面霜", "水乳", "面膜", "爽肤水",
    "香水", "香氛", "口红", "粉底", "彩妆", "美妆",
    "壮阳", "补肾", "玛咖", "鹿茸", "伟哥",
    "剃须刀", "须后水", "文胸", "内衣",
)


def _is_adult_only(text, category=""):
    """成人专属品类判定。儿童场合用它挡掉白酒、抗老护肤之类。"""
    combined = f"{text} {category}".lower()
    return any(term in combined for term in _ADULT_ONLY_TERMS)


# 「六一/儿童节」和「生日」互斥：儿童节商品的标题常带"…生日惊喜"这类 SEO
# 尾缀（如"六一礼物送小学生…艺术生日惊喜"），会让标题被误判成生日相关。
# 请求不含儿童节语义时，标题命中 children_day 即视为场合冲突。
_MUTUALLY_EXCLUSIVE_FAMILIES = {
    ("children_day", "birthday"),
}


def _occasion_family_conflicts(detected, requested_keys):
    conflicts = [key for key in detected if key not in requested_keys]
    # 互斥家族：即使请求里同时"命中"了对方的关键词（SEO 噪声），也算冲突。
    for a, b in _MUTUALLY_EXCLUSIVE_FAMILIES:
        if a in detected and b in requested_keys and a not in requested_keys:
            if a not in conflicts:
                conflicts.append(a)
        if b in detected and a in requested_keys and b not in requested_keys:
            if b not in conflicts:
                conflicts.append(b)
    return conflicts


def _occasion_family_hits(text):
    text = str(text or "").lower()
    return {
        family
        for family, terms in OCCASION_FAMILIES.items()
        if any(term in text for term in terms)
    }


# 品牌表。用来给 _select_diverse_rows 的品牌去重（第一遍每品牌 ≤2 件）。
#
# 2026-11-17：原来只有 11 个零食品牌，实测目录里 4057/4180 行（97.1%）落进
# 「其他品牌」这一个桶，品牌去重等于不存在。直接后果：三台 Nespresso 胶囊机
# 一起进前 8，用户看到「换汤不换药」的三张卡。
#
# 下面这些不是凭印象列的，是按目录实测计数补的（括号里是命中行数）：
# 苹果 52、小熊 29、小米 25、富士 19、徕芬 14、美的 10、五粮液 10、九阳 10、
# 欧莱雅 9、华为 7、膳魔师 7、苏泊尔 5、燕之屋 5、奈斯派索 4、八马 4、
# 东阿 4、飞利浦 3、荣耀 3、泸州老窖 3、雅诗兰黛 2、郎酒 2、茅台 2、
# 罗技 2、小仙炖 2、剑南春 2、vivo 2、樱桃 2、西泠印社 2、洋河 1、
# 富光 1、兰蔻 1、安伯尼克 1、飞科 1、麦瑞克 1。
#
# 英文名同时保留（Cherry/Nespresso/Apple/Anbernic），因为标题两种写法都有。
PRODUCT_BRANDS = (
    # 零食（原有）
    "三只松鼠", "百草味", "良品铺子", "沃隆", "洽洽", "来伊份",
    "盐津铺子", "盼盼", "徐福记", "华美", "稻香村",
    # 数码 / 家电
    "苹果", "apple", "小米", "华为", "荣耀", "三星", "vivo", "oppo",
    "富士", "佳能", "索尼", "罗技", "樱桃", "cherry", "安伯尼克", "anbernic",
    "飞利浦", "美的", "苏泊尔", "九阳", "格兰仕", "松下", "西门子",
    "小熊", "摩飞", "北鼎", "鸣盏", "戴森", "徕芬", "飞科", "麦瑞克",
    "膳魔师", "虎牌", "象印", "富光",
    # 电子书阅读器。2026-11-17 加：AI 精选里出现「阅星曈X3」和「阅星曈X4」
    # 两台同款不同代，因为 18 件阅读器全归在「通用|数码」这一个 family，
    # 品牌又全算「其他品牌」，两道去重都拦不住。
    "掌阅", "ireader", "汉王", "kindle", "亚马逊", "阅星曈", "xteink",
    "乐哨", "炳捷", "忆知音",
    # 咖啡
    "奈斯派索", "nespresso", "德龙", "delonghi", "柏翠", "格米莱",
    # 美妆
    "欧莱雅", "兰蔻", "雅诗兰黛", "玉兰油", "olay", "高夫", "欧珑",
    "安娜苏", "纳西索", "ahc",
    # 酒水 / 滋补 / 茶
    "茅台", "五粮液", "郎酒", "泸州老窖", "汾酒", "洋河", "剑南春", "洋酒",
    "同仁堂", "东阿", "燕之屋", "小仙炖", "八马", "武夷星", "大益",
    # 文创
    "西泠印社",
)

# 同一个品牌的中英文写法要归一到同一个名字，否则「奈斯派索」和「nespresso」
# 会被当成两个品牌，品牌去重照样漏。键是表里的写法，值是归一后的标准名。
BRAND_ALIASES = {
    "apple": "苹果",
    "cherry": "樱桃",
    "nespresso": "奈斯派索",
    "anbernic": "安伯尼克",
    "delonghi": "德龙",
    "olay": "玉兰油",
    "ahc": "AHC",
}

PRODUCT_FAMILIES = (
    ("坚果", ("每日坚果", "混合坚果", "坚果", "干果", "果仁", "炒货")),
    ("面包", ("面包", "吐司", "糕点", "蛋糕")),
    ("月饼", ("月饼",)),
    ("茶礼盒", ("茶叶", "茶礼", "茶包")),
    ("咖啡礼盒", ("咖啡",)),
    ("水果礼盒", ("水果", "果篮")),
    ("巧克力礼盒", ("巧克力",)),
    ("酒类礼盒", ("果酒", "红酒", "葡萄酒", "白酒", "啤酒")),
    ("干货海产礼盒", ("花胶", "鲍鱼", "元贝", "海产", "海鲜", "羊肚菌")),
    ("保温杯礼盒", ("保温杯", "咖啡杯", "钛杯")),
    ("香薰礼盒", ("香薰", "香氛", "蜡烛", "扩香")),
    ("鲜花礼盒", ("鲜花", "花束", "花篮", "永生花", "干花")),
    ("绿植礼盒", ("发财树", "绿植", "盆栽", "植物")),
)


def _product_family_key(row):
    if row.get("product_family_id"):
        return str(row["product_family_id"])
    if row.get("product_family_key"):
        return str(row["product_family_key"])
    text = " ".join(
        str(row.get(field) or "")
        for field in ("title", "category", "collection_query")
    ).lower()
    brand = next(
        (brand for brand in PRODUCT_BRANDS if _brand_in_text(brand, text)),
        "通用",
    )
    brand = BRAND_ALIASES.get(brand, brand)
    family = next(
        (family for family, terms in PRODUCT_FAMILIES if any(term in text for term in terms)),
        str(row.get("category") or "其他").strip() or "其他",
    )
    return f"{brand}|{family}"


def _title_occasion_conflict(title, payload, collection_query="", occasion_tags=""):
    """判断商品是否明确绑定在别的送礼场合上。

    2026-11-17：分两个信源，可信度不同，不能一视同仁。

    标题是商品自己说的话——「满月礼盒」「周岁抓周」「伴娘裙」写明了就是那个
    场合的货，拿它判冲突是对的。

    目录标签（collection_query / occasion_tags）是采集侧给的 SEO 归类，只说明
    「谁会来买」，不说明「这货只能送那个场合」。实测代价：500–1000 元档 197 件
    里被砍掉 143 件，其中包含——
      · CHERRY 机械键盘（用户明说想换键盘，整个价格带就这一把）
      · Nespresso 咖啡机 ×3（用户明说常喝咖啡）
      · 空气炸锅、徕芬吹风机、富士拍立得、Kindle、vivo 手表
    这些标签写着「婚礼/乔迁/毕业」，但咖啡机当生日礼完全成立。所以标签只用来
    判「收礼人是否对得上」这一类硬错配（母婴/儿童用品送给成年朋友），不参与
    节日场合的冲突判定。
    """
    title = str(title or "").lower()
    collection_query = str(collection_query or "").lower()
    occasion_tags = str(occasion_tags or "").lower()
    if not title and not collection_query and not occasion_tags:
        return None
    occasion = str(payload.get("occasion") or "").lower()
    occasion_label = str(payload.get("occasion_label") or "").lower()
    custom = str(payload.get("occasion_custom") or "").lower()
    requested_text = f"{occasion} {occasion_label} {custom}"
    requested_keys = _occasion_family_hits(requested_text)
    requested_keys.add(occasion)

    # 只有标题参与场合冲突判定。
    detected = _occasion_family_hits(title)
    if not detected:
        return None
    conflicts = _occasion_family_conflicts(detected, requested_keys)
    if not conflicts:
        return None
    labels = {
        "spring_festival": "春节/过年", "mid_autumn": "中秋",
        "dragon_boat": "端午", "double_ninth": "重阳",
        "valentines": "情人节/七夕", "wedding": "婚礼/新婚",
        "housewarming": "乔迁", "opening": "开业/开张",
        "anniversary": "周年/纪念日", "onboarding": "入职",
        "business": "商务/年会", "hospital_visit": "探病/慰问",
        "birthday": "生日", "graduation": "毕业", "thank_you": "答谢/感谢",
        "children_day": "六一/儿童节",
    }
    return "商品标题明确绑定" + "、".join(labels[key] for key in conflicts) + "场景"


def _gender_score(row, payload):
    """按收礼人偏向给商品打分。用户没选就返回 0，不做任何性别推断。

    商品库 4180 件里没有任何性别字段，只能靠标题词判断。这里用的两组词是
    从实际标题里挑的高频词，不是通用性别刻板印象清单——目的是让「选了偏男性」
    时保温杯/茶/咖啡/键盘这类排前面、香水/护肤/首饰排后面，反过来同理。

    打分幅度（±6）刻意和关系匹配（+5/-4）同级：性别是用户明确说的，
    但它是软偏好，不该压过预算和硬性避开项（那些在更早的地方直接过滤）。
    """
    gender = str(payload.get("recipient_gender") or "").strip().lower()
    if gender not in {"male", "female"}:
        return 0
    title = " ".join(
        str(row.get(key) or "") for key in ("title", "title_key", "category")
    ).lower()
    if not title.strip():
        return 0

    male_words = (
        "男士", "男款", "男友", "男朋友", "爸爸", "父亲", "送男", "商务",
        "剃须", "刀头", "打火机", "白酒", "威士忌", "红酒", "洋酒", "黄酒",
        "机械键盘", "键盘", "鼠标", "耳机", "音箱", "音响", "充电宝",
        "手电", "工具", "户外", "钓鱼", "鱼竿", "运动", "健身", "球",
        "模型", "手办", "航模", "无人机", "手表", "腕表", "皮带", "腰带",
        "领带", "钱包", "钢笔", "茶具", "紫砂", "咖啡", "车载", "保温杯",
        "焖烧", "数码", "相机", "眼镜", "太阳镜", "酒",
    )
    female_words = (
        "女士", "女款", "女友", "女朋友", "女生", "妈妈", "母亲", "送女",
        "香水", "香氛", "香薰", "护手霜", "护肤", "水乳", "精华", "面膜",
        "口红", "唇釉", "眼影", "粉底", "化妆", "美甲", "身体乳",
        "项链", "吊坠", "耳环", "耳钉", "手链", "戒指", "锁骨链", "毛衣链",
        "发夹", "头饰", "发圈", "丝巾", "围巾",
        "花束", "鲜花", "玫瑰", "永生花", "干花",
        "玩偶", "毛绒", "盲盒", "手账", "贴纸", "小夜灯",
        "可爱", "萌", "少女", "精致",
    )
    male_hit = sum(1 for word in male_words if word in title)
    female_hit = sum(1 for word in female_words if word in title)
    if gender == "male":
        return (male_hit - female_hit) * 3
    return (female_hit - male_hit) * 3


# 硬性性别排除词表：只收「标题里写了就等于说明收礼人性别」的品类，
# 刻意不含「护肤/美妆」这类中性词（男士护肤也是护肤），靠 title 里的
# 男士/女士标记放行。
_MALE_FORBIDDEN = (
    "香水", "香氛", "口红", "唇釉", "唇膏", "面膜", "水乳", "精华液",
    "面霜", "美妆", "彩妆", "粉底", "眼影", "腮红", "睫毛膏", "指甲油",
    "卫生巾", "文胸", "丝巾", "耳环", "耳钉", "耳坠", "项链", "手链",
    "发夹", "头饰", "睡衣", "家居服", "珍珠", "蝴蝶结", "蕾丝",
)
_FEMALE_FORBIDDEN = (
    "剃须刀", "刀头", "须后水", "打火机", "雪茄", "鱼竿", "钓竿", "钓具",
    "烟具", "领带", "袖扣",
)


def _row_text(row):
    """商品的可判定文本：原始 title + 展示用 title_key，两者都要看。

    2026-11-17：目录里 4177/4180 行的 `title` 和 `title_key` 不一样。`title` 是
    抓取原文，塞满「送女友/万圣节/儿童零食/测评天花板」这类营销词；`title_key`
    是清洗后的商品名，**卡片上显示的是它**。实测「纳西索纯粹迷情礼盒测评伪体香
    天花板」的 title 没有「香水」二字，而展示名是「纳西索纯粹迷情礼盒（伪体香
    香水装）」——只查 title 就会把一件香水放进男性收礼人的页面。

    两个字段各有用途，所以合并判断：title_key 是用户真正看到的（漏掉它等于放过
    用户看得见的错），title 带原始关键词（漏掉它等于放过商家自己标的品类）。
    """
    row = row if isinstance(row, dict) else {}
    parts = [
        str(row.get("title_key") or "").strip(),
        str(row.get("title") or "").strip(),
        str(row.get("category") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def _gender_mismatch(row, payload):
    """收礼人性别明确时，判断商品是不是明显给另一个性别的。

    2026-11-17：`_gender_score` 只是**排序**用的软偏好（±6），不删除任何商品。
    所以 8 件 AI 精选遵守了「男性收礼人不要香水」这条提示词规则，而 22 件
    「更多选项」是按本地名次顺取的，完全没过这道关——实测男性收礼人的页面上
    排着香水两件、面膜两件、美妆工具一件。用户往下翻就看到明显不该出现的礼物。

    这里加的是**硬性排除**，只认标题里明说的品类词，不用 `_gender_score`
    那套含「商务/精致/可爱」的模糊词表——那套用来排序可以，用来删商品会误伤。

    标题里带相反性别标记时一律放行（「高夫男士护肤礼盒」是给男性的，不能因为
    含「护肤」就砍掉）。性别未填时不做任何判断。
    """
    gender = str(payload.get("recipient_gender") or "").strip().lower()
    if gender not in {"male", "female"}:
        return None
    title = _row_text(row)

    male_marked = ("男士", "男款", "男式", "男友", "爸爸", "父亲", "送老公", "男朋友")
    female_marked = ("女士", "女款", "女式", "女友", "妈妈", "母亲", "送老婆", "女朋友")

    if gender == "male":
        if any(m in title for m in male_marked):
            return None
        for word in _MALE_FORBIDDEN:
            if word in title:
                return word
        return None

    if any(m in title for m in female_marked):
        return None
    for word in _FEMALE_FORBIDDEN:
        if word in title:
            return word
    return None


def _avoid_terms(payload):
    """把 avoid_custom 拆成能匹配商品标题的词。

    2026-11-17：这个字段以前只被拼进顾问提问（`_build_advisor_question`），
    从未参与商品过滤。实测：用户写「不要香水 茶叶 白酒」，候选池前后
    **完全一致**，9 件被明确排除的商品照旧在池子里，进了 Dify 还排到前排。
    用户已经说出口的排除意愿，必须在候选池这一层就生效。

    拆词复用 _text_terms（含 2–4 字滑动窗口），这样「不要香水」里的「香水」
    能被抓出来。同时去掉否定词本身，免得「不要」去匹配标题。
    """
    payload = payload if isinstance(payload, dict) else {}
    raw = str(payload.get("avoid_custom") or "").strip()
    if not raw:
        return []
    drop = {"不要", "不用", "别送", "不送", "避免", "讨厌", "不喜欢", "不能",
            "希望", "可以", "一个", "东西", "礼物", "礼品"}
    return [t for t in _text_terms(raw) if t not in drop and len(t) >= 2]


def _avoided_by_user(row, avoid_terms):
    """商品命中用户明确排除的词时返回该词。"""
    if not avoid_terms:
        return None
    text = _row_text(row).lower()
    for term in avoid_terms:
        if term in text:
            return term
    return None


def _diy_within_budget(payload, budget_min=None, budget_max=None):
    """Return the DIY gifts whose cost can satisfy the requested budget.

    2026-11-17：之前 DIY_GIFTS 是无条件整段塞进候选池的，完全不看预算。
    结果用户要 500–1000 元的生日礼，前 8 名里 6 个是 22–97 元的手写卡片盒、
    照片书、茶包——性别规则一修好，这堆廉价自制礼物就暴露成了主问题。
    自制礼物的 cost 全在 25–88 元，够不到几百元的档，所以高预算时应当整体让位。

    判定口径：预算下限高于自制礼物成本时才算「不达标」；只设上限或预算低
    （比如 50–100 元）时保留——那种场景下手作恰好是正解，不能一刀切砍掉。
    参数缺省时从 payload 里取，供两处候选池复用。
    """
    payload = payload if isinstance(payload, dict) else {}
    if budget_min is None:
        try:
            raw = payload.get("budget_min")
            budget_min = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            budget_min = None
    if budget_max is None:
        try:
            raw = payload.get("budget_max")
            budget_max = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            budget_max = None
    if budget_min is None or budget_min <= 0:
        kept = list(DIY_GIFTS)
    else:
        kept = []
        for item in DIY_GIFTS:
            try:
                cost = float(item.get("cost", item.get("price", 0)) or 0)
            except (TypeError, ValueError):
                cost = 0.0
            # 成本低于下限时，手作还能靠加料/换材料往上够；只砍够不着的。
            if cost and cost < budget_min * 0.5:
                continue
            kept.append(item)
    # 用户明确排除的品类，手作里也要一并排掉（「不要香水」→ 不做香薰蜡烛）。
    avoid_terms = _avoid_terms(payload)
    if avoid_terms:
        kept = [item for item in kept if not _avoided_by_user(item, avoid_terms)]
    return kept


def get_dify_candidates(payload, limit=DIFY_CANDIDATE_LIMIT):
    """Build a bounded, fact-preserving candidate set for Dify's rule node."""
    payload = payload if isinstance(payload, dict) else {}
    gift_mode = payload.get("gift_mode") or "all"
    if gift_mode not in {"all", "purchased", "diy"}:
        logging.warning(
            "unknown gift_mode %r, treating as 'all' (caller: %s)",
            gift_mode,
            payload.get("occasion_label") or "unknown",
        )
        gift_mode = "all"
    candidates = []

    if gift_mode in {"all", "purchased"}:
        if gift_mode == "all":
            # 2026-11-17：自制礼物过不了预算时，空出的名额要让给购买礼物，
            # 否则候选池会从 36 掉到 20（500–1000 元场景实测），
            # 用户要的 30 张卡就凑不出来。
            diy_pool = _diy_within_budget(payload)
            diy_limit = min(max(0, limit - max(1, limit // 2)), len(diy_pool))
            purchased_limit = max(1, limit - diy_limit)
        else:
            purchased_limit = limit
            diy_limit = 0
            diy_pool = []
        request_text = " ".join(
            str(payload.get(key) or "")
            for key in ("occasion_label", "occasion_custom", "relation_label", "relation_custom", "free_text")
        )
        terms = _text_terms(request_text)
        avoid_terms = _avoid_terms(payload)
        budget_max = payload.get("budget_max")
        budget_min = payload.get("budget_min")
        try:
            budget_max = float(budget_max) if budget_max not in (None, "") else None
        except (TypeError, ValueError):
            budget_max = None
        try:
            budget_min = float(budget_min) if budget_min not in (None, "") else None
        except (TypeError, ValueError):
            budget_min = None

        ranked = []
        family_seen = {}
        for row in _load_real_catalog():
            if str(row.get("currency") or "").upper() != "CNY":
                continue
            if not row.get("title"):
                continue
            if _is_packaging_only(row):
                continue
            if _is_baby_care(_row_text(row), row.get("category"), payload):
                continue
            if _avoided_by_user(row, avoid_terms):
                continue
            if _gender_mismatch(row, payload):
                continue
            occasion_conflict = _title_occasion_conflict(
                row.get("title"), payload, row.get("collection_query"),
                row.get("occasion_tags"),
            )
            if occasion_conflict:
                continue
            try:
                price = float(row.get("price"))
            except (TypeError, ValueError):
                continue
            quality = _price_quality(row, price)
            if quality == "suspect_low_entry_price":
                continue
            if _price_masked(row):
                continue
            if budget_max is not None and budget_max > 0 and price > budget_max:
                continue
            if budget_min is not None and budget_min > 0 and price < budget_min:
                continue
            family_key = _product_family_key(row)
            title_text = " ".join(
                str(row.get(key) or "")
                for key in ("title", "category", "collection_query")
            ).lower()
            score = sum(2 for term in terms if term in title_text)
            if payload.get("occasion_label") and str(payload["occasion_label"]).lower() in title_text:
                score += 3
            # 关系匹配（与 get_preview_candidates 同款）：命中关系加 5 分，
            # 非空零交集扣 4 分——防止多样性兜底把毫无关系的品类捞进池子。
            relation_tags = str(row.get("relation_tags") or "")
            if relation_tags:
                relation_text = " ".join(
                    str(payload.get(key) or "")
                    for key in ("recipient_relation", "relation_label", "relation_custom")
                )
                if relation_text:
                    tags = [
                        t.strip()
                        for t in re.split(r"[/、,，]", relation_tags)
                        if t.strip()
                    ]
                    if any(tag in relation_text for tag in tags):
                        score += 5
                    else:
                        score -= 4
            # 收礼人偏向加权。用户选了才生效；没选（空）完全不加不减，
            # 保持原来「不做性别推断」的行为。
            #
            # 为什么在本地打分而不是只靠 Dify：Dify 的 LLM 2 只能从
            # eligible_products 里挑，而这个 24 件候选集就是这里产出的。
            # 只在提示词里写「带受众词」，商品根本进不了候选集，等于没改。
            score += _gender_score(row, payload)
            in_budget_range = (
                budget_min is None
                or budget_min <= price
            )
            quality_rank = 0 if quality == "observed_search_card_price" else 1
            range_rank = 0 if in_budget_range else 1
            target_price = (
                (budget_min + budget_max) / 2
                if budget_min is not None and budget_max is not None
                else budget_min
            )
            price_distance = (
                abs(price - target_price)
                if target_price is not None
                else price
            )
            ranked.append(
                (
                    score,
                    quality_rank,
                    range_rank,
                    price_distance,
                    price,
                    str(row.get("item_id") or row.get("product_id") or ""),
                    row,
                )
            )

        # Highest score first, then observed prices, then closest to the middle
        # of the requested range, then cheapest as a stable tiebreak.
        ranked.sort(key=lambda item: (-item[0], item[1:-1]))
        selected_rows = _select_diverse_rows(ranked, purchased_limit)
        for row in selected_rows:
            family_key = _product_family_key(row)
            family_seen[family_key] = row
            candidate = _real_product_candidate(row)
            if row.get("product_family_id"):
                candidate["product_family_key"] = row["product_family_id"]
            if row.get("product_family_label"):
                candidate["product_family_label"] = row["product_family_label"]
            candidate["product_family_key"] = family_key
            candidate["product_family_label"] = family_key.replace("|", " ")
            candidate["product_family_count"] = sum(
                1 for ranked_item in ranked
                if _product_family_key(ranked_item[-1]) == family_key
            )
            candidates.append(candidate)
        if gift_mode == "all":
            candidates.extend(diy_pool[:diy_limit])

    elif gift_mode == "diy":
        # 用户明确只要手作时，预算过滤不能把池子清空。
        # 2026-11-17：_diy_within_budget 会把成本够不到预算下限的手作滤掉，
        # 但手作成本全在 25–88 元——用户选「只要自制」又给 500–1000 预算时，
        # 池子直接变成 0，前端显示「AI 这次没选出」，看着像模型失败，其实是
        # 本地筛空的。手作只有这一批，给不出比给空好，所以回退到全部手作，
        # 成本如实写在卡上，让用户自己判断要不要加料往上够。
        diy_kept = _diy_within_budget(payload)
        if not diy_kept:
            # 预算兜底：只回退到「没被用户排除的」手作，不能把用户明说
            # 不要的品类又加回来。
            _avoid = _avoid_terms(payload)
            diy_kept = [it for it in DIY_GIFTS
                        if not _avoided_by_user(it, _avoid)]
        candidates.extend(diy_kept[:limit])

    return candidates[:limit]


def get_preview_candidates(payload, limit=PREVIEW_CANDIDATE_LIMIT):
    """Build the larger local display pool without enlarging Dify's input."""
    payload = payload if isinstance(payload, dict) else {}
    if (payload.get("gift_mode") or "all") == "diy":
        # 同上：明确只要手作时不让预算过滤把池子清空。
        diy_kept = _diy_within_budget(payload)
        if not diy_kept:
            _avoid = _avoid_terms(payload)
            diy_kept = [it for it in DIY_GIFTS
                        if not _avoided_by_user(it, _avoid)]
        return diy_kept[:limit]

    request_text = " ".join(
        str(payload.get(key) or "")
        for key in ("occasion_label", "occasion_custom", "relation_label", "relation_custom", "free_text")
    )
    terms = _text_terms(request_text)
    avoid_terms = _avoid_terms(payload)
    try:
        budget_min = float(payload.get("budget_min")) if payload.get("budget_min") not in (None, "") else None
    except (TypeError, ValueError):
        budget_min = None
    try:
        budget_max = float(payload.get("budget_max")) if payload.get("budget_max") not in (None, "") else None
    except (TypeError, ValueError):
        budget_max = None

    ranked = []
    for row in _load_real_catalog():
        if str(row.get("currency") or "").upper() != "CNY":
            continue
        if not row.get("title"):
            continue
        if _is_packaging_only(row):
            continue
        if _is_baby_care(_row_text(row), row.get("category"), payload):
            continue
        if _avoided_by_user(row, avoid_terms):
            continue
        if _gender_mismatch(row, payload):
            continue
        if _title_occasion_conflict(row.get("title"), payload, row.get("collection_query"), row.get("occasion_tags")):
            continue
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if _price_quality(row, price) == "suspect_low_entry_price":
            continue
        if _price_masked(row):
            continue
        if budget_min is not None and budget_min > 0 and price < budget_min:
            continue
        if budget_max is not None and budget_max > 0 and price > budget_max:
            continue
        text = " ".join(
            str(row.get(key) or "")
            for key in ("title", "category", "collection_query")
        ).lower()
        score = sum(2 for term in terms if term in text)
        if payload.get("occasion_label") and str(payload["occasion_label"]).lower() in text:
            score += 3
        # 关系匹配：目录里 3034 条带 relation_tags（如「导师/朋友/同事」），
        # 以前这个字段从没参与打分——毕业送老师时 beeshum 宝宝水杯和老师
        # 送礼毫无关系，却因为多样性兜底混进候选。命中关系加 5 分；**反向
        # 也一样重要**：relation_tags 非空但与请求关系零交集时扣 4 分，
        # 把「父母/子女」的宝宝用品排到多样性兜底捞不到的位置。
        relation_tags = str(row.get("relation_tags") or "")
        if relation_tags:
            relation_text = " ".join(
                str(payload.get(key) or "")
                for key in ("recipient_relation", "relation_label", "relation_custom")
            )
            if relation_text:
                tags = [
                    t.strip()
                    for t in re.split(r"[/、,，]", relation_tags)
                    if t.strip()
                ]
                if any(tag in relation_text for tag in tags):
                    score += 5
                else:
                    score -= 4
        # 收礼人偏向加权，与 get_dify_candidates 同一套规则：用户没选返回 0。
        # 展示池也必须加，否则 Dify 按性别选出的候选会被这里重新排回去。
        score += _gender_score(row, payload)
        ranked.append((score, price, str(row.get("item_id") or ""), row))

    ranked.sort(key=lambda item: (-item[0], _budget_distance(item[1], budget_min, budget_max), item[1], item[2]))
    purchased_target = limit
    diy_target = 0
    diy_pool = []
    if (payload.get("gift_mode") or "all") == "all":
        # 同 get_dify_candidates：自制礼物被预算淘汰时空位补给购买礼物，
        # 否则展示池会缩水，凑不满用户要的卡数。
        diy_pool = _diy_within_budget(payload, budget_min, budget_max)
        diy_target = min(max(0, limit - max(1, int(round(limit * 0.7)))), len(diy_pool))
        purchased_target = max(1, limit - diy_target)
    selected_rows = _select_diverse_rows(ranked, purchased_target)
    candidates = []
    for row in selected_rows:
        family_key = _product_family_key(row)
        candidate = _real_product_candidate(row)
        candidate["product_family_key"] = family_key
        candidate["product_family_label"] = row.get("product_family_label") or family_key
        candidates.append(candidate)
    if diy_target:
        candidates.extend(diy_pool[:diy_target])
    return candidates


def _compact_dify_candidate(product):
    """Keep only fields needed by the Dify rule and ranker nodes.

    2026-11-16 瘦身：实测候选池 36 条原本 15441 字符，而 LLM 2 的思考量
    主要由「要评估多少候选、每条带多少字段」决定（把当选数从 8 砍到 4，
    completion_tokens 只从 7694 降到 5966 —— 说明成本在输入池不在输出）。
    所以砍字段是真正无损的提速：

    - `id` / `name` 与 `product_id` / `title` 完全重复，留一对就够；
    - `currency` 恒为 CNY、`delivery_days` 恒为 None、`delivery_days_status`
      只表示采集状态，三者对排序没有信息量；
    - `status` / `product_family_count` 同上。

    安全前提：merge_dify_with_preview 会把 title/price/source_url/delivery_days
    等展示字段全部从本地 preview 覆盖回来，Dify 返回里不需要带这些。
    """
    required_fields = (
        "product_id",
        "title",
        "gift_type",
        "category",
        "price",
        "currency",
        "delivery_days",
        "status",
        "tags",
        "description",
    )
    optional_fields = (
        "collection_query",
        "product_family_label",
        "cost",
        "prep_days",
        "risk_notes",
        "safety_notes",
        "allergens",
    )
    compact = {field: product.get(field) for field in required_fields}
    # Catalog DIY entries use id/name; Dify rules and ranker require
    # product_id/title for every candidate. Keep the identity when compacting.
    compact["product_id"] = product.get("product_id") or product.get("id") or product.get("diy_id")
    compact["title"] = product.get("title") or product.get("name")
    compact.update(
        {
            field: product.get(field)
            for field in optional_fields
            if product.get(field) not in (None, "", [])
        }
    )
    if product.get("gift_type") == "diy":
        for field in ("materials", "steps", "preservation", "difficulty"):
            if product.get(field) not in (None, "", []):
                compact[field] = product[field]
    return compact


def build_dify_request(payload):
    candidates = get_dify_candidates(payload)
    # 收礼人偏向要传到 LLM，但 Dify 的 start 节点只声明了 gift_request 和
    # candidate_products 两个变量，多传一个未声明字段会被拒绝。所以不新增
    # 变量，而是把这句话拼进 gift_request 文本里——它本来就是「一段文本」，
    # 模型读到末尾这行就知道该怎么处理受众词。
    #
    # 为什么单独写一句而不是只靠 JSON 里的 recipient_gender 字段：gift_request
    # 是一大坨 JSON，模型在里头容易漏掉单个字段；显式点名一次更稳。
    # 用户没选就明确写「未指定」并禁止推断，提示词里那条
    # 「不要根据性别推断兴趣」照旧生效。
    gender_raw = str(payload.get("recipient_gender") or "").strip().lower()
    gender_hint = {
        "male": "收礼人偏向=偏男性（用户自己选的，请在检索词里带上这类人常用的品类词）",
        "female": "收礼人偏向=偏女性（用户自己选的，请在检索词里带上这类人常用的品类词）",
    }.get(gender_raw, "收礼人偏向=未指定（不要自行推断性别）")
    return {
        "inputs": {
            "gift_request": json.dumps(payload, ensure_ascii=False) + "\n" + gender_hint,
            "candidate_products": json.dumps(
                [_compact_dify_candidate(item) for item in candidates],
                ensure_ascii=False,
            ),
        },
        "response_mode": DIFY_RESPONSE_MODE,
        "user": "gift-right-demo",
    }


def _looks_like_placeholder(value):
    """判断这个值是不是 deploy.sh 写进去的占位文字。

    2026-11-18：deploy.sh 把 `.env` 模板写成「在这里填你的推荐工作流密钥」，
    而 dify_is_configured() 只检查非空，于是健康检查一路报 configured: true，
    真正发请求时才在 Authorization 头里撞 latin-1 编码错误，
    错误信息是 "'latin-1' codec can't encode characters in position 7-19"，
    完全看不出根因是「密钥没填」。这里把占位值识别出来，启动时就报清楚。
    """
    if not value:
        return False
    if not all(ord(ch) < 128 for ch in value):
        return True  # 密钥一定是 ASCII，带非 ASCII 就是中文占位文字
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in ("在这里填", "改成你", "your-", "placeholder", "changeme")
    )


def dify_is_configured():
    return bool(
        _clean_dify_base_url()
        and os.getenv("DIFY_API_KEY", "").strip()
        and not _looks_like_placeholder(os.getenv("DIFY_API_KEY", "").strip())
    )


def dify_config_problem():
    """返回配置问题的人话说明，没问题时返回 None。"""
    base = _clean_dify_base_url()
    key = os.getenv("DIFY_API_KEY", "").strip()
    chat = os.getenv("DIFY_CHAT_API_KEY", "").strip()
    if not base:
        return "DIFY_BASE_URL 没配"
    if not key:
        return "DIFY_API_KEY 没配"
    if _looks_like_placeholder(key):
        return (
            "DIFY_API_KEY 还是占位文字，不是真的密钥。"
            "去 Dify 后台该应用的「访问 API」页拿 app- 开头的密钥，"
            "填进 .env 后重启后端"
        )
    if chat and _looks_like_placeholder(chat):
        return "DIFY_CHAT_API_KEY 还是占位文字，顾问问答会失败"
    return None


def _read_dify_stream(response):
    """Read Dify SSE events and return the final workflow result."""
    node_trace = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue

        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue

        event = json.loads(payload)
        if not isinstance(event, dict):
            continue

        # 2026-11-17：空排序要定位到节点。流里每个 node_finished 都带该节点
        # 的输出摘要，之前全部丢弃，所以只能看到最终 candidates=[]，看不出是
        # LLM 2 没选、还是后面的代码节点把它清了。这里收一份轻量 trace。
        if event.get("event") == "node_finished":
            _nd = event.get("data") or {}
            try:
                _out = _nd.get("outputs") or {}
                _entry = {
                    "node_id": _nd.get("node_id"),
                    "title": _nd.get("title"),
                    "node_type": _nd.get("node_type"),
                    "status": _nd.get("status"),
                    "output_keys": sorted(_out.keys()) if isinstance(_out, dict) else [],
                    "output_size": len(json.dumps(_out, ensure_ascii=False)),
                    "preview": json.dumps(_out, ensure_ascii=False)[:400],
                }
                # 关键：把节点输出里的候选数量直接解析出来。空排序的定位
                # 全靠这一步——否则只能看到 preview 的 400 字符，判断不了
                # 是 LLM 2 没选，还是后面的代码节点把它清掉了。
                # LLM 2 和「代码执行 2」的原文无条件全量留存。注意必须放在
                # 解析尝试之前：空排序恰恰是「原文不是合法 JSON」造成的，
                # 若等到 json.loads 成功才记录，最需要看的那次反而记不到。
                if _nd.get("node_id") in ("1792294044029", "1792295487009"):
                    for _k in ("text", "result"):
                        _rv = _out.get(_k) if isinstance(_out, dict) else None
                        if isinstance(_rv, str):
                            _entry.setdefault("raw_by_key", {})[_k] = _rv
                    if isinstance(_out, dict) and _out.get("finish_reason"):
                        _entry["finish_reason"] = _out["finish_reason"]
                if isinstance(_out, dict):
                    for _k in ("text", "result"):
                        _v = _out.get(_k)
                        if isinstance(_v, str) and _v.strip().startswith("{"):
                            try:
                                _p = json.loads(_v)
                            except Exception:
                                continue
                            if not isinstance(_p, dict):
                                continue
                            _counts = {}
                            for _f in (
                                "candidates",
                                "eligible_products",
                                "excluded_summary",
                            ):
                                if isinstance(_p.get(_f), list):
                                    _counts[_f] = len(_p[_f])
                            _rr = _p.get("rule_result")
                            if isinstance(_rr, dict) and isinstance(
                                _rr.get("eligible_products"), list
                            ):
                                _counts["rule_result.eligible_products"] = len(
                                    _rr["eligible_products"]
                                )
                            if _counts:
                                _entry.setdefault("counts", {}).update(_counts)
                            if _p.get("ranking_version"):
                                _entry["ranking_version"] = _p["ranking_version"]
                            if "candidates" in _counts:
                                _entry["text_len"] = len(_v)
                            break
                node_trace.append(_entry)
            except Exception:
                pass
            continue

        if event.get("event") != "workflow_finished":
            continue

        finished = event.get("data")
        if not isinstance(finished, dict):
            raise ValueError("Dify 流式响应缺少 workflow_finished.data")

        status = str(finished.get("status") or "").lower()
        if status and status not in {"succeeded", "success"}:
            error = finished.get("error") or finished.get("message") or status
            raise ValueError(f"Dify 工作流未成功完成：{error}")

        # 2026-11-16：把 token 用量写进日志。LLM 2 的 completion_tokens 直接
        # 决定工作流墙钟（实测 2650 → 7694 tokens 时耗时 26s → 46s），
        # 之前这个数字被丢弃，只能靠草稿端点猜。记下来才能看清瓶颈。
        try:
            usage = finished.get("total_tokens")
            if usage is not None:
                # 2026-11-16：区分输入/输出。实测 total_tokens=51532，远高于
                # 草稿端看到的 7694（那是单节点 completion）。必须分开看，
                # 才知道该砍输入池还是砍输出长度。
                print(
                    f"[dify-usage] total_tokens={usage} "
                    f"prompt={finished.get('prompt_tokens')} "
                    f"completion={finished.get('completion_tokens')} "
                    f"elapsed={finished.get('elapsed_time')}s",
                    flush=True,
                )
                # 完整 keys 只打一次，方便确认字段真名。
                if not globals().get("_DIFY_USAGE_KEYS_LOGGED"):
                    globals()["_DIFY_USAGE_KEYS_LOGGED"] = True
                    print(f"[dify-usage-keys] {sorted(finished.keys())}", flush=True)
        except Exception:
            pass

        return {"data": finished, "_node_trace": node_trace}

    raise TimeoutError("Dify 流式响应结束前没有收到 workflow_finished")


def _dify_opener():
    """返回一个不吃 Windows 系统代理的 urllib opener。

    2026-11-17：urllib 默认读注册表里的系统代理。这台机器的系统代理指向
    127.0.0.1:7890（本地代理软件），代理没开时每个请求立刻 WinError 10061，
    而裸 socket / http.client 直连完全正常——表现成「Dify 随机连不上」，
    实际由本地代理开关决定，跟网络无关。这里显式不走系统代理；真要代理
    就显式设 HTTPS_PROXY（ProxyHandler 会读环境变量）。
    """
    if os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"):
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def maybe_call_dify(payload):
    global DIFY_LAST_ERROR
    base_url = _clean_dify_base_url()
    api_key = os.getenv("DIFY_API_KEY", "")
    if not base_url or not api_key:
        DIFY_LAST_ERROR = {
            "code": "not_configured",
            "message": "DIFY_BASE_URL 或 DIFY_API_KEY 未配置",
        }
        return None
    # 占位密钥不能往下走。拼进 Authorization 头会在 http.client 里
    # 抛 latin-1 编码错误，错误信息完全指不到「密钥没填」这个根因。
    if _looks_like_placeholder(api_key):
        DIFY_LAST_ERROR = {
            "code": "placeholder_key",
            "retryable": False,
            "message": dify_config_problem() or "DIFY_API_KEY 还是占位文字",
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        return None

    request_body = build_dify_request(payload)
    request = urllib.request.Request(
        f"{base_url}/v1/workflows/run",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    # 2026-11-17：urllib 默认读 Windows 注册表里的系统代理。这台机器的
    # 系统代理指向 127.0.0.1:7890（本地代理软件），代理没开时每个请求都
    # 立刻 WinError 10061，而裸 socket / http.client 直连完全正常——
    # 表现成「Dify 随机连不上」，实际是本地代理开关决定的，与网络无关。
    _opener = _dify_opener()
    for attempt in range(DIFY_MAX_RETRIES + 1):
        try:
            with _opener.open(request, timeout=DIFY_TIMEOUT_SECONDS) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if DIFY_RESPONSE_MODE == "streaming" and "text/event-stream" in content_type:
                    result = _read_dify_stream(response)
                else:
                    body = response.read().decode("utf-8")
                    result = json.loads(body)
                DIFY_LAST_ERROR = None
                return result
        except urllib.error.HTTPError as error:
            try:
                body = error.read().decode("utf-8", errors="replace")
                parsed = json.loads(body)
                detail = parsed if isinstance(parsed, dict) else {"body": body[:500]}
            except (OSError, json.JSONDecodeError):
                detail = {"body": str(body)[:500] if "body" in locals() else ""}
            # A POST may already have started a workflow when a network/5xx
            # error arrives. Retry only explicit rate limiting, which is a
            # definitive rejection, to avoid duplicate model runs.
            retryable = error.code == 429
            DIFY_LAST_ERROR = {
                "code": "http_error",
                "status": error.code,
                "retryable": retryable,
                "attempt": attempt + 1,
                "message": detail.get("message") or detail.get("code") or "Dify API 返回 HTTP 错误",
                "detail": detail,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            if not retryable or attempt >= DIFY_MAX_RETRIES:
                return None
        except urllib.error.URLError as error:
            DIFY_LAST_ERROR = {
                "code": "network_error",
                "retryable": False,
                "attempt": attempt + 1,
                "message": str(getattr(error, "reason", error)),
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            return None
        except TimeoutError:
            DIFY_LAST_ERROR = {
                "code": "timeout",
                "retryable": False,
                "attempt": attempt + 1,
                "message": f"Dify 请求超过 {DIFY_TIMEOUT_SECONDS} 秒未返回",
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            return None
        except (ValueError, json.JSONDecodeError) as error:
            DIFY_LAST_ERROR = {
                "code": "stream_error",
                "retryable": False,
                "attempt": attempt + 1,
                "message": str(error),
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            return None
        time.sleep(min(2 ** attempt, 4))
    return None


def _chat_scope(question):
    question = str(question or "").strip().lower()
    explicit_out_of_scope = (
        "与礼物无关",
        "和礼物无关",
        "不涉及礼物",
        "普通闲聊",
        "写一首诗",
        "写段诗",
        "写一篇小说",
        "算命",
        "股票",
        "天气",
    )
    if any(marker in question for marker in explicit_out_of_scope):
        return False
    keywords = (
        "礼物", "送", "场合", "关系", "忌讳", "禁忌", "制作", "自制", "材料",
        "步骤", "保存", "保质", "过敏", "食品", "香味", "地区", "预算", "推荐",
        "曲奇", "果酱", "茶包", "香囊", "相框", "植物", "手工", "商品",
        "生日", "纪念日", "周年", "毕业", "乔迁", "开业", "答谢", "感谢",
        "回礼", "伴手礼", "见面礼", "贺礼", "礼盒", "送给", "送人",
        "朋友", "同事", "同学", "老师", "导师", "老板", "上司", "父母",
        "长辈", "孩子", "男朋友", "女朋友", "伴侣", "情侣", "客户",
        # 习俗/包装/地区都属于送礼决策，问"广东包装用什么颜色"这种话里
        # 可能一个"送"字都没有，原来会被判成越界。
        "讲究", "风俗", "习惯", "包装", "颜色", "寓意", "数目", "数量",
        "单数", "双数", "红包", "喜", "丧", "节", "过年", "春节", "中秋", "端午",
    ) + REGION_HINTS
    return any(keyword in question for keyword in keywords)


def dify_chat_is_configured():
    chat_key = os.getenv("DIFY_CHAT_API_KEY", "").strip()
    return bool(_clean_dify_base_url() and chat_key and not _looks_like_placeholder(chat_key))


def _parse_dify_chat_response(body):
    """Extract a Dify Chat response from blocking JSON or SSE text."""
    if isinstance(body, dict):
        answer = body.get("answer")
        if isinstance(answer, str) and answer.strip():
            return answer.strip(), body.get("metadata") or {}
        data = body.get("data")
        if isinstance(data, dict):
            answer = data.get("answer") or data.get("text")
            if isinstance(answer, str) and answer.strip():
                return answer.strip(), data.get("metadata") or {}
        return None, {}

    answers = []
    metadata = {}
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        answer = event.get("answer")
        if isinstance(answer, str):
            answers.append(answer)
        if isinstance(event.get("metadata"), dict):
            metadata = event["metadata"]
    answer = "".join(answers).strip()
    return (answer or None), metadata


def _strip_chat_reasoning(value):
    """Remove leaked chain-of-thought wrappers before returning chat text."""
    text = str(value or "")
    text = re.sub(r"<think\b[^>]*>[\s\S]*?</think\s*>", "", text, flags=re.IGNORECASE)
    # If a provider emits an unterminated reasoning block, fail closed.
    text = re.sub(r"<think\b[^>]*>[\s\S]*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<!--\s*dify-deepseek-reasoning\s*-->", "", text, flags=re.IGNORECASE)
    return text.strip()


def maybe_call_dify_chat(question, context=None):
    """Ask a dedicated Dify Chat app, never bypassing the local scope guard."""
    global DIFY_LAST_ERROR
    if not dify_chat_is_configured():
        return None

    base_url = _clean_dify_base_url()
    context = context if isinstance(context, dict) else {}
    guard_prompt = (
        "你是 GiftRight 礼物顾问。只能回答送礼、礼物选择、礼物制作、材料、"
        "保存、配送、预算、过敏、风险、场合、关系和礼物习俗相关问题。"
        "如果问题与送礼无关，只回复：我只回答和送礼物相关的问题。"
        "回答必须克制清晰，单次回答只提 1-3 点最关键建议，禁止空洞寒暄与长篇大论。"
        "不得编造商品价格、日期、来源、禁忌或用户未提供的事实。"
        "涉及医疗、保健品、过敏原或宗教禁忌时，必须明确声明：‘此项涉及健康或信仰红线，必须向当事人或权威成分表核实，AI不能代为确认’。"
        "若用户问题缺少关键决策信息（如预算、关系或具体偏好），应先提出具体核实问题，不凭空假设。"
        "请优先使用已配置的 GiftRight 知识库；知识库有相关内容时应优先引用。"
        "知识库没有完全匹配内容时，仍然要基于通用送礼常识给出有帮助的回答，"
        "并明确标注“以下为一般建议，不代表知识库中的具体条目”。"
        "只输出给用户看的最终答案；严禁输出思考过程、推理草稿、分析标签、"
        "<think> 标签、隐藏推理或解释你如何得出答案。"
        f"\n用户问题：{question}"
    )
    if context:
        guard_prompt += "\n当前上下文：" + json.dumps(context, ensure_ascii=False)

    request_body = {
        "inputs": {},
        "query": guard_prompt,
        "response_mode": "streaming" if DIFY_CHAT_RESPONSE_MODE == "streaming" else "blocking",
        "conversation_id": "",
        "user": "gift-right-chat",
    }
    request = urllib.request.Request(
        f"{base_url}/v1/chat-messages",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {DIFY_CHAT_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _dify_opener().open(
            request, timeout=DIFY_TIMEOUT_SECONDS
        ) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/event-stream" in content_type:
                body = response.read().decode("utf-8", errors="replace")
                answer, metadata = _parse_dify_chat_response(body)
            else:
                body = json.loads(response.read().decode("utf-8"))
                answer, metadata = _parse_dify_chat_response(body)
        if not answer:
            raise ValueError("Dify Chat 返回为空")
        answer = _strip_chat_reasoning(answer)
        if not answer:
            raise ValueError("Dify Chat 只返回了思考内容，没有最终答复")
        DIFY_LAST_ERROR = None
        return {"answer": answer, "metadata": metadata}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        DIFY_LAST_ERROR = {
            "code": "chat_request_failed",
            "message": str(error),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        return None


def _match_catalog_by_alias(question_lower):
    """Fall back to everyday Chinese nouns when the user does not type the full product name.

    Without this the local engine only matches a catalog name verbatim (or a tag in
    English), so natural questions like "曲奇怎么保存" fall through to the generic
    "not found" answer even though the catalog does cover them.
    """
    aliases = (
        ("曲奇", ("曲奇", "饼干", "黄油饼干")),
        ("果酱", ("果酱", "果泥")),
        ("茶包", ("茶包", "茶叶", "花茶", "袋泡茶")),
        ("香囊", ("香囊", "艾草", "布艺挂饰")),
        ("咖啡", ("咖啡", "手冲")),
        ("照片书", ("照片书", "相册", "回忆卡片", "手写信")),
        ("花", ("干花", "花束", "鲜花", "花材", "相框")),
        ("植物", ("绿植", "植物", "微景观")),
        ("护手霜", ("护手霜", "手霜")),
        ("巧克力", ("巧克力",)),
        ("伞", ("雨伞", "晴雨伞", "伞")),
        ("钟", ("钟表", "小钟", "座钟")),
        ("笔记本", ("笔记本", "本子")),
        ("保温杯", ("保温杯", "水杯", "杯子")),
        ("帆布", ("帆布袋", "帆布")),
        ("手工皂", ("手工皂", "香皂", "皂")),
        ("香薰", ("香薰", "蜡片", "香氛", "精油")),
        ("坚果", ("坚果", "果干", "水果干")),
        ("收纳", ("收纳包", "收纳篮", "杯垫", "编织")),
        ("按摩", ("按摩球", "筋膜球")),
        ("果酒", ("果酒", "酒")),
        ("卡套", ("卡套", "皮具")),
    )
    for name_key, words in aliases:
        if not any(word in question_lower for word in words):
            continue
        hits = [item for item in CATALOG if name_key in item["name"]]
        if hits:
            return hits
    return []


REGION_HINTS = (
    "广东", "客家", "东源", "福建", "福州", "上海", "江苏", "河南", "长葛",
    "贵州", "水族", "陕西", "北京", "天津", "浙江", "四川", "湖南", "湖北",
    "山东", "山西", "广西", "云南", "江西", "安徽", "东北", "香港", "台湾", "潮汕",
)


def _match_region_in_question(question_lower):
    """Return the region the question names, if the library covers it."""
    for hint in REGION_HINTS:
        if hint in question_lower:
            for row in REGIONAL_VARIATIONS:
                scope = str(row.get("region_scope") or "")
                if hint in scope:
                    return hint
            return hint
    return ""


def _regional_answer_for(question_lower):
    """Answer 地方习俗 questions from the 100-row regional library.

    The chat endpoint used to ignore REGIONAL_VARIATIONS entirely, so asking about a
    region fell through to the generic "nothing found" reply even though the library
    has sourced rows for it.
    """
    region = _match_region_in_question(question_lower)
    if not region:
        if any(word in question_lower for word in ("地区", "地方", "习俗", "风俗", "习惯")):
            covered = []
            for row in REGIONAL_VARIATIONS:
                scope = str(row.get("region_scope") or "")
                label = scope.replace("语境", "").replace("资料", "").strip()
                if label and label not in covered:
                    covered.append(label)
            if covered:
                return (
                    "知识库里有来源的地方条目只覆盖这几处："
                    + "、".join(covered)
                    + "。\n换个说法直接问某个地方，例如“广东送礼有什么要注意的”。"
                    "没有来源的地方，系统不会编，建议直接问收礼人本人或其家人。"
                )
        return None
    rows = [row for row in REGIONAL_VARIATIONS if region in str(row.get("region_scope") or "")]
    if not rows:
        return (
            f"当前知识库没有检索到“{region}”的具体地方忌讳条目。"
            "地方习俗只能作为软提醒，建议直接问收礼人本人或其家人。"
        )
    wanted = []
    if any(word in question_lower for word in ("颜色", "包装", "色")):
        wanted.append("packaging_color")
    if any(word in question_lower for word in ("数量", "几个", "双数", "单数")):
        wanted.append("gift_quantity")
    if any(word in question_lower for word in ("吃", "食品", "食物", "特", "菜")):
        wanted.append("festival_food")
    if any(word in question_lower for word in ("清真", "素食", "宗教")):
        wanted.append("dietary_rule")
    picked = [row for row in rows if row.get("subtopic") in wanted] if wanted else []
    if not picked:
        picked = [row for row in rows if row.get("subtopic") in ("family_preference", "uncertainty", "document_scope")]
    if not picked:
        picked = rows[:3]
    lines = []
    for row in picked[:3]:
        label = {
            "packaging_color": "包装颜色",
            "gift_quantity": "数量组合",
            "festival_food": "节令食品",
            "dietary_rule": "饮食禁忌",
            "family_preference": "家庭偏好",
            "document_scope": "资料来源范围",
            "uncertainty": "不确定时的处理",
        }.get(row.get("subtopic"), row.get("subtopic", "习俗"))
        lines.append(f"{label}：{row.get('variation_statement', '')}")
    return "该地区的地方习俗（只针对有来源的条目，不能外推到全地区）：\n" + "\n".join(lines)


def _regional_sources_for(question_lower, limit=3):
    region = _match_region_in_question(question_lower)
    if not region:
        return []
    seen = []
    for row in REGIONAL_VARIATIONS:
        if region not in str(row.get("region_scope") or ""):
            continue
        url = row.get("source_url") or ""
        if not url or url in seen:
            continue
        seen.append(url)
        if len(seen) >= limit:
            break
    return [{"title": f"{region}地方民俗资料", "url": url} for url in seen]


def _taboo_rows_for(question_lower):
    return [
        row for row in TABOOS
        if any(part.lower() in question_lower for part in row.get("keyword", "").split("/") if part.strip())
    ]


def _taboo_notes_for(question_lower, limit=4):
    return [row.get("reason", "") for row in _taboo_rows_for(question_lower)[:limit] if row.get("reason")]


def _taboo_sources_for(question_lower, limit=4):
    labels = {
        "public_cultural_guide": "公开文化资料",
        "official_guide": "官方指南",
        "academic": "研究资料",
    }
    sources = []
    for row in _taboo_rows_for(question_lower)[:limit]:
        if not row.get("source_url"):
            continue
        raw = str(row.get("source_type") or "")
        sources.append({"title": labels.get(raw, raw or "习俗资料"), "url": row["source_url"]})
    return sources


def _rank_chat_matches(question_lower, items):
    """Order chat candidates by how well they answer what was actually asked.

    Longest-name-wins matters because "素食曲奇礼盒" would otherwise be picked over
    "手工黄油曲奇礼袋" for "自制曲奇怎么保存" — the shorter match is the less specific
    one. Questions that mention 自制/手工 prefer DIY entries, and questions asking
    about 忌讳/寓意/送不送 prefer entries whose taboos actually cover the question.
    """
    wants_diy = any(word in question_lower for word in ("自制", "手作", "手工", "diy", "材料", "步骤", "怎么做"))
    wants_taboo = any(word in question_lower for word in ("忌讳", "禁忌", "寓意", "合适吗", "能送", "能不能送", "讲究"))

    def score(item):
        total = 0
        name = item["name"].lower()
        if name in question_lower:
            total += 100
        elif name[:4] in question_lower:
            total += 40
        if wants_diy and item.get("gift_type") == "diy":
            total += 30
        if wants_diy and item.get("gift_type") != "diy":
            total -= 20
        if wants_taboo:
            flags = {str(tag).lower() for tag in item.get("tags", [])}
            if any(row.get("flag") in flags for row in TABOOS):
                total += 25
        total += len(name) // 4
        return total

    return sorted(items, key=score, reverse=True)


def _dify_chat_stream_events(question, context=None):
    """Yield SSE lines while proxying Dify chat streaming response.

    Events:
      event: stage   data: {"stage": "retrieving"}   — request sent to Dify
      event: delta   data: {"text": "..."}           — incremental answer text
      event: done    data: {"sources": [...], "provider": "dify"}
      event: fallback data: {"payload": {...}}       — local answer replaces stream
    """
    question = str(question or "").strip()
    context = context if isinstance(context, dict) else {}

    def sse(event, data):
        nl = chr(10)
        return "event: " + event + nl + "data: " + json.dumps(data, ensure_ascii=False) + nl + nl

    if not question or not _chat_scope(question):
        yield sse("fallback", {"payload": {
            "scope": "gift_knowledge",
            "answer": "我只回答送礼、礼物制作、适用场合、注意事项、忌讳和知识库内容相关的问题。",
            "sources": [{"title": "范围外问题，未查询知识库", "url": ""}], "related_gifts": [], "refusal": True,
        }})
        return

    remote_ok = False
    if dify_chat_is_configured():
        base_url = _clean_dify_base_url()
        guard_prompt = (
            "你是 GiftRight 礼物顾问。只能回答送礼、礼物选择、礼物制作、材料、"
            "保存、配送、预算、过敏、风险、场合、关系和礼物习俗相关问题。"
            "如果问题与送礼无关，只回复：我只回答和送礼物相关的问题。"
            "回答必须克制清晰，单次回答只提 1-3 点最关键建议，禁止空洞寒暄与长篇大论。"
            "不得编造商品价格、日期、来源、禁忌或用户未提供的事实。"
            "涉及医疗、保健品、过敏原或宗教禁忌时，必须明确声明：‘此项涉及健康或信仰红线，必须向当事人或权威成分表核实，AI不能代为确认’。"
            "若用户问题缺少关键决策信息（如预算、关系或具体偏好），应先提出具体核实问题，不凭空假设。"
            "请优先使用已配置的 GiftRight 知识库；知识库有相关内容时应优先引用。"
            "知识库没有完全匹配内容时，仍然要基于通用送礼常识给出有帮助的回答，"
            "并明确标注“以下为一般建议，不代表知识库中的具体条目”。"
            "只输出给用户看的最终答案；严禁输出思考过程、推理草稿、分析标签、"
            "<think> 标签、隐藏推理或解释你如何得出答案。"
            f"{chr(10)}用户问题：{question}"
        )
        if context:
            guard_prompt += chr(10) + "当前上下文：" + json.dumps(context, ensure_ascii=False)
        request_body = {
            "inputs": {},
            "query": guard_prompt,
            "response_mode": "streaming",
            "conversation_id": "",
            "user": "gift-right-chat",
        }
        request = urllib.request.Request(
            f"{base_url}/v1/chat-messages",
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {DIFY_CHAT_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            yield sse("stage", {"stage": "retrieving"})
            with _dify_opener().open(
                request, timeout=DIFY_TIMEOUT_SECONDS
            ) as response:
                metadata = {}
                pending = ""
                emitted = 0
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if not payload_str or payload_str == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if isinstance(event.get("metadata"), dict):
                        metadata = event["metadata"]
                    chunk = event.get("answer")
                    if isinstance(chunk, str) and chunk:
                        pending += chunk
                        clean = _strip_chat_reasoning(pending)
                        if len(clean) > emitted:
                            yield sse("delta", {"text": clean[emitted:]})
                            emitted = len(clean)
                final = _strip_chat_reasoning(pending)
                if final:
                    remote_ok = True
                    # 来源栏两条规矩：有可点开的 url 就给链接；只有文档名
                    # 没有 url（Dify 的 retriever_resources 经常不带 url）
                    # 也照样列出文档名——比静默空白诚实，用户至少知道答案
                    # 是查过库的。实在一条都没有，给一条明示「未附来源」。
                    # 同一文档多次命中只列一次，按首次命中排序；文件名翻译
                    # 成用户能读的知识库名（06_merge.md → 商品知识库）。
                    sources = []
                    seen_titles = set()
                    for item in metadata.get("retriever_resources", []) if isinstance(metadata, dict) else []:
                        if not isinstance(item, dict):
                            continue
                        title = _friendly_source_title(
                            item.get("document_name") or item.get("title") or "")
                        source_url = str(item.get("url") or item.get("source_url") or "").strip()
                        if not title and not source_url:
                            continue
                        if not title:
                            title = "知识库片段"
                        dedup_key = source_url or title
                        if dedup_key in seen_titles:
                            continue
                        seen_titles.add(dedup_key)
                        sources.append({"title": title, "url": source_url})
                    if not sources:
                        sources.append({"title": "本次回答未附知识库来源", "url": ""})
                    yield sse("done", {"sources": sources, "provider": "dify"})
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            global DIFY_LAST_ERROR
            DIFY_LAST_ERROR = {
                "code": "chat_request_failed",
                "message": str(error),
                "at": datetime.now().isoformat(timespec="seconds"),
            }

    if not remote_ok:
        yield sse("stage", {"stage": "local_answer"})
        yield sse("fallback", {"payload": answer_gift_chat(question, context)})


def _friendly_source_title(title):
    """把 Dify 后台的文档文件名翻译成用户能读的知识库名。

    「06_merge.md」「04_地域与禁忌提醒库.md」是内部文件名，.md 后缀和
    编号前缀对用户是噪音。查不到映射时至少剥掉 .md 后缀和编号前缀。
    """
    text = str(title or "").strip()
    if not text:
        return text
    friendly = {
        "01_商品事实库": "商品知识库",
        "01_商品事实库_v2": "商品知识库",
        "02_自制礼物知识库": "自制礼物库",
        "03_节日与场合知识库": "节日与场合库",
        "04_地域与禁忌提醒库": "地域与禁忌库",
        "05_证据与来源追溯库": "来源追溯库",
        "06_商品事实库_合并重点名版": "商品知识库",
        "06_merge": "商品知识库",
        "06_商品事实库v4_合并版": "商品知识库",
    }
    stem = text[:-3] if text.lower().endswith(".md") else text
    if stem in friendly:
        return friendly[stem]
    # 兜底：剥编号前缀（如「03_」）和 .md 已在上面剥过
    stripped = re.sub(r"^\d{1,2}_", "", stem)
    return stripped or text


def answer_gift_chat(question, context=None):
    question = str(question or "").strip()
    context = context or {}
    if not question or not _chat_scope(question):
        return {
            "scope": "gift_knowledge",
            "answer": "我只回答送礼、礼物制作、适用场合、注意事项、忌讳和知识库内容相关的问题。",
            # 拒答也是一次回答，来源栏同样不能静默空白。
            "sources": [{"title": "范围外问题，未查询知识库", "url": ""}],
            "related_gifts": [],
            "refusal": True,
        }

    remote = maybe_call_dify_chat(question, context)
    if remote:
        metadata = remote.get("metadata") or {}
        # 与流式端点同一条规矩：没 url 就列文档名，一条都没有就明示「未附来源」；
        # 同一文档多次命中只列一次；文件名翻译成用户能读的知识库名。
        sources = []
        seen_titles = set()
        for item in metadata.get("retriever_resources", []) if isinstance(metadata, dict) else []:
            if not isinstance(item, dict):
                continue
            title = _friendly_source_title(
                item.get("document_name") or item.get("title") or "")
            source_url = str(item.get("url") or item.get("source_url") or "").strip()
            if not title and not source_url:
                continue
            if not title:
                title = "知识库片段"
            dedup_key = source_url or title
            if dedup_key in seen_titles:
                continue
            seen_titles.add(dedup_key)
            sources.append({"title": title, "url": source_url})
        if not sources:
            sources.append({"title": "本次回答未附知识库来源", "url": ""})
        return {
            "scope": "gift_knowledge",
            "answer": remote["answer"],
            "sources": sources,
            "related_gifts": [],
            "refusal": False,
            "provider": "dify",
        }

    question_lower = question.lower()
    selected = []
    context_id = context.get("gift_id")
    if context_id:
        selected = [item for item in CATALOG if item["id"] == context_id]
    if not selected:
        selected = [
            item for item in CATALOG
            if item["name"].lower() in question_lower
            or item["name"][:4].lower() in question_lower
            or any(tag.lower() in question_lower for tag in item.get("tags", []))
        ]
    if not selected:
        selected = _match_catalog_by_alias(question_lower)
    selected = _rank_chat_matches(question_lower, selected)[:3]
    sources = []
    if selected:
        item = selected[0]
        if any(word in question for word in ("怎么做", "制作", "步骤", "材料")) and item.get("gift_type") == "diy":
            answer = (
                f"{item['name']}的材料包括：{'、'.join(item.get('materials', []))}。"
                f"建议步骤：{'；'.join(item.get('steps', []))}。"
                f"难度为{item.get('difficulty', '未标注')}，预计需要{item.get('prep_days', 1)}天。"
            )
        elif any(word in question for word in ("保存", "保质", "多久", "过敏", "风险", "注意")):
            answer = (
                f"{item['name']}需要注意：{item.get('risk_notes', '')}"
                f"{item.get('safety_notes', '')}"
                f"保存建议：{item.get('preservation', '请按商品标签或制作记录确认。')}"
            )
        else:
            answer = (
                f"{item['name']}属于{'自制礼物' if item.get('gift_type') == 'diy' else '购买礼物'}，"
                f"预算参考{_reference_price_range(item.get('cost', item.get('price')))},"
                f"{'制作' if item.get('gift_type') == 'diy' else '配送'}约"
                f"{item.get('prep_days', item.get('delivery_days', '未标注'))}天。"
                f"{item.get('description', '')}"
            )
        if item.get("source_url"):
            sources.append({"title": item.get("source_title", "知识库来源"), "url": item["source_url"]})
        # A product can match while the question is really about custom/luck. Without
        # this, "送长辈钟表合适吗" answers with the product blurb and silently drops
        # the actual 送钟 taboo the catalog knows about.
        if any(word in question for word in ("忌讳", "禁忌", "寓意", "合适吗", "能送", "能不能送", "讲究")):
            supplement = _taboo_notes_for(question_lower)
            if supplement:
                answer = f"{answer}\n\n相关习俗（仅作软提醒，不是全国统一禁忌）：{'；'.join(supplement)}"
                sources.extend(_taboo_sources_for(question_lower))
    else:
        taboo_hits = _taboo_notes_for(question_lower)
        regional = _regional_answer_for(question_lower)
        if taboo_hits:
            answer = "相关习俗只能作为软提醒，不能直接当作全国统一禁忌："
            answer += "；".join(taboo_hits[:4])
            sources = _taboo_sources_for(question_lower)
            if regional:
                answer = f"{answer}\n\n{regional}"
                sources.extend(_regional_sources_for(question_lower))
        elif regional:
            answer = regional
            sources = _regional_sources_for(question_lower)
        elif any(word in question_lower for word in ("寓意", "含义", "象征", "代表什么")):
            # "某个礼物的寓意是什么" names no product, so nothing matches — but a
            # bare "not found" is a bad answer when the catalog knows dozens of these.
            samples = CATALOG[:4]
            lines = [f"{item['name']}：{_gift_meaning(item)}" for item in samples]
            answer = (
                "寓意要落到具体礼物上才算数，泛泛说会失真。库里几条示例：\n"
                + "\n".join(lines)
                + "\n换个说法直接问某件礼物，例如“手冲咖啡礼盒的寓意是什么”。"
            )
        else:
            answer = (
                "资料中未检索到完全对应的具体条目。以下为一般送礼建议："
                "优先结合送礼场合、关系亲疏、预算、实用性和收礼人的明确禁忌来选择；"
                "购买类礼物应核对规格、价格、库存和配送，食品、香味类或个性化礼物"
                "还应确认过敏和接受程度。"
                "想要具体条目，可以直接问某件礼物，"
                "例如“手工黄油曲奇礼袋怎么保存”“水果果酱小罐要注意什么”“生日送钟表合适吗”。"
            )

    return {
        "scope": "gift_knowledge",
        "answer": answer,
        "sources": sources,
        "related_gifts": [{"id": item["id"], "name": item["name"]} for item in selected],
        "refusal": False,
        "provider": "local",
    }

def _parse_json_value(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_json_any(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    for _ in range(3):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            cleaned = re.sub(
                r"<think>.*?</think>",
                "",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
            decoder = json.JSONDecoder()
            parsed = None
            for index, char in enumerate(cleaned):
                if char not in "[{":
                    continue
                try:
                    parsed, _ = decoder.raw_decode(cleaned[index:])
                    break
                except json.JSONDecodeError:
                    continue
            if parsed is None:
                return None
        if isinstance(parsed, (dict, list)):
            return parsed
        if isinstance(parsed, str):
            text = parsed.strip()
        else:
            return None
    return None


def _repair_stray_quote_after_number(text):
    """修掉 `"rank":5"` 这种数字后面多出来的一个引号。

    2026-11-17 定位到的空排序根因：LLM 2（Ranker）偶尔把 `"rank": 5`
    写成 `"rank":5"`，整段 JSON 因此不合法。Dify 的「代码执行 2」节点
    解析失败后静默返回空候选，用户看到的是「AI 这次没有选出」。
    实测两次空结果分别是 2 处、5 处多余引号，删掉后 8 条候选全部恢复，
    rank 1..8 齐全、strategy 合法——所以这是纯语法噪声，不是内容错误。

    只删「数字后紧跟引号、且引号后是逗号/右括号」的那一个引号，别的位置
    一律不动；字符串里的数字不受影响，因为要求前面是 `:` 或 `,` 之类。
    """
    if not isinstance(text, str) or not text:
        return text
    return re.sub(r'(:\s*-?\d+(?:\.\d+)?)"(?=\s*[,}\]])', r"\1", text)


def _normalize_structured_output(value):
    parsed = _parse_json_any(value)
    if not isinstance(parsed, dict):
        return None

    for wrapper in ("result", "output", "text"):
        nested = _parse_json_any(parsed.get(wrapper))
        if isinstance(nested, dict):
            normalized = _normalize_structured_output(nested)
            if normalized is not None:
                return normalized

    if "candidates" in parsed:
        candidate_value = parsed.get("candidates")
    else:
        candidate_value = parsed.get("ranked_candidates")
    candidates = _parse_json_any(candidate_value)
    if not isinstance(candidates, list):
        return None

    rule_result = _parse_json_any(parsed.get("rule_result"))
    decision_trace = _parse_json_any(parsed.get("decision_trace"))
    if not isinstance(rule_result, dict):
        return None
    # Dify's final Card Writer contract does not require decision_trace.
    # Synthesize a minimal trace so a successful workflow is not rejected
    # merely because this display-only metadata was omitted.
    if not isinstance(decision_trace, dict):
        decision_trace = {
            "filter_steps": {},
            "selection_mode": "dify_workflow",
        }

    intent = parsed.get("intent")
    if not isinstance(intent, dict):
        intent = {}

    labels = {
        "safe": "稳妥款",
        "heart": "心意款",
        "special": "特别款",
        "practical": "也可以",
        "diy": "自制款",
        "memory": "纪念款",
        "distinctive": "个性款",
    }
    default_type = intent.get("gift_mode") or "purchased"

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate.setdefault(
            "product_id",
            candidate.get("id") or candidate.get("diy_id") or "",
        )
        candidate.setdefault(
            "title",
            candidate.get("name") or candidate.get("product_name") or "未命名候选",
        )
        candidate.setdefault(
            "display_title",
            _display_title(candidate.get("title")),
        )
        candidate.setdefault("gift_type", default_type)
        candidate.setdefault(
            "gift_type_label",
            "自制礼物" if candidate["gift_type"] == "diy" else "购买礼物",
        )
        candidate.setdefault("strategy_label", labels.get(candidate.get("strategy"), "推荐款"))
        candidate.setdefault("category", "未分类")
        candidate.setdefault("description", "")
        candidate.setdefault("meaning", "Dify 未提供该礼物的寓意说明。")
        candidate.setdefault(
            "why_this",
            "；".join(candidate.get("reason_evidence") or [])
            or "Dify 未提供可核对的选择理由。",
        )
        candidate.setdefault(
            "reason_evidence",
            [candidate["why_this"]] if candidate["why_this"] else [],
        )
        candidate.setdefault("watch_out", "")
        # 发货/时效话术不再展示：Dify 老输出里带的配送填充语一律清掉
        for _k in ("watch_out", "fulfillment_estimate", "fulfillment_label"):
            _v = candidate.get(_k)
            if isinstance(_v, str) and ("配送" in _v or "时效" in _v or "发货" in _v):
                candidate[_k] = ""
        candidate.setdefault("soft_warnings", [])
        candidate.setdefault("taboo_notes", [])
        candidate.setdefault("source", "")
        candidate.setdefault("source_url", "")
        if candidate.get("delivery_days") is None:
            candidate.setdefault("fulfillment_estimate", "")
        candidate.setdefault("planning_label", "")
        candidate.setdefault("region_note", "")
        candidate.setdefault("volatility_reasons", [])

    parsed["candidates"] = candidates
    parsed["rule_result"] = rule_result
    parsed["decision_trace"] = decision_trace
    return parsed


def normalize_dify_response(response):
    """Extract the structured workflow output without exposing transport details."""
    if not isinstance(response, dict):
        return None

    candidates = [
        response.get("data", {}).get("outputs") if isinstance(response.get("data"), dict) else None,
        response.get("outputs"),
        response.get("result"),
        response,
    ]
    for value in candidates:
        parsed = _normalize_structured_output(value)
        if parsed is not None:
            return parsed
    return None


def enrich_dify_result(result, payload, llm_selected=False):
    """Restore source facts omitted by the Dify ranking/output nodes.

    llm_selected=True 表示 result 的 candidates 是 Dify 排序模型挑出来的
    （而不是本地规则预览），只有这条路径才允许把卡标记成「模型文案」。
    """
    if not isinstance(result, dict):
        return result

    source_items = list(CATALOG)
    source_items.extend(get_dify_candidates(payload, limit=DIFY_CANDIDATE_LIMIT))
    by_id = {}
    for item in source_items:
        if not isinstance(item, dict):
            continue
        for key in ("id", "product_id", "item_id", "offer_id"):
            value = str(item.get(key) or "")
            if value:
                by_id.setdefault(value, item)
    # Dify 知识检索命中 06 库的 1688/淘宝卡片，其商品 ID 不在本地 24 条候选池里，
    # 只存在于知识库 md 文件中。解析 md 卡片（商品ID=...；标题=...；价格=...）补索引，
    # 否则这些候选在页面上显示为「未命名候选」。
    for row in _load_real_catalog():
        # 重点名优先：v2/v3 行只有 title_key（LLM 改写短标题），没有 name 字段。
        # 不补这里，Dify 候选会回退到营销原题（【生日礼物】/【双11抢购】头）。
        if not row.get("name"):
            row["name"] = str(row.get("title_key") or "").strip() or row.get("title") or ""
        for key in ("id", "product_id", "item_id", "offer_id"):
            value = str(row.get(key) or "")
            if value:
                by_id.setdefault(value, row)
    for kb_item in _load_kb_product_cards():
        for key in ("id", "product_id", "item_id", "offer_id"):
            value = str(kb_item.get(key) or "")
            if value:
                by_id.setdefault(value, kb_item)

    intent = normalize_intent(payload)
    factual_fields = (
        "name",
        "title",
        "gift_type",
        "category",
        "price",
        "price_quality",
        "cost",
        "delivery_days",
        "prep_days",
        "difficulty",
        "materials",
        "steps",
        "preservation",
        "allergens",
        "safety_notes",
        "risk_notes",
        "description",
        "source",
        "source_url",
        "source_title",
        "tags",
        "status",
    )

    for card in result.get("candidates", []):
        if not isinstance(card, dict):
            continue

        source = by_id.get(str(card.get("product_id") or ""))
        if not source:
            continue

        for field in factual_fields:
            value = source.get(field)
            if value not in (None, "", []):
                card[field] = value

        card["title"] = source.get("name") or source.get("title") or card.get("title")
        card["display_title"] = _display_title(card["title"])
        gift_type = source.get("gift_type") or "purchased"
        card["gift_type"] = gift_type
        card["gift_type_label"] = "自制礼物" if gift_type == "diy" else "购买礼物"
        observed_price = source.get("price", source.get("cost", card.get("price")))
        if gift_type == "purchased":
            price_display = _reference_price_range(
                observed_price, source.get("price_quality")
            )
        else:
            price_display = _reference_price_range(observed_price)
        card["price"] = price_display
        card["price_band"] = price_display
        card["cost"] = _reference_price_range(source.get("cost", observed_price))
        if gift_type == "purchased" and source.get("price_quality"):
            card["price_note"] = (
                "这款的价格看起来偏低，下单前先确认规格和实际到手价。"
                if source["price_quality"] == "suspect_low_entry_price"
                else ""
            )
            warnings = card.get("soft_warnings")
            if not isinstance(warnings, list):
                warnings = []
            # 价格口径属于所有卡片共享的提醒，统一放到页面底部的
            # notices 里逐条展示；不再往每张卡的 soft_warnings 塞同一条话。
            card["soft_warnings"] = list(dict.fromkeys(warnings))
        elif gift_type == "diy" and observed_price is not None:
            card["price_note"] = ""
        generated_facts = _local_reason_facts(source, intent, price_display)
        # 理由来源优先级：Dify why_recommended > Dify reason_evidence > 本地兜底。
        # 「标题为…」「类别为…」这类复述字段的话不算理由，当占位处理。
        def _lazy(text):
            return bool(re.search(r"^(标题为|类别为|标题：|类别：)", str(text or "").strip()))

        def _sanitize_ai_copy(text):
            if not text:
                return ""
            t = str(text).strip()
            # 剥离复述字段头
            t = re.sub(r"^(标题为|类别为|标题：|类别：)", "", t).strip()
            # 子句级过滤价格/预算与AI车轱辘套话
            parts = re.split(r"([，,。；;！!？?])", t)
            kept = []
            forbidden_clause_keywords = [
                "预算", "价格", "价位", "百元档", "千元档", "到手价", "性价比", "便宜",
                "不会出错", "闭眼入", "正合分寸", "送礼的分寸", "不越界", "不显刻意",
                "更留得住", "陪伴感", "一次性消耗品", "加点心意又不想太隆重", "关系不错的朋友",
                "朋友间不铺张", "轻松的氛围", "爱看或想开始看", "拿捏", "深度推演",
            ]
            opening_pattern = r"^(朋友|长辈|同事|对象|父母|领导|收礼人)?(过)?(生日|结婚|过节|平时)?(想|打算)?送(实用(的(东西|物件)?|物件)?|心意(礼物)?|吃的|点用得久的)?$"
            i = 0
            while i < len(parts):
                chunk = parts[i].strip()
                i += 2
                if not chunk:
                    continue
                if re.match(opening_pattern, chunk):
                    continue
                if any(kw in chunk for kw in forbidden_clause_keywords):
                    continue
                kept.append(chunk)

            res = "，".join(kept) if kept else t

            replacements = [
                (r"比一次性消耗品更留得住这份朋友情[。！!？?]*", "适合日常摆放留存。"),
                (r"留得住这份朋友情[。！!？?]*", "适合日常摆放留存。"),
                (r"也让人一眼看出你花了心思[。！!？?]*", "适合表达心意。"),
                (r"让人一眼看出你花了心思[。！!？?]*", "适合表达心意。"),
                (r"最不容易出错[，,]*", "日常较稳妥，"),
                (r"绝不会错[，,]*", "日常较稳妥，"),
                (r"闭眼入[，,]*", ""),
                (r"最怕太正式[，,]*", "注重日常实用，"),
                (r"不挑人也不显刻意[，,]*", "口味温和，"),
                (r"想送点有心意又不占地方的东西[，,]*", "兼顾心意与实用，"),
                (r"带着童年动画的俏皮感[，,]*", "设计轻便有趣，"),
                (r"满满心意", "实用心意"),
                (r"满满的仪式感", "仪式感"),
                (r"仪式感拉满", "实用得体"),
                (r"拿捏[，,]*", ""),
                (r"深度推演[，,]*", ""),
            ]
            for pattern, repl in replacements:
                res = re.sub(pattern, repl, res)
            res = re.sub(r"^[，,、\s]+", "", res)
            res = re.sub(r"[，,、\s]+$", "", res)
            res = re.sub(r"^[，,、\s]*又\s*", "", res)
            res = re.sub(r"[，,、]{2,}", "，", res)
            res = re.sub(r"([。！!？?])+", r"\1", res)
            if res and not re.search(r"[。！!？?]$", res):
                res += "。"
            return res.strip()

        llm_why = str(card.get("why_recommended") or "").strip()
        if not llm_why or _is_placeholder_text(llm_why) or _lazy(llm_why):
            llm_why = ""
        llm_evidence = [
            str(item).strip()
            for item in (card.get("reason_evidence") or [])
            if str(item).strip() and not _is_placeholder_text(item) and not _lazy(item)
        ]
        llm_why_from_model = False
        if llm_why:
            cleaned_why = _sanitize_ai_copy(_redact_exact_price_text(llm_why))
            card["why_this"] = cleaned_why
            card["reason_evidence"] = [
                _sanitize_ai_copy(_redact_exact_price_text(item)) for item in (llm_evidence or [cleaned_why])
            ]
            llm_why_from_model = True
        elif llm_evidence and llm_selected:
            cleaned_ev = _sanitize_ai_copy(_redact_exact_price_text(llm_evidence[0]))
            card["why_this"] = cleaned_ev
            card["reason_evidence"] = [
                _sanitize_ai_copy(_redact_exact_price_text(item)) for item in llm_evidence
            ]
            llm_why_from_model = True
        elif llm_selected:
            # 模型挑中了这张卡但没给理由：宁可空着，也不要拿本地模板
            # 冒充模型文案（卡片会显示「为什么选它」，内容必须是模型写的）。
            card["why_this"] = ""
            card["reason_evidence"] = []
        else:
            # 本地路径的 why_this 也不该是「按 X 这个方向挑的；查过 N 条忌讳」
            # 这种过程话——它是内部筛选日志，不是给用户的理由。备选卡本来
            # 就不渲染文案块，这里置空即可。
            card["why_this"] = ""
            card["reason_evidence"] = generated_facts

        lead_days = source.get("prep_days", source.get("delivery_days"))
        if lead_days is not None:
            card["delivery_days"] = lead_days
            card["fulfillment_label"] = "制作" if gift_type == "diy" else "配送"
            card["fulfillment_estimate"] = (
                f"预计制作完成 {lead_days} 天"
                if gift_type == "diy"
                else ""
            )
            card.setdefault("planning_label", f"建议至少提前 {lead_days} 天准备")

        # 文案来源必须如实标记：卡上写着「由模型根据你的场景挑出，附推荐理由
        # 和寓意」，所以 is_llm_copy 只能在模型真的给了文案时为 True。
        # 以前这里无条件写 True，再用本地寓意表/理由模板补空，于是模板文案
        # 被当成模型文案展示——用户问「LLM 干了什么」时这就是错的答案。
        card["selected_by"] = "llm" if llm_selected else "local_rule"
        card["copy_source"] = "template"
        llm_meaning = str(card.get("meaning") or "").strip()
        has_llm_meaning = bool(llm_meaning) and not _is_placeholder_text(llm_meaning)
        if not has_llm_meaning:
            # 本地路径（llm_selected=False）不再用寓意表补文案——卡片会显示
            # 「这份礼物想说什么」，内容必须是模型写的，模板补上就是冒充。
            # 备选卡本来就不渲染这两块，空着不损失任何信息。
            card["meaning"] = _meaning_for_product(source) if llm_selected else ""
        else:
            card["meaning"] = _sanitize_ai_copy(_redact_exact_price_text(llm_meaning))
        # 只有模型既选中了卡、又至少贡献了寓意或理由之一，才算「模型文案」。
        # 两样都没有时它只是一张被排序器排过序的规则卡，前端不该给它挂
        # 「为什么选它 / 这份礼物想说什么」这两块，也不该放进「AI 精选」区。
        llm_copy = llm_selected and (has_llm_meaning or llm_why_from_model)
        if llm_copy:
            card["copy_source"] = "llm"
        else:
            card["copy_source"] = "template"
            # 无论本地还是 LLM 路径，非模型文案的卡 meaning 一律清空——
            # 以前只清 llm_selected 分支，本地预览的模板寓意漏了出去。
            card["meaning"] = ""
        card["is_llm_copy"] = bool(llm_copy)
        notes = _item_taboo_notes(source, intent)
        if notes:
            card["taboo_notes"] = notes

        # 预览层已经把 risk_notes/safety_notes 拼进 watch_out 了（还带上了
        # 配送时效），这里不能再拼一遍——整段重复就是这么来的。缺了才补，
        # 且"时间快照"这类每条商品都一样的免责话由页底提醒统一讲，卡片不重复。
        existing_watch_out = card.get("watch_out", "")
        if existing_watch_out in {"...", "暂无特别提醒"}:
            existing_watch_out = ""
        existing_watch_out = _strip_snapshot_warnings(existing_watch_out)
        if not existing_watch_out:
            extra = [
                text
                for text in (source.get("safety_notes"),)
                if text and text not in {"...", "暂无特别提醒"}
            ]
            existing_watch_out = "；".join(dict.fromkeys(extra))
        # 没有真实注意事项就不写——页底统一提醒"未标注不代表没有"。
        card["watch_out"] = existing_watch_out
        for field in ("description", "meaning", "watch_out", "risk_note", "price_note"):
            if isinstance(card.get(field), str):
                card[field] = _redact_exact_price_text(card[field])
        card["soft_warnings"] = [
            _redact_exact_price_text(item) if isinstance(item, str) else item
            for item in (card.get("soft_warnings") or [])
        ]

    return result


def _balanced_display_slice(cards, limit):
    """按礼物类型分配显示上限，避免一类把另一类整个挤出结果。

    因为 get_preview_candidates 先排购买类、后拼自制类，直接 cards[:limit]
    会让 gift_mode=all 时的自制候选全部落在截断线之后——"手作推荐"页
    永远显示 0 件。这里在两类都存在时各分一半，顺序仍按原 rank。
    """
    if len(cards) <= limit:
        return cards
    purchased = [card for card in cards if card.get("gift_type") != "diy"]
    diy = [card for card in cards if card.get("gift_type") == "diy"]
    if not purchased or not diy:
        return cards[:limit]
    diy_quota = max(1, min(len(diy), limit // 2))
    purchased_quota = max(1, limit - diy_quota)
    keep = {id(card) for card in purchased[:purchased_quota]}
    keep.update(id(card) for card in diy[:diy_quota])
    return [card for card in cards if id(card) in keep]


def _normalize_preview_cards(result):
    """本地兜底路径的卡全是规则筛出来的，不是 LLM 文案。

    enrich_dify_result 会统一强写 is_llm_copy=True，这里纠正回来；
    备选卡（practical）的寓意/理由是模型文案，没有模型背书就清掉。
    """
    for card in result.get("candidates", []):
        if not isinstance(card, dict):
            continue
        card["is_llm_copy"] = False
        if card.get("strategy") == "practical":
            card["meaning"] = ""
            card["why_this"] = ""
            card["reason_evidence"] = []
    return result


def build_fast_preview(payload):
    # 注意：这里刻意保留 _overflow_eligible_cards（下划线开头的内部键）。
    # merge_dify_with_preview 需要它来兜底查找「Dify 选中但没进前 30 条展示」
    # 的卡；对外输出由 GiftRightHandler._send_json 统一剥离下划线键，
    # 所以它不会出现在 API 响应里。
    result = _build_fast_preview_inner(payload)
    return _normalize_preview_cards(result)


def _build_fast_preview_inner(payload):
    candidate_products = get_preview_candidates(
        payload,
        limit=PREVIEW_CANDIDATE_LIMIT,
    )
    checked = dify_rule_node_main(
        raw_gift_request=json.dumps(payload, ensure_ascii=False),
        candidate_products=json.dumps(candidate_products, ensure_ascii=False),
    )
    checked_result = _parse_json_any(checked.get("result")) or {}
    eligible = checked_result.get("eligible_products", [])
    strategies = (
        ("safe", "稳妥款"),
        ("heart", "心意款"),
        ("special", "特别款"),
        ("safe", "稳妥款"),
        ("heart", "心意款"),
    )
    cards = []
    # 策略按「同一类礼物内的名次」发，而不是按整个 eligible 的下标。
    # 否则自制候选永远排在购买候选之后，全部落到 practical/备选，
    # 「手作推荐」页每一张卡都是同一个紫色圆点——语义色等于失效。
    type_rank = {}
    global_rank = 0
    for product in eligible:
        if not isinstance(product, dict):
            continue
        global_rank += 1
        kind = product.get("gift_type") or "purchased"
        index = type_rank.get(kind, 0)
        type_rank[kind] = index + 1
        strategy, strategy_label = strategies[index] if index < len(strategies) else ("practical", "也可以")
        price = product.get("price", product.get("cost"))
        is_purchased = product.get("gift_type") != "diy"
        price_display = (
            _reference_price_range(price, product.get("price_quality"))
            if is_purchased
            else _reference_price_range(price)
        )
        taboo_notes = _item_taboo_notes(product, payload)
        # 本地兜底理由只服务「LLM 没给出文案」时的占位；措辞日常化，
        # 不再用「候选通过当前硬约束检查」这类系统话术开头。
        why_parts = []
        category = str(product.get("category") or "").strip()
        if category:
            why_parts.append(f"按{category}这个方向挑的")
        occasion_label = str(payload.get("occasion_label") or "").strip()
        title_text = str(product.get("name") or product.get("title") or "")
        if occasion_label and occasion_label in title_text:
            why_parts.append(f"商品本身贴合「{occasion_label}」这个场合")
        if taboo_notes:
            why_parts.append(f"查过 {len(taboo_notes)} 条相关忌讳，没有撞上")
        else:
            why_parts.append("没有命中已知的送礼忌讳")
        reason = "；".join(why_parts) + "。"
        watch_out = product.get("risk_notes") or product.get("safety_notes") or ""
        cards.append(
            {
                "product_id": str(
                    product.get("product_id")
                    or product.get("id")
                    or product.get("diy_id")
                    or ""
                ),
                "title": product.get("name") or product.get("title") or "未命名候选",
                "display_title": _display_title(product.get("name") or product.get("title")),
                "strategy": strategy,
                "strategy_label": strategy_label,
                "rank": global_rank,
                "gift_type": product.get("gift_type") or "purchased",
                "gift_type_label": (
                    "自制礼物"
                    if product.get("gift_type") == "diy"
                    else "购买礼物"
                ),
                "category": product.get("category") or "未分类",
                "price": price_display,
                "price_band": price_display,
                "price_note": (
                    "这款的价格看起来偏低，下单前先确认规格和实际到手价。"
                    if product.get("price_quality") == "suspect_low_entry_price"
                    else ""
                ),
                "cost": _reference_price_range(product.get("cost", price)),
                "delivery_days": product.get("delivery_days", product.get("prep_days")),
                "description": product.get("description") or "",
                # 预览卡的 meaning/why_this 是本地模板（_gift_meaning /
                # _local_reason_facts），不算模型文案——置空交给 enrich 层。
                # 以前这里填了模板文案，enrich 的 has_llm_meaning 把它误判
                # 成「模型给的」，模板卡就顶着模型文案的样子进了精选区。
                "meaning": "",
                "why_this": _redact_exact_price_text(reason),
                "reason_evidence": [_redact_exact_price_text(reason)],
                "watch_out": watch_out or "",
                "is_llm_copy": False,
                "source": product.get("source") or product.get("source_title") or "",
                "source_url": product.get("source_url") or "",
                "soft_warnings": [],
                "taboo_notes": taboo_notes,
                "materials": product.get("materials") or [],
                "steps": product.get("steps") or [],
                "difficulty": product.get("difficulty"),
                "preservation": product.get("preservation") or "",
                "allergens": product.get("allergens") or [],
                "fulfillment_label": (
                    "制作" if product.get("gift_type") == "diy" else "配送"
                ),
                "fulfillment_estimate": (
                    f"预计制作完成 {product.get('prep_days')} 天"
                    if product.get("gift_type") == "diy"
                    and product.get("prep_days") is not None
                    else ""
                ),
                "planning_label": "",
                "region_note": "",
                "volatility_reasons": [],
            }
        )

    displayed_cards = _balanced_display_slice(cards, DISPLAY_CANDIDATE_LIMIT)
    # 2026-11-17：Dify 是从 DIFY_CANDIDATE_LIMIT(36) 条里选的，而页面只展示
    # DISPLAY_CANDIDATE_LIMIT(30) 条。Dify 选中第 31–36 条时，合并阶段按
    # preview_by_id 查不到，就被当成「不在本地池」丢掉——实测 5 次里 3 次
    # 稳定丢掉同一个 product_id(1067172451407)。这不是模型错选，是两边口径
    # 不一致。这里把截断前的合格卡一并带出去，供合并时兜底查找。
    _all_eligible_cards = cards
    # 规则节点的真实输出结构是 {intent, rule_result:{hard_blocks, soft_warnings,
    # route_estimate...}, route_estimate, ...}。以前这里读的是
    # checked_result["excluded"] / ["rule_warnings"]，两个 key 都不存在，
    # 于是「距离送礼还有几天」算出来的时效提醒永远掉在空里、页面一个字都不返回。
    checked_rule_result = checked_result.get("rule_result")
    if not isinstance(checked_rule_result, dict):
        checked_rule_result = {}
    hard_blocks = checked_rule_result.get("hard_blocks") or []
    rule_warnings = checked_rule_result.get("soft_warnings") or []
    route_estimate = checked_result.get("route_estimate")
    if not isinstance(route_estimate, dict):
        route_estimate = None
    checked_intent = checked_result.get("intent")
    if not isinstance(checked_intent, dict):
        checked_intent = {}
    # 前端要用 intent.gift_mode 决定落在购买页还是手作页，这个字段来自请求
    # payload 而不是规则节点的解析结果，所以两层都要：payload 打底、规则节点
    # 的规范化 intent 覆盖在上。
    merged_intent = {**payload, **checked_intent}
    result = {
        "trace_id": "gr-preview-" + uuid.uuid4().hex[:8],
        "mode": "local_rule_mode",
        "intent": merged_intent,
        "rule_result": {
            "eligible_product_ids": [
                card["product_id"] for card in displayed_cards
            ],
            "hard_blocks": hard_blocks,
            "soft_warnings": rule_warnings,
            "excluded_summary": checked_rule_result.get("excluded_summary") or [],
            "rules_version": checked_rule_result.get("rules_version")
            or "dify-hard-rule-node",
        },
        "route_estimate": route_estimate,
        "decision_trace": {
            "filter_steps": {
                "candidate_pool": len(candidate_products),
                "eligible": len(cards),
            },
            "unknown_notes": checked_result.get("input_diagnostics") or [],
            "selection_mode": "instant_deterministic_preview",
        },
        "candidates": displayed_cards,
        # 内部字段：截断前的全部合格卡（含 Dify 可能选中但未展示的那几条）。
        # 供 enrich_dify_result 合并时兜底，不对外输出。
        "_overflow_eligible_cards": _all_eligible_cards,
        "page_notices": _build_page_notices(
            payload, displayed_cards, rule_warnings=rule_warnings,
            route_estimate=route_estimate,
        ),
        "scene_taboo_summary": _scene_taboo_summary(payload),
        "excluded_summary": [],
    }
    return enrich_dify_result(result, payload)


def _build_page_notices(payload, cards, rule_warnings=None, route_estimate=None):
    """页底统一提醒：只说所有卡片共享的事实口径，措辞日常一点。

    价格口径不该在每张卡上重复；凡是个别卡片才有的风险，仍然留在
    该卡的 watch_out / taboo_notes 里。时效提醒已随配送时效功能
    （2026-11-14 用户决定）整体下线；rule_warnings / route_estimate
    参数保留以兼容旧调用，不再产生任何时效文案。
    """
    notices = []
    if cards:
        # 口径不变，措辞按人平常说话的方式写。旧版三条（"参考信息，由你
        # 自己决定""商品快照""不代表没有任何讲究——"）是免责声明腔，
        # 2026-11-13 用户批"AI 味太重"后全部重写。
        notices.append("价格仅供参考，具体以购买时为准。")
        notices.append("库存、规格与优惠活动请在下单前确认。")
        notices.append("送礼习惯因地区与家庭而异，拿不准时建议先了解对方的习惯。")
    return notices


# 场合规则（occasion_rules.csv）里的 forbidden_flags 是英文 token，
# 要显示给用户就得落到中文词上。表里只有这些 token 有确定的中文说法；
# unknown_scope / unverified_assumption 这类是「系统该怎么处理」的元标记，
# 不是礼物禁忌，不往页面上搬。
_OCCASION_FLAG_LABELS = {
    "clock": "钟表",
    "umbrella": "伞",
    "shoes": "鞋",
    "pear": "梨",
    "sharp": "刀剪",
    "fragile": "易碎品",
    "alcohol": "含酒精的礼物",
    "celebratory_alcohol": "含酒精的礼物",
    "allergen": "含过敏原的食品",
    "unknown_allergen": "成分不明的食品",
    "unknown_dietary_status": "饮食禁忌不明的食品",
    "meat": "肉类",
    "animal_product": "动物制品（皮革/羊毛等）",
    "strong_scent": "气味浓的物品",
    "intimate": "过于私密的礼物",
    "luxury_or_high_pressure": "价格压力大的礼物",
    "luxury_or_cash_like": "现金或类现金的礼物",
    "expensive_pressure": "价格压力大的礼物",
    "highly_personal_unknown_preference": "高度个性化的定制礼物",
    "loud_personalized": "过于张扬的定制礼物",
    "oversized": "体积过大的礼物",
    "perishable_unknown_delivery": "生鲜类",
    "politically_sensitive": "涉及政治立场的物品",
    "age_stereotype": "带年龄刻板印象的物品",
    "red_festive": "红色节庆包装",
    "cross_contact": "有交叉污染风险的食品",
}

# 前端 occasion 值是简写（birthday / thank_you / holiday ...），
# occasion_rules 用的是具体场合键。这里把能对上的都接上，
# 对不上的（holiday/celebration/other）交给自定义场合文字去匹配。
_OCCASION_VALUE_ALIASES = {
    "birthday": ("birthday", "one_year_old"),
    "thank_you": ("thank_you", "client_thank_you", "boss_thank_you"),
    "graduation": ("graduation",),
    "onboarding": ("new_job", "workplace"),
    "celebration": ("promotion", "opening", "engagement"),
    "holiday": ("chinese_new_year", "mid_autumn", "festival_general"),
}

# 这几条的触发词描述的是「送礼对象」而非「场合」，只能从关系字段命中。
_RELATION_ONLY_KEYS = {"workplace", "elderly_visit"}

_OCCASION_TEXT_KEYWORDS = (
    ("chinese_new_year", ("春节", "过年", "新年", "年货")),
    ("mid_autumn", ("中秋", "月饼")),
    ("lantern_festival", ("元宵",)),
    ("dragon_boat", ("端午", "粽子")),
    ("qixi", ("七夕",)),
    ("double_ninth", ("重阳",)),
    ("winter_solstice", ("冬至",)),
    ("laba", ("腊八",)),
    ("qingming", ("清明",)),
    ("wedding", ("婚礼", "结婚", "婚宴")),
    ("engagement", ("订婚",)),
    ("housewarming", ("乔迁", "搬家", "新居", "入伙")),
    ("get_well", ("探病", "生病", "住院", "康复")),
    ("hospital_visit", ("探病", "医院", "住院")),
    ("condolence", ("白事", "丧事", "追悼")),
    ("teacher_day", ("教师节", "老师节", "谢师")),
    ("retirement", ("退休",)),
    ("promotion", ("升职", "晋升",)),
    ("new_job", ("入职", "新工作", "上班第一天")),
    ("farewell", ("离职", "送别", "毕业离别")),
    ("apology", ("道歉", "赔罪")),
    ("graduation", ("毕业",)),
    ("birthday", ("生日", "寿")),
    ("one_year_old", ("满月", "百天", "周岁")),
    ("new_baby_full_month", ("满月", "百天")),
    ("workplace", ("同事", "职场", "领导", "上司")),
    ("religious_diet", ("清真", "宗教", "素食")),
    ("elderly_visit", ("长辈", "老人", "看望")),
    ("business_visit", ("商务", "客户", "拜访")),
    ("opening", ("开业", "开张")),
    ("thank_you", ("答谢", "感谢", "谢师")),
)


def _occasion_keys_for(payload):
    """把一次请求翻译成 occasion_rules 里的场合键，宽但不乱猜。

    自定义场合文字比下拉选项更具体：选了「庆祝」又写了「同学婚礼」时，
    该按婚礼算，而不是把 celebration 展开成 promotion/opening/engagement
    一起命中——否则婚礼场景会冒出「晋升祝贺」的规则。
    """
    text = " ".join(
        str(payload.get(field) or "")
        for field in ("occasion_label", "occasion_custom", "free_text")
    )
    relation_text = " ".join(
        str(payload.get(field) or "")
        for field in ("relation_label", "relation_custom")
    )
    text_keys = []
    for key, words in _OCCASION_TEXT_KEYWORDS:
        # 这几条判的是「送礼对象」而不是「场合」，只该从关系字段命中——
        # 否则「同事乔迁新居」里的「同事」会把乔迁场景误判成职场场合，
        # 冒出「职场关系避免过度私密」这种和乔迁无关的提醒。
        # 但场合词（乔迁/婚礼/春节…）不受此限，场合文字里出现就命中。
        haystack = relation_text if key in _RELATION_ONLY_KEYS else text
        if any(word in haystack for word in words) and key not in text_keys:
            text_keys.append(key)
    # 送礼对象词只从关系字段来；场合词优先级更高。如果同时命中了「对象键 +
    # 场键」（如 relation=同事 + custom=乔迁），只保留场键—— workplace 这类
    # 规则说「职场关系避免过度私密」，对乔迁、生日场景都不适用。
    occasion_keys = [k for k in text_keys if k not in _RELATION_ONLY_KEYS]
    if occasion_keys:
        return occasion_keys
    return text_keys
    value = str(payload.get("occasion") or "").strip().lower()
    return list(_OCCASION_VALUE_ALIASES.get(value, ()))


def _occasion_rule_items(payload, limit=3):
    """场合级底线提醒：来自 occasion_rules.csv，一直没被运行时加载过。"""
    if not OCCASION_RULES:
        return []
    keys = _occasion_keys_for(payload)
    if not keys:
        return []
    items = []
    seen = set()
    for row in OCCASION_RULES:
        if str(row.get("occasion") or "").strip() not in keys:
            continue
        flags = [
            flag.strip()
            for flag in str(row.get("forbidden_flags") or "").split("|")
            if flag.strip()
        ]
        labels = [
            _OCCASION_FLAG_LABELS[flag]
            for flag in flags
            if flag in _OCCASION_FLAG_LABELS
        ]
        note = str(row.get("reason") or "").strip()
        if not labels or not note:
            continue
        keyword = "、".join(labels)
        if keyword in seen:
            continue
        seen.add(keyword)
        item = {"keyword": keyword, "note": note, "source": "知识库场合规则"}
        source_url = str(row.get("source_url") or "").strip()
        if source_url:
            item["source_url"] = source_url
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _scene_taboo_summary(payload, llm_answer=None, llm_used=False):
    """场景级忌讳/注意事项总提醒（页首展示）。

    来源分三层，按优先级：
    1. 知识库（taboos.csv 确定性匹配：场合/关系/自由描述/收礼人地区）；
    2. 礼物顾问（Dify 知识库问答，在后台任务里补充，替换占位）;
    3. 都没有时，给一条诚实的兜底话：没查到明确忌讳，但请注意核实。
    返回 {status: kb_found|ai_found|none, items: [{keyword, note}], sources: [...], disclaimer}
    """
    searchable_parts = [
        str(payload.get("occasion_label") or ""),
        str(payload.get("occasion_custom") or ""),
        str(payload.get("recipient_relation") or ""),
        str(payload.get("relation_label") or ""),
        str(payload.get("relation_custom") or ""),
        str(payload.get("free_text") or ""),
        str(payload.get("recipient_region") or ""),
    ]
    searchable = " ".join(part for part in searchable_parts if part)
    question_lower = searchable.lower()

    items = []
    seen_keywords = set()
    for row in _taboo_rows_for(question_lower):
        keyword = str(row.get("keyword") or "").strip()
        note = str(row.get("reason") or "").strip()
        if not keyword or not note or keyword in seen_keywords:
            continue
        seen_keywords.add(keyword)
        items.append({"keyword": keyword, "note": note, "source": "知识库"})
        if len(items) >= 6:
            break

    # 地区条目：以前是拿「广东」去 taboos.csv 里做子串匹配，命中的是
    # 「广东东源/客家语境」这种县级的来源范围，还会把 4 条一模一样的
    # 「节庆日期可能不同」重复挂上去。改成走 regional_variation 的场合
    # 语义，并且每轮只出一条合并后的说明。
    region = str(payload.get("recipient_region") or "").strip()
    if region:
        regional_notes = [n for n in _regional_notes({"recipient_region": region})
                          if n.get("topic") != "no_specific_match"]
        if regional_notes:
            scopes = list(dict.fromkeys(n["region"] for n in regional_notes))
            # 只保留真正「有具体地方差异」的那几条，元陈述（只说明来源范围）
            # 对用户没有信息量，丢掉。
            concrete = [n for n in regional_notes
                        if n.get("topic") not in {"document_scope", "festival_name"}]
            picked = (concrete or regional_notes)[:2]
            # 每条都带着「广东东源/客家语境：」的前缀，拼起来会重复四遍；
            # 去前缀，来源范围统一在括号里说一次。
            cleaned = []
            for n in picked:
                text = str(n.get("note") or "").strip()
                for scope in scopes:
                    if text.startswith(scope):
                        text = text[len(scope):].lstrip("：: ")
                if text:
                    cleaned.append(text)
            key = f"{region}地方习俗"
            if cleaned and key not in seen_keywords:
                seen_keywords.add(key)
                items.append({
                    "keyword": key,
                    "note": f"{'；'.join(cleaned)}（该条来源只覆盖{'、'.join(scopes)}，不代表全国）",
                    "source": "知识库地区条目",
                })

    sources = _taboo_sources_for(question_lower, limit=3)

    # 第三层：场合级底线提醒。taboos.csv 的礼仪词只有钟/伞/鞋/梨/刀剪几条，
    # 用户写「给长辈送生日礼物」这种自然描述时基本命中不到；occasion_rules.csv
    # 里按场合组织的那 100 条正好补这一层（春节避钟伞鞋梨、婚礼避刀剪……）。
    for item in _occasion_rule_items(payload):
        keyword = item["keyword"]
        if keyword in seen_keywords:
            continue
        seen_keywords.add(keyword)
        items.append(item)
        if len(items) >= 8:
            break

    if llm_used and llm_answer:
        note = re.sub(r"\*\*([^\*]+)\*\*", lambda m: m.group(1), str(llm_answer))
        note = re.sub(r"^#{1,4}\s*", "", note, flags=re.M)
        return {
            "status": "ai_found",
            "items": items,
            "ai_note": note.strip(),
            "sources": sources,
            "disclaimer": "以下提醒结合了知识库条目和通用送礼常识；地方习俗有家庭差异，最终以收礼人的习惯为准。",
        }
    if items:
        return {
            "status": "kb_found",
            "items": items,
            "ai_note": "",
            "sources": sources,
            "disclaimer": "以下来自 GiftRight 知识库的条目匹配；没列出的不代表绝对安全，地方习俗有家庭差异，最终以收礼人的习惯为准。",
        }
    # KB 没命中、LLM 也没给出有效内容：诚实兜底
    return {
        "status": "none",
        "items": [],
        "ai_note": "",
        "sources": [],
        "disclaimer": "知识库里没有查到和这个场景直接对应的忌讳条目，不代表完全没有——涉及食品、香味、材质和数字寓意时，建议先跟对方身边的朋友确认一下。",
    }


def _scene_taboo_question(payload, candidates=None):
    """把用户场景转成一句给礼物顾问的自然语言问题。

    candidates 默认不传。原因：推荐路径上顾问与 Dify 并行跑，顾问提问时
    Dify 还没返回，此时能拿到的只有本地预览候选，而页面最终展示的是
    Dify 排序后的候选——两者常常是不同的商品，顾问点评预览那几件、页面
    展示另外几件，用户看到的提醒和卡片对不上。等 Dify 回来再问又是串行，
    要多等十几秒。所以顾问只回答场景层面的讲究，不点评具体候选。
    """
    occasion = payload.get("occasion_custom") or payload.get("occasion_label") or "送礼"
    relation = payload.get("relation_custom") or payload.get("relation_label") or ""
    region = payload.get("recipient_region") or ""
    avoid = payload.get("avoid_custom") or ""
    parts = [f"给{relation}送{occasion}礼物".replace("送送礼", "送礼")]
    if region:
        parts.append(f"对方在{region}")
    question = "，".join(parts) + "，有什么忌讳、寓意和注意事项需要留意？"
    if avoid:
        question += f"（我已经确定不要：{avoid}）"
    # 候选摘要：最多列 3 件，让顾问的回答基于同一组候选。
    candidate_titles = []
    for card in (candidates or []):
        if not isinstance(card, dict):
            continue
        title = str(card.get("title") or card.get("name") or "").strip()
        if title:
            candidate_titles.append(title)
        if len(candidate_titles) >= 3:
            break
    if candidate_titles:
        question += f"系统目前推荐的礼物是：{'、'.join(candidate_titles)}，请对照这些推荐给出判断。"
    return question



def _final_hard_filter(candidates, payload, intent=None):
    """最终硬约束统一过滤（P0-1）。

    规则节点/本地预览已经在候选池层做过硬约束，但 Dify 返回的卡只经过
    「ID 必须出现在 preview 里」的白名单检查，且 Dify 卡缺少动态拼出的
    tags 字段（tags 由 derived_tags + taboo_flags 运行时拼接，不落 JSON），
    engine.evaluate_product 的 tags 路径会漏判。这里对最终展示集合再统一
    过一遍：engine 权威判定（预算/gift_mode/food_diy/fragrance_diy/偏好表/
    自定义避开）+ 文本兜底（香味/酒精/坚果，针对缺 tags 的卡）。

    注意：candidates 是「展示卡」（enrich/merge 后），price/cost 已被写成
    「约 179–219 元」这样的展示字符串，status 也常常缺失（preview 卡没有
    该字段，Dify 卡补的 status 多为 unknown）。直接把展示卡喂给
    evaluate_product 会在 _cost(item) > budget_max 这类数值比较上抛
    TypeError（字符串与 int 不可比），也会把缺 status 的卡误判成 inactive。
    所以这里先把展示卡映射回候选池行（原始 price/status/tags 都在行上），
    权威判定跑在行上；文本兜底（香味/酒精/坚果）仍查展示卡自身文本。

    返回 (kept, removed)。removed 元素为 (card, reason)。
    """
    if intent is None:
        intent = normalize_intent(payload)
    # 候选池行索引：与 enrich_dify_result 同源（本地 24 条池 + 实时目录 + KB 卡）。
    pool_rows = []
    try:
        pool_rows.extend(get_dify_candidates(payload, limit=DIFY_CANDIDATE_LIMIT))
    except Exception:
        pass
    pool_rows.extend(_load_real_catalog())
    try:
        pool_rows.extend(_load_kb_product_cards())
    except Exception:
        pass
    row_by_id = {}
    for row in pool_rows:
        if not isinstance(row, dict):
            continue
        for key in ("id", "product_id", "item_id", "offer_id"):
            value = str(row.get(key) or "")
            if value:
                row_by_id.setdefault(value, row)
    hard_prefs = set(intent.get("hard_preferences") or [])
    avoid_custom = str(intent.get("avoid_custom") or "").strip()
    avoid_text = avoid_custom

    # 触发文本兜底的条件（与 rule_node_final 的四路触发对齐）：
    # 1) hard_preferences 里的 avoid_fragrance / no_fragrance / fragrance_free
    # 2) avoid_custom 里含香/酒/坚果相关词
    # 3) accepts_fragrance_diy 为 False
    check_fragrance = bool(
        hard_prefs.intersection({"avoid_fragrance", "no_fragrance", "fragrance_free"})
        or any(
            word in avoid_text
            for word in ("香薰", "香氛", "香水", "香味", "香囊", "香包", "气味")
        )
        or intent.get("accepts_fragrance_diy") is False
    )
    check_alcohol = bool(
        hard_prefs.intersection({"non_alcoholic", "avoid_alcohol"})
        or any(word in avoid_text for word in ("酒", "酒精"))
    )
    check_nuts = bool(
        hard_prefs.intersection({"avoid_nuts", "allergy_free"})
        or any(word in avoid_text for word in ("坚果", "花生"))
    )

    kept = []
    removed = []
    for card in candidates:
        if not isinstance(card, dict):
            removed.append((card, "非对象候选"))
            continue
        # 权威判定：跑在候选池行上（原始数值/status/tags），展示卡只负责文本兜底。
        product_id = str(card.get("product_id") or "")
        row = row_by_id.get(product_id) or card
        # 候选池行能走到最终展示，说明它已在规则节点/预览层过活筛选；
        # status=''/unknown 是采集未标注，不是 inactive，按 rule_node_final
        # 的语义（active|unknown 皆可用）处理：统一置 active 再判，避免误杀。
        row_for_check = dict(row)
        row_for_check["status"] = "active"
        # 候选行 price 是字符串（'199.0'），cost 常为 None；evaluate_product 里
        # _cost() 直接返回原值参与数值比较，必须转成 float，否则字符串与 int
        # 比较抛 TypeError。
        _row_price = row_for_check.get("cost", row_for_check.get("price"))
        if not isinstance(_row_price, (int, float)):
            try:
                _row_price = float(str(_row_price).replace(",", "").strip() or "0")
            except (TypeError, ValueError):
                _row_price = None
        row_for_check["price"] = _row_price if _row_price is not None else 0
        row_for_check["cost"] = _row_price if _row_price is not None else 0
        hard_blocks, _soft = evaluate_product(row_for_check, intent)
        reasons = [message for _code, message in hard_blocks]

        # 文本兜底：Dify 卡/缺 tags 卡在 evaluate_product 的 tags 路径上漏判。
        # 只有对应约束被触发时才跑文本判定，避免误伤（例如商品标题恰好含
        # 「香」字但实际是木质摆件）。
        if check_fragrance and has_fragrance(card):
            reasons.append("命中明确禁忌：香味/香氛类商品（用户声明避开香味）")
        if check_alcohol and has_alcohol(card):
            reasons.append("命中明确禁忌：酒精/酒类商品（用户声明避开酒精）")
        if check_nuts and has_nuts(card):
            reasons.append("命中明确禁忌：坚果类商品（用户声明避开坚果）")

        if reasons:
            removed.append((card, "；".join(reasons)))
        else:
            kept.append(card)
    return kept, removed


def _rescue_ranker_from_trace(dify_result):
    """从节点 trace 里的 Ranker 原文恢复候选列表。

    背景（2026-11-17 实测）：约 20–33% 的请求 Dify 交回 candidates=[]。
    抓到的空结果里，`规则合并检查` 都正常给出 36 条 eligible_products，
    `LLM 2` 的原文也确实写出了 8 条完整候选（rank 1..8、strategy 合法、
    finish_reason=stop 没被截断），但紧随其后的 `代码执行 2` 吐出 0 条。
    原因是模型偶尔把 `"rank":5` 写成 `"rank":5"`，多一个引号让整段 JSON
    不合法，而那个代码节点的 parse_json 失败后静默返回空。

    所以候选不是没选出来，是卡在一道 JSON 解析上。这里拿 LLM 2 原文自己
    解一遍；解析不出来就返回 []，让上层照旧走「AI 这次没有选出」。
    """
    if not isinstance(dify_result, dict):
        return []
    trace = dify_result.get("_node_trace") or []
    raw_text = ""
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        if entry.get("node_id") != RANKER_NODE_ID:
            continue
        raw_map = entry.get("raw_by_key") or {}
        raw_text = raw_map.get("text") or ""
        break
    if not raw_text:
        return []

    parsed = None
    for attempt in (raw_text, _repair_stray_quote_after_number(raw_text)):
        cleaned = re.sub(
            r"<think>[\s\S]*?</think>", "", attempt or ""
        ).replace("```json", "").replace("```", "").strip()
        try:
            candidate = json.loads(cleaned)
        except (TypeError, ValueError):
            continue
        if isinstance(candidate, dict) and isinstance(
            candidate.get("candidates"), list
        ):
            parsed = candidate
            break
    if not parsed:
        return []

    rescued = []
    seen = set()
    for item in parsed.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "").strip()
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        card = dict(item)
        card["product_id"] = product_id
        # rank 可能被写成字符串，也可能缺失；按出现顺序补齐连续序号，
        # 页面按 rank 排序，缺号会让顺序乱掉。
        try:
            card["rank"] = int(str(item.get("rank")).strip())
        except (TypeError, ValueError):
            card["rank"] = len(rescued) + 1
        rescued.append(card)
        if len(rescued) >= 8:
            break
    return rescued


def merge_dify_with_preview(dify_result, payload, advisor=None):
    """把 Dify 排序结果与本地预览合并成最终页面数据。

    advisor：可选的顾问结果字典 {answer, ok}，由 _run_dify_job 在等 Dify 的
    同时并行取回。传进来就把它交给场景提醒做「AI 综合提醒」；不传（None）
    就退回纯知识库版本。早先推荐路径完全不调顾问，场景提醒永远只有 KB 层，
    ai_note 恒为空——那是 P1 性能优化时主动移除的串行调用，不是遗漏。
    """
    # 这里必须拿内层结果：build_fast_preview 会把 _overflow_eligible_cards
    # 剥掉，而合并正好需要它来兜底查找 Dify 选中但未进前 30 条的卡。
    # 内层结果同样要过一遍卡片规范化，否则 preview 卡的 is_llm_copy
    # 会停在 enrich 强写的 True 上，备选卡就被当成模型文案渲染了。
    preview = _normalize_preview_cards(build_fast_preview(payload))
    # Dify 这一轮到底交回几条候选。必须在循环外先记下来——循环结束后
    # dify_result["candidates"] 会被合并结果覆盖，那时再读就是假数字。
    _dify_raw_candidates = [
        c for c in (dify_result.get("candidates") or []) if isinstance(c, dict)
    ]
    _dify_raw_count = len(_dify_raw_candidates)
    preview_cards = [
        card
        for card in preview.get("candidates", [])
        if isinstance(card, dict) and card.get("product_id")
    ]
    preview_by_id = {
        str(card["product_id"]): card
        for card in preview_cards
    }
    # Dify 的候选池比页面展示池大（36 vs 30）。它选中被截断的那几条时，
    # 上面这张表查不到，会误判成「不在本地池」而丢弃。把截断前的合格卡
    # 并进来做兜底查找——它们同样过了本地硬约束，只是没进前 30 条展示。
    _overflow = preview.get("_overflow_eligible_cards") or []
    _overflow_by_id = {}
    for _card in _overflow:
        if not isinstance(_card, dict):
            continue
        _pid = str(_card.get("product_id") or "")
        if _pid and _pid not in preview_by_id:
            _overflow_by_id[_pid] = _card
            preview_by_id[_pid] = _card
    selected = []
    # 2026-11-17：偶发过一次 dify_ranked_candidates=0（模型这次没给排序），
    # 用户看到的是「AI 这次没有选出」。要区分「模型没选」和「模型选了但被
    # 本地合并丢掉」——两者的修法完全不同，所以把丢弃原因记进 decision_trace。
    _dropped_by_merge = []
    for card in dify_result.get("candidates", []):
        if not isinstance(card, dict):
            continue
        product_id = str(card.get("product_id") or "")
        preview_card = preview_by_id.get(product_id)
        if not preview_card:
            # Dify is never allowed to re-introduce a product that failed the
            # local budget and hard-constraint filter.
            _dropped_by_merge.append(
                {
                    "product_id": product_id,
                    "title": card.get("title") or card.get("name") or "",
                    "reason": "not_in_local_preview_pool",
                }
            )
            continue

        # Keep Dify's explanation/ranking fields, but restore all factual
        # display fields from the locally filtered preview. This prevents a
        # stale or hallucinated Dify price such as "约 68 元" from appearing
        # when the user selected a 100–300 yuan budget.
        merged = dict(card)
        for field in (
            "title",
            "gift_type",
            "gift_type_label",
            "category",
            "price",
            "price_band",
            "price_note",
            "cost",
            "delivery_days",
            "description",
            "source",
            "source_url",
            "materials",
            "steps",
            "difficulty",
            "preservation",
            "allergens",
            "fulfillment_label",
            "fulfillment_estimate",
            "planning_label",
            "region_note",
            "volatility_reasons",
            "watch_out",
        ):
            if field in preview_card:
                merged[field] = preview_card[field]
        # 备选卡（不该有 LLM 文案）混进 selected 时以 preview 的为准：
        # LLM 文案字段不在上面的覆盖清单里，这里补一道来源标记。
        # 默认 False：文案标记漏了就按「非模型文案」处理，宁可少挂一块，
        # 也不能给规则卡套上模型背书。
        merged.setdefault("is_llm_copy", False)
        merged.setdefault("copy_source", "template")
        selected.append(merged)
    selected_ids = {str(card["product_id"]) for card in selected}
    remaining = []

    for card in preview_cards:
        product_id = str(card.get("product_id") or "")
        if product_id and product_id not in selected_ids:
            card["strategy"] = "practical"
            card["strategy_label"] = "也可以"
            # 用户要求：本地筛选（非 LLM 选出）的卡只保留注意事项，
            # 寓意/理由是模型生成的文案，备选卡没有模型背书就不展示。
            card["meaning"] = ""
            card["why_this"] = ""
            card["reason_evidence"] = []
            card["is_llm_copy"] = False
            remaining.append(card)

    # 被展示上限截断、但 Dify 真的选中的卡：它们已经在 selected 里（上面
    # preview_by_id 兜底查到了），这里只记一笔便于排查，不重复加入。
    _promoted = [
        {
            "product_id": str(card.get("product_id") or ""),
            "title": card.get("title") or "",
        }
        for card in selected
        if str(card.get("product_id") or "") in _overflow_by_id
    ]
    if _promoted:
        dify_result.setdefault("decision_trace", {}).setdefault(
            "filter_steps", {}
        )["promoted_from_overflow"] = _promoted

    # P0-1 最终硬约束统一过滤：Dify 排序结果 + preview 备选合并后，
    # 对最终展示集合再过一遍权威硬约束。Dify 卡缺 tags（tags 是运行时
    # 拼接的），evaluate_product 的 tags 路径会漏判，所以补文本兜底
    # （香味/酒精/坚果）。被剔除的卡记录进 decision_trace，供前端提示
    # 「这件因新筛选结果已不再推荐」。
    merged_all = selected + remaining
    merged_kept, merged_removed = _final_hard_filter(
        merged_all, payload, intent=normalize_intent(payload)
    )
    if merged_removed:
        dify_result.setdefault("decision_trace", {}).setdefault(
            "filter_steps", {}
        )["final_hard_removed"] = [
            {
                "product_id": str(card.get("product_id") or ""),
                "title": card.get("title") or card.get("name") or "",
                "reason": reason,
            }
            for card, reason in merged_removed
        ]
    merged_all = merged_kept

    for index, card in enumerate(merged_all, start=1):
        card["rank"] = index

    dify_result["candidates"] = merged_all
    preview_rule_result = preview.get("rule_result") or {}
    # 页底统一提醒：所有卡片共有的价格口径/时效说明收拢到这里，
    # 卡片上不再逐张重复同一段免责话。时效警告来自本地规则节点，
    # 所以要把 preview 的 soft_warnings / route_estimate 一起传进去。
    dify_result["page_notices"] = _build_page_notices(
        payload, dify_result["candidates"],
        rule_warnings=preview_rule_result.get("soft_warnings") or [],
        route_estimate=preview.get("route_estimate"),
    )
    dify_result.setdefault("route_estimate", preview.get("route_estimate"))
    dify_rule_result = dify_result.setdefault("rule_result", {})
    for key in (
        "eligible_product_ids",
        "hard_blocks",
        "soft_warnings",
        "excluded_summary",
        "rules_version",
    ):
        if key in preview_rule_result:
            dify_rule_result[key] = preview_rule_result[key]
    dify_result.setdefault("decision_trace", {}).setdefault(
        "filter_steps",
        {},
    )["displayed_eligible_candidates"] = len(dify_result["candidates"])
    dify_result["decision_trace"]["dify_ranked_candidates"] = len(selected)
    if _dropped_by_merge:
        dify_result["decision_trace"]["dropped_by_merge"] = _dropped_by_merge
    # 2026-11-17：实测 8 次里有 2 次 Dify 返回空 candidates（约 25%），
    # 用户看到的是「AI 这次没有选出」。这里区分三种成因，否则无从下手：
    #   0 → 模型这轮没给排序（上游 LLM 问题）
    #   >0 且全被丢 → 模型给了 product_id，但和本地 preview 池对不上
    #   >0 且有保留 → 正常
    if not selected:
        # 注意：dify_result["candidates"] 此时已经被合并结果覆盖（就是剩下的
        # preview 卡），直接读它会得到「30 条候选、0 匹配」这种自相矛盾的
        # 假数字。真实入口数量必须在合并开始时抓（_dify_raw_candidates）。
        dify_result["decision_trace"]["empty_rank_diagnosis"] = {
            "raw_candidates_from_dify": _dify_raw_count,
            "local_preview_pool": len(preview_cards),
            "matched": len(selected),
            # 对不上时最要紧的是看 ID 长什么样：是空串、是 DIY id、还是
            # 真·别的商品。只取前 5 条样本，避免 trace 膨胀。
            "raw_ids_sample": [
                str(c.get("product_id") or "") for c in _dify_raw_candidates[:5]
            ],
            "raw_keys_sample": (
                sorted(_dify_raw_candidates[0].keys())
                if _dify_raw_candidates
                else []
            ),
        }
    # 场景提醒分两层：知识库条目在下、AI 综合提醒在上（ai_note 单独渲染，
    # 不覆盖 items）。advisor 由 _run_dify_job 在等 Dify 的同时并行取回，
    # 所以这里不额外付出串行等待；advisor 为空（未配置/超时/失败）就退回
    # 纯知识库版本，页面结构不变，只是少了那段综合提醒。
    advisor_answer = ""
    if isinstance(advisor, dict) and advisor.get("ok"):
        advisor_answer = str(advisor.get("answer") or "").strip()
    if advisor_answer:
        dify_result["scene_taboo_summary"] = _scene_taboo_summary(
            payload, llm_answer=advisor_answer, llm_used=True
        )
    else:
        dify_result.setdefault("scene_taboo_summary", _scene_taboo_summary(payload))
    return dify_result


def _cleanup_dify_jobs():
    cutoff = time.time() - DIFY_JOB_TTL_SECONDS
    with DIFY_JOBS_LOCK:
        expired = [
            job_id
            for job_id, job in DIFY_JOBS.items()
            if job.get("created_at", 0) < cutoff
        ]
        for job_id in expired:
            DIFY_JOBS.pop(job_id, None)


def _advisor_worker(question, context, box):
    """在后台线程里问一次礼物顾问，结果塞进 box。

    独立成函数而不是用 lambda：异常必须在子线程里吃掉，否则线程静默死掉、
    主线程 join 拿到空 box，页面上表现为「AI 提醒偶尔消失」这种难查的现象。
    """
    try:
        reply = answer_gift_chat(question, context)
    except Exception as error:  # 顾问失败不能拖垮推荐主流程
        box["error"] = str(error)
        box["ok"] = False
        return
    answer = str((reply or {}).get("answer") or "").strip()
    # 拒答不算有效提醒：那段「我只回答送礼相关问题」放上去是噪音。
    box["ok"] = bool(answer) and not (reply or {}).get("refusal")
    box["answer"] = answer


def _run_dify_job(job_id, payload):
    _t_start = time.time()
    _timing = {"local_prefilter_ms": 0}  # 初筛在 /api/recommend 请求路径已完成
    try:
        # 顾问与 Dify 并行：顾问要等 10~20 秒，放在 Dify 后面串行会把它整段
        # 加到用户等待时间上（审计实测约 18 秒）。这里在发起 Dify 之前就起
        # 线程，墙钟时间取两者较大值，等于顾问白送。
        #
        # 问题里**不带候选清单**。曾经带过（用本地预览候选），结果顾问点评的
        # 是预览那三件、页面展示的是 Dify 排序后的另外三件，两边对不上——比
        # 不提醒更糟。而等 Dify 回来再问就成了串行，白拿 18 秒。所以顾问只
        # 回答场景本身（关系/场合/地域/已定避开项），它对哪件礼物都成立；
        # 单件商品的注意事项由卡片上的 watch_out / taboo_notes 承担。
        advisor_box = {"ok": False, "answer": "", "error": None}
        advisor_thread = None
        if dify_chat_is_configured():
            try:
                advisor_question = _scene_taboo_question(payload, candidates=None)
                advisor_context = {
                    "occasion": payload.get("occasion_custom") or payload.get("occasion_label") or "",
                    "relation": payload.get("relation_custom") or payload.get("relation_label") or "",
                    "recipient_region": payload.get("recipient_region") or "",
                    "avoid_custom": payload.get("avoid_custom") or "",
                }
                advisor_thread = threading.Thread(
                    target=_advisor_worker,
                    args=(advisor_question, advisor_context, advisor_box),
                    name="giftright-advisor",
                    daemon=True,
                )
                advisor_thread.start()
            except Exception:
                # 起线程都失败就当作没有顾问，推荐照常走。
                advisor_thread = None
        _t_workflow_start = time.time()
        dify_result = maybe_call_dify(payload)
        _t_workflow_end = time.time()
        _timing["dify_workflow_ms"] = round((_t_workflow_end - _t_workflow_start) * 1000)
        # Dify 响应自身带的 elapsed_time（若有）原样透出，供对照墙钟耗时。
        _dify_elapsed = None
        if isinstance(dify_result, dict):
            for _candidate in (
                dify_result.get("data", {}).get("elapsed_time"),
                dify_result.get("elapsed_time"),
            ):
                if _candidate is not None:
                    _dify_elapsed = _candidate
                    break
        if _dify_elapsed is not None:
            _timing["dify_elapsed_time_s"] = _dify_elapsed
        if dify_result is None:
            with DIFY_JOBS_LOCK:
                DIFY_JOBS[job_id] = {
                    "status": "failed",
                    "error": DIFY_LAST_ERROR or {
                        "code": "request_failed",
                        "message": "Dify 请求未返回结果",
                    },
                    "updated_at": time.time(),
                }
            return

        normalized = normalize_dify_response(dify_result)
        if normalized is None:
            # A malformed Card Writer response must not erase the valid local
            # recommendation or surface a blocking browser alert. Keep the
            # local rule result visible and record the remote format issue.
            fallback = build_fast_preview(payload)
            with DIFY_JOBS_LOCK:
                DIFY_JOBS[job_id] = {
                    "status": "succeeded",
                    "payload": {
                        "source": "local_rule_mode",
                        "dify_status": "invalid_structured_output_fallback",
                        "dify_error": {
                        "code": "invalid_structured_output",
                        "message": "Dify 已完成，但最终输出不符合页面数据契约",
                        },
                        "result": fallback,
                    },
                    "updated_at": time.time(),
                }
            return

        normalized = enrich_dify_result(normalized, payload, llm_selected=True)
        # 2026-11-17：Dify 交回空候选时，若能从节点 trace 里拿到 Ranker
        # （LLM 2）的原文，就地补一次排序。根因是模型偶发把 `"rank":5` 写成
        # `"rank":5"`，Dify 的「代码执行 2」解析失败后静默吐空列表——候选
        # 其实一直在，只是没通过那道 JSON 解析。这里不依赖改 Dify 节点，
        # 也不改变任何筛选规则：补出来的候选照样要过 merge_dify_with_preview
        # 的本地预算/硬约束比对，进不来的会被记进 dropped_by_merge。
        _rescued_from = None
        if not (normalized.get("candidates") or []):
            _rescued = _rescue_ranker_from_trace(dify_result)
            if _rescued:
                normalized["candidates"] = _rescued
                normalized = enrich_dify_result(normalized, payload, llm_selected=True)
                _rescued_from = "ranker_node_trace"
        if not (normalized.get("candidates") or []):
            try:
                _dbg = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "payload": payload,
                    "outputs": (dify_result or {}).get("data", {}).get("outputs"),
                    "node_trace": (dify_result or {}).get("_node_trace"),
                    "elapsed_time": _dify_elapsed,
                }
                with open(
                    _EMPTY_DUMP_PATH, "a", encoding="utf-8"
                ) as _fh:
                    _fh.write(json.dumps(_dbg, ensure_ascii=False) + "\n")
            except Exception:
                pass
        elif _rescued_from:
            # 抢救成功也要留痕：否则「这次没空」和「这次本来要空、被救回来」
            # 在日志里长得一样，故障率会被悄悄抹掉。
            _dt = normalized.get("decision_trace")
            if isinstance(_dt, dict):
                _dt["ranker_rescue"] = {
                    "source": _rescued_from,
                    "recovered": len(normalized.get("candidates") or []),
                }
        # 收顾问结果。Dify 通常要 30 秒以上，顾问多半已经跑完，join 几乎
        # 立即返回；只有顾问比 Dify 还慢时才真的等，且上限 5 秒——超过就
        # 放弃这段提醒，不能让页首那块把整页拖住。
        if advisor_thread is not None:
            _t_advisor_wait = time.time()
            advisor_thread.join(timeout=5)
            _timing["advisor_wait_ms"] = round((time.time() - _t_advisor_wait) * 1000)
            if advisor_thread.is_alive():
                advisor_box["ok"] = False
                _timing["advisor_timeout"] = True
            # 诊断：顾问这条链最容易出的问题是「没报错但也没内容」，页面上
            # 只表现为场景提醒退回 KB 版，查起来没有线索。这里把结论记进
            # timing，随 /api/recommend/status 一起透出。
            _timing["advisor_ok"] = bool(advisor_box.get("ok"))
            _timing["advisor_len"] = len(str(advisor_box.get("answer") or ""))
            if advisor_box.get("error"):
                _timing["advisor_error"] = str(advisor_box["error"])[:200]
        normalized = merge_dify_with_preview(normalized, payload, advisor=advisor_box)
        _timing["result_processing_ms"] = round((time.time() - _t_workflow_end) * 1000)
        _timing["total_ms"] = round((time.time() - _t_start) * 1000)
        # A Dify workflow can legally return an empty ranking because of a
        # stale mapping, rate limit, or an over-strict remote rule. Never let
        # that asynchronous response erase the already-valid local preview.
        if not normalized.get("candidates"):
            fallback = build_fast_preview(payload)
            with DIFY_JOBS_LOCK:
                DIFY_JOBS[job_id] = {
                    "status": "succeeded",
                    "payload": {
                        "source": "local_rule_mode",
                        "dify_status": "empty_result_fallback",
                        "result": fallback,
                    },
                    "updated_at": time.time(),
                }
            return
        with DIFY_JOBS_LOCK:
            DIFY_JOBS[job_id] = {
                "status": "succeeded",
                "payload": {
                    "source": "dify",
                    # 前端 renderResults 只读 payload.result 存进 state.result，
                    # 而分区的「AI 正在挑选中 / AI 这次没选出」话术靠
                    # state.result.dify_status 判断——成功路径必须把这个字段
                    # 带上，否则永远停在「running」态的判断里。
                    "dify_status": "succeeded",
                    "result": normalized,
                    "timing": _timing,
                },
                "updated_at": time.time(),
            }
    except Exception as error:
        with DIFY_JOBS_LOCK:
            DIFY_JOBS[job_id] = {
                "status": "failed",
                "error": {
                    "code": "background_dify_error",
                    "message": str(error),
                },
                "updated_at": time.time(),
            }


def start_dify_job(payload):
    if not dify_is_configured():
        return None
    _cleanup_dify_jobs()
    job_id = "dify-" + uuid.uuid4().hex[:12]
    with DIFY_JOBS_LOCK:
        DIFY_JOBS[job_id] = {
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    thread = threading.Thread(
        target=_run_dify_job,
        args=(job_id, dict(payload)),
        daemon=True,
    )
    thread.start()
    return job_id


def _access_cookie_value():
    """cookie 里存的值：口令的 HMAC 摘要，恒为 ASCII 十六进制。

    不能直接存 ACCESS_TOKEN —— HTTP 头只能编码 latin-1，
    口令带中文（模板默认就是中文）会让 send_header 抛 UnicodeEncodeError。
    """
    if not ACCESS_TOKEN:
        return ""
    return hmac.new(
        ACCESS_TOKEN.encode("utf-8"), b"giftright-access", "sha256"
    ).hexdigest()


def _token_matches(supplied):
    """口令比对。必须转成 bytes 再比。

    2026-11-18：原来直接 hmac.compare_digest(str, str)，口令里一旦有中文
    （默认模板就是「改成你的口令」）就抛 TypeError:
    "comparing strings with non-ASCII characters is not supported"，
    请求线程直接崩、连接被关，浏览器看到的是空白页而不是 401。
    转成 UTF-8 bytes 后 compare_digest 正常，且仍保持恒定时间比较。

    另外同时接受 cookie 摘要（见 _access_cookie_value），
    这样浏览器第二次访问不必再带 ?token=。
    """
    if not supplied or not ACCESS_TOKEN:
        return False
    try:
        raw = supplied.encode("utf-8")
        return hmac.compare_digest(raw, ACCESS_TOKEN.encode("utf-8")) or \
            hmac.compare_digest(raw, _access_cookie_value().encode("ascii"))
    except (TypeError, AttributeError, UnicodeError):
        return False


def _access_token_from(headers):
    """从 header 或 cookie 取口令。"""
    if not ACCESS_TOKEN:
        return ""
    token = (headers.get("X-GiftRight-Token") or "").strip()
    if token:
        return token
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == ACCESS_COOKIE:
            return value.strip()
    return ""


def _client_ip(handler):
    """取真实客户端 IP。Fly/Cloudflare 会放在 Fly-Client-IP / X-Forwarded-For。"""
    for name in ("Fly-Client-IP", "CF-Connecting-IP", "X-Real-IP"):
        value = (handler.headers.get(name) or "").strip()
        if value:
            return value
    forwarded = (handler.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.client_address[0] if handler.client_address else "unknown"


def _rate_limited(ip, limit_per_min, cost=1):
    """固定窗口限流：返回 True 表示该拒绝。"""
    if limit_per_min <= 0:
        return False
    now = time.time()
    window = int(now // 60)
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.get(ip)
        if not bucket or bucket[0] != window:
            bucket = [window, 0]
            _RATE_BUCKETS[ip] = bucket
        bucket[1] += cost
        if len(_RATE_BUCKETS) > 4096:
            for key in [k for k, v in _RATE_BUCKETS.items() if v[0] != window]:
                _RATE_BUCKETS.pop(key, None)
        return bucket[1] > limit_per_min


def _access_failed(ip):
    """记一次口令失败，返回是否已经超限。"""
    with _ACCESS_FAILS_LOCK:
        window = int(time.time() // 300)
        record = _ACCESS_FAILS.get(ip)
        if not record or record[0] != window:
            record = [window, 0]
            _ACCESS_FAILS[ip] = record
        record[1] += 1
        return record[1] > ACCESS_MAX_FAILS


class GiftRightHandler(BaseHTTPRequestHandler):
    @staticmethod
    def _strip_private(value):
        """递归去掉下划线开头的内部键，内部数据不进 API 响应。"""
        if isinstance(value, dict):
            return {
                k: GiftRightHandler._strip_private(v)
                for k, v in value.items()
                if not (isinstance(k, str) and k.startswith("_"))
            }
        if isinstance(value, list):
            return [GiftRightHandler._strip_private(v) for v in value]
        return value

    def _send_json(self, data, status=200):
        encoded = json.dumps(
            self._strip_private(data), ensure_ascii=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_file(self, path):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".svg": "image/svg+xml; charset=utf-8",
        }
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # 2026-11-19：以前这里一个缓存头都不发，浏览器按启发式规则自行缓存
        # app.js / styles.css。改了前端重启服务，用户看到的还是旧文件，
        # 只能靠 Ctrl+F5 才生效 —— 改了半天「看起来没变化」就是这么来的。
        # 静态文件很小（最大 66KB），每次都回源校验的代价远低于「改了看不到」。
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_error(405)

    def _check_access(self, is_write=False):
        """返回 True 表示可以继续；否则已写好响应，调用方直接 return。

        不设 ACCESS_TOKEN 时永远放行（本地模式）。
        """
        if not ACCESS_TOKEN:
            return True
        ip = _client_ip(self)
        provided = _access_token_from(self.headers)
        if _token_matches(provided):
            if is_write and _rate_limited(ip, RATE_LIMIT_PER_MIN):
                self._send_error_payload(
                    "rate_limited",
                    "这一分钟请求太多了，歇一下再试。",
                    429,
                )
                return False
            return True
        if _access_failed(ip):
            self._send_error_payload(
                "too_many_attempts",
                "口令错太多次了，等五分钟再试。",
                429,
            )
            return False
        self._send_error_payload("unauthorized", "需要访问口令", 401)
        return False

    def do_GET(self):
        # 口令引导：浏览器没法带自定义 header，所以允许用 /?token=xxx 校验一次，
        # 通过后种 cookie 再跳回 /。这样 web/ 下的前端一行都不用改。
        if ACCESS_TOKEN:
            query = parse_qs(urlparse(self.path).query)
            if "token" in query:
                supplied = (query.get("token") or [""])[0]
                if _token_matches(supplied):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    # cookie 里放摘要而不是原文：Set-Cookie 只能写 latin-1，
                    # 口令带中文时直接 UnicodeEncodeError。摘要恒为 ASCII 十六进制，
                    # 顺带也不再让原始口令出现在浏览器可读的存储里。
                    self.send_header(
                        "Set-Cookie",
                        f"{ACCESS_COOKIE}={_access_cookie_value()}; Path=/; "
                        "HttpOnly; SameSite=Lax; Max-Age=2592000",
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if _access_failed(_client_ip(self)):
                    self._send_error_payload(
                        "too_many_attempts", "口令错太多次了，等五分钟再试。", 429
                    )
                    return
                self._send_error_payload("unauthorized", "口令不对", 401)
                return
        # 静态页面本身放行（要能打开），/api/* 需要口令。
        if self.path.startswith("/api/") and self.path != "/api/health":
            if not self._check_access():
                return
        if self.path.startswith("/api/recommend/status"):
            query = parse_qs(urlparse(self.path).query)
            job_id = (query.get("job_id") or [""])[0]
            with DIFY_JOBS_LOCK:
                job = dict(DIFY_JOBS.get(job_id) or {})
            if not job:
                self._send_json(
                    {
                        "status": "missing",
                        "error": {
                            "code": "job_not_found",
                            "message": "Dify 后台任务不存在或已过期",
                        },
                    },
                    status=404,
                )
                return
            # 2026-11-16：任务在跑的时候把已等待秒数透出来。
            # 之前前端只有一个「正在检查约束」的死文案，Dify 跑 40~90 秒
            # 完全看不出来它是在干活还是卡死了，用户只能干等到以为它挂了。
            if job.get("status") == "running" and job.get("created_at"):
                job["elapsed_s"] = round(time.time() - job["created_at"], 1)
            self._send_json(job)
            return

        if self.path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "version": APP_VERSION,
                    "mode": "dify_enabled" if dify_is_configured() else "local_rule_mode",
                    "dify": {
                        "configured": dify_is_configured(),
                        "chat_configured": dify_chat_is_configured(),
                        "config_problem": dify_config_problem(),
                        "max_retries": DIFY_MAX_RETRIES,
                        "last_error": DIFY_LAST_ERROR,
                    },
                }
            )
            return
        if self.path == "/api/research":
            if not EXPOSE_RESEARCH:
                self._send_error_payload("not_found", "接口不存在", 404)
                return
            if not EXPERIMENT_PATH.exists():
                self._send_json(
                    {"error": {"code": "research_not_ready", "message": "实验结果尚未生成"}},
                    status=404,
                )
                return
            self._send_json(json.loads(EXPERIMENT_PATH.read_text(encoding="utf-8")))
            return
        if self.path == "/":
            self._send_file(WEB_ROOT / "index.html")
            return
        requested = (self.path.split("?", 1)[0]).lstrip("/")
        if requested in {
            "app.js",
            "styles.css",
            "hero_pixel_workshop.png",
            "intro_pixel_gift.png",
            "hero_craft_worktable.jpg",
            "intro_craft_wrapping.jpg",
            "gift-wrapping-worktable.jpg",
            "pixel_intro_gift.svg",
            "pixel_hero_workshop.svg",
        }:
            self._send_file(WEB_ROOT / requested)
            return
        self.send_error(404)

    def _send_error_payload(self, code, message, status):
        """One error shape everywhere: {"error": {code, message}}."""
        self._send_json(
            {"error": {"code": code, "message": message}},
            status=status,
        )

    def do_POST(self):
        if self.path not in {"/api/recommend", "/api/chat", "/api/chat/stream"}:
            self._send_error_payload("not_found", "接口不存在", 404)
            return
        # 三个接口都烧 Dify 额度，统一走口令 + 限流。
        if not self._check_access(is_write=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error_payload("invalid_request", "请求格式无效", 400)
            return
        if length <= 0:
            self._send_error_payload("empty_body", "请求体不能为空", 400)
            return
        if length > MAX_REQUEST_BODY_BYTES:
            self._send_error_payload("body_too_large", "请求体过大", 413)
            return
        try:
            payload = _parse_api_payload(self.rfile.read(length))
        except ValueError as error:
            status = 413 if str(error) == "请求体过大" else 400
            self._send_error_payload(
                "body_too_large" if status == 413 else "invalid_request",
                str(error),
                status,
            )
            return

        if self.path == "/api/chat":
            self._send_json(answer_gift_chat(payload.get("question"), payload.get("context")))
            return

        if self.path == "/api/chat/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in _dify_chat_stream_events(payload.get("question"), payload.get("context")):
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        local_result = build_fast_preview(payload)
        job_id = start_dify_job(payload)
        response = {
            "source": "local_rule_mode",
            "dify_status": "running" if job_id else "not_configured",
            "dify_job_id": job_id,
            "result": local_result,
        }
        # 2026-11-18：没起后台任务时要把原因带出来。原来只有
        # dify_status="not_configured" 一个状态字，前端拿不到 dify_error
        # 就不显示任何提示，密钥没填这件事在页面上完全看不出来。
        if not job_id:
            problem = dify_config_problem()
            if problem:
                response["dify_error"] = {
                    "code": "placeholder_key",
                    "message": problem,
                }
        self._send_json(response)

    def log_message(self, fmt, *args):
        # 2026-11-16：加时间戳。之前日志只有请求行没有时间，
        # 「用户等了五分钟」这种事只能靠 updated_at 反推，查不准。
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[GiftRight {stamp}] {self.address_string()} - {fmt % args}", flush=True)


if __name__ == "__main__":
    # 默认只监听本机；云端用 GIFT_RIGHT_HOST=0.0.0.0 让容器外能访问。
    HOST = os.getenv("GIFT_RIGHT_HOST", "127.0.0.1").strip() or "127.0.0.1"
    server = ThreadingHTTPServer((HOST, PORT), GiftRightHandler)
    print(f"GiftRight running at http://{HOST}:{PORT}")
    print("Mode:", "Dify API" if os.getenv("DIFY_API_KEY") else "local rule mode")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()
