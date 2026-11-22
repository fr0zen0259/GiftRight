import csv
import json
import re
import uuid
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PRODUCTS_PATH = ROOT / "data" / "products.json"
DIY_GIFTS_PATH = ROOT / "data" / "diy_gifts.json"
RULES_PATH = ROOT / "data" / "rules.json"
TABOOS_PATH = ROOT / "data" / "real_rules" / "taboos.csv"
REGIONAL_PATH = ROOT / "data" / "real_rules" / "regional_variation_100.csv"
OCCASION_RULES_PATH = ROOT / "data" / "real_rules" / "occasion_rules.csv"


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_csv(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


PRODUCTS = load_json(PRODUCTS_PATH)
DIY_GIFTS = load_json(DIY_GIFTS_PATH) if DIY_GIFTS_PATH.exists() else []
CATALOG = PRODUCTS + DIY_GIFTS
RULES = load_json(RULES_PATH)
TABOOS = load_csv(TABOOS_PATH)
REGIONAL_VARIATIONS = load_csv(REGIONAL_PATH)
# occasion_rules.csv（100 条场合规则，带 forbidden_flags + source_url）之前
# 只在 build_* 脚本里被读过，运行时从没加载——所以「春节避开钟伞鞋梨」这类
# 场合级底线提醒在页面上从来不出现，只能靠用户自己把「手表」写进描述里碰运气。
OCCASION_RULES = load_csv(OCCASION_RULES_PATH)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _number(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_intent(raw):
    raw = raw or {}
    budget_min = _number(raw.get("budget_min"))
    budget_max = _number(raw.get("budget_max"))
    hard_preferences = _as_list(raw.get("hard_preferences"))
    soft_preferences = _as_list(raw.get("soft_preferences"))
    free_text = str(raw.get("free_text") or "").strip()
    avoid_custom = str(raw.get("avoid_custom") or "").strip()

    unknown_notes = list(raw.get("unknown_notes") or [])
    if not raw.get("deadline") and raw.get("days_left") is None:
        unknown_notes.append("未提供明确到货或制作截止日期，时效未校验")
    if budget_max is None:
        unknown_notes.append("未提供明确预算上限")
    if not raw.get("occasion_custom") and not raw.get("occasion"):
        unknown_notes.append("未提供明确场合")
    if not raw.get("relation_custom") and not raw.get("recipient_relation"):
        unknown_notes.append("未提供明确关系")
    if avoid_custom:
        unknown_notes.append("已保留用户自定义避开内容，并参与候选过滤")

    if any(token in free_text for token in ("可能不喜欢香味", "不确定是否喜欢香味", "怕她不喜欢香味")):
        soft_preferences.append("avoid_fragrance_uncertain")

    gift_mode = raw.get("gift_mode") or "all"
    if gift_mode not in {"all", "purchased", "diy"}:
        gift_mode = "all"

    return {
        "occasion": raw.get("occasion") or "other",
        "occasion_label": raw.get("occasion_label") or raw.get("occasion_custom") or "未指定场合",
        "occasion_custom": str(raw.get("occasion_custom") or "").strip(),
        "recipient_relation": raw.get("recipient_relation") or "other",
        "relation_label": raw.get("relation_label") or raw.get("relation_custom") or "未指定关系",
        "relation_custom": str(raw.get("relation_custom") or "").strip(),
        "budget_min": budget_min,
        "budget_max": budget_max,
        "deadline": raw.get("deadline") or None,
        "days_left": _number(raw.get("days_left")),
        "hard_preferences": sorted(set(hard_preferences)),
        "soft_preferences": sorted(set(soft_preferences)),
        "free_text": free_text,
        "avoid_custom": avoid_custom,
        "user_region": str(raw.get("user_region") or "").strip(),
        "recipient_region": str(raw.get("recipient_region") or "").strip(),
        "gift_mode": gift_mode,
        "accepts_food_diy": raw.get("accepts_food_diy", True) is not False,
        "accepts_fragrance_diy": raw.get("accepts_fragrance_diy", True) is not False,
        "unknown_notes": sorted(set(unknown_notes)),
        "input_confidence": raw.get("input_confidence") or "medium",
    }


def _lead_days(item):
    return item.get("prep_days", item.get("delivery_days", 999))




def _cost(item):
    return item.get("cost", item.get("price", 0))


def _has(item, tag):
    return tag in item.get("tags", [])


def _item_type(item):
    return item.get("gift_type", "purchased")


def _item_text(item):
    """把商品的全部可判定文字拼成一段，供文本兜底匹配。

    真实商品池（data/real_catalog/*.json，4180 条）只有 title / title_key，
    没有 name 字段——早先这里只读 name，导致真实池拼出来是空串，所有
    文本兜底（含自定义避开项）在真实商品上静默失效。title/title_key 必须
    一起读，二者一个是营销原题、一个是清洗后短标题。
    """
    tags = item.get("tags") or item.get("derived_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    fields = [
        item.get("name", ""),
        item.get("title", ""),
        item.get("title_key", ""),
        item.get("category", ""),
        item.get("description", ""),
        item.get("risk_notes", ""),
        item.get("raw_text", ""),
        " ".join(str(tag) for tag in tags),
    ]
    return " ".join(str(field) for field in fields if field).lower()


def _reference_band(relation):
    return RULES["reference_price_bands"].get(
        relation, RULES["reference_price_bands"]["friend"]
    )




def _custom_avoid_blocks(item, avoid_text):
    """用户自定义避开项。

    三条路径，任一命中即拦：
    1) tag 匹配——只对本地目录（32 条，带 tags）有效；
    2) 品类匹配——真实商品池的 category 字段（家居日用/香氛/首饰…）；
    3) 标题匹配——只在标题「靠前位置」找落地词，避免把标题里顺带提到的
       东西当成商品本身。
    早先只有 tag 路径，真实池一条 tag 都没有，自定义避开项在真实商品上
    等于完全没生效。

    为什么标题不能整串搜：真实池里「避开鞋」会把「招财猫玄关钥匙收纳
    摆件（入户鞋柜创意装饰）」拦掉，「避开刀」会拦掉「海蓝之谜紧塑刀面
    颈膜礼盒」和「化妆刷（晕染刀锋刷）」。这些标题只是顺带提了那个字，
    商品本身不是鞋也不是刀。所以标题路径只认前 HEAD_POS 个字符内出现的
    落地词，且要求该词后面不紧跟修饰性后缀（如 鞋柜/刀具包）。

    覆盖边界：只覆盖能落到具体物品名/材质名的避开项。审美类（不要太贵、
    要体面、别太俗气）没有客观依据，不在此列，也不会被这里误判成已处理。
    """
    text = str(avoid_text or "").lower()
    if not text:
        return []
    title = str(item.get("title_key") or item.get("title") or item.get("name") or "").lower()
    category = str(item.get("category") or "").lower()
    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    # 标题只认前 12 个字：真实池标题形如「费列罗榛果威化巧克力礼盒（8粒装）」，
    # 主体品类都在开头，括号里的是规格/赠品/场景。
    head = title[:12]
    # 这些后缀说明落地词是修饰语而不是商品主体（鞋柜、刀具包、伞架）。
    suffix_block = ("柜", "架", "包", "扣", "刷", "膜", "座", "套", "链", "凳")
    # 这些前缀说明落地词只是名字的一部分：「广州酒家」「荔煌酒家」是商家名，
    # 「酒酿」是食品工艺，都不是酒类商品。
    prefix_block = {"酒": ("家", "酿", "香", "店"), "鞋": (), "刀": (), "伞": (), "钟": ()}

    # (用户措辞同义词组, tag, 品类词, 标题落地词, 理由)
    checks = [
        (("酒", "酒精", "饮酒", "喝酒"),
         ("wine", "beer", "alcohol", "alcoholic", "liquor"),
         ("酒", "酒类"), ("红酒", "葡萄酒", "白酒", "啤酒", "果酒", "米酒", "黄酒", "洋酒", "酒"),
         "用户自定义避开酒精或酒类"),
        (("香味", "香氛", "香薰", "香水", "气味"),
         ("fragrance",), ("香氛", "香薰"), ("香薰", "香氛", "香水", "香囊", "香包", "香膏"),
         "用户自定义避开香味或气味"),
        (("食品", "吃的", "甜食", "零食"),
         ("food",), ("食品", "糕点", "坚果", "水果"), ("巧克力", "曲奇", "饼干", "糖果", "蛋糕", "糕点", "零食"),
         "用户自定义不想要食品类礼物"),
        (("植物", "花束", "花材", "鲜花"),
         ("plant", "flower"), ("花礼", "花束"), ("花束", "鲜花", "花材", "绿植", "盆栽", "干花", "多肉"),
         "用户自定义避开植物或花材"),
        (("坚果", "花生"),
         ("allergen",), ("坚果",), ("坚果", "花生", "核桃", "腰果", "榛子", "巴旦木"),
         "用户自定义避开坚果或花生"),
        (("动物制品", "皮革", "真皮"),
         ("animal_product", "leather"), ("服饰配饰",), ("真皮", "皮革", "羊皮", "牛皮"),
         "用户自定义避开动物源材质"),
        (("太高调", "高调", "夸张", "显眼"),
         ("distinctive",), (), (),
         "用户自定义希望避免过于高调"),
        (("钟表", "手表", "送钟", "时钟"),
         ("clock",), ("钟表",), ("时钟", "台钟", "闹钟", "手表", "腕表", "电子钟"),
         "用户自定义避开钟表类"),
        (("鞋", "鞋子"),
         ("shoes",), ("鞋",), ("运动鞋", "帆布鞋", "拖鞋", "皮鞋", "鞋"),
         "用户自定义避开鞋类"),
        (("伞", "雨伞"),
         ("umbrella",), ("伞",), ("雨伞", "晴雨伞", "折叠伞"),
         "用户自定义避开伞"),
        (("刀", "刀具", "利器"),
         ("knife",), ("刀具",), ("刀具", "菜刀", "剪刀"),
         "用户自定义避开刀具利器"),
        (("白色包装",),
         ("white_packaging",), (), ("白色包装",),
         "用户自定义避开白色包装"),
        (("咖啡",),
         ("coffee",), ("咖啡",), ("咖啡",),
         "用户自定义避开咖啡"),
        (("茶", "茶叶"),
         ("tea",), ("茶饮",), ("茶叶", "红茶", "绿茶", "乌龙", "普洱"),
         "用户自定义避开茶"),
    ]
    blocks = []
    for word_group, item_tags, cats, head_words, message in checks:
        if not any(word in text for word in word_group):
            continue
        if any(tag in tags for tag in item_tags):
            blocks.append(("avoid_custom", message))
            continue
        if cats and any(word in category for word in cats):
            blocks.append(("avoid_custom", message))
            continue
        if head_words:
            for word in head_words:
                pos = head.find(word)
                if pos < 0:
                    continue
                # 落地词后面紧跟修饰后缀，说明它不是商品主体
                tail = head[pos + len(word):pos + len(word) + 1]
                if tail and tail in suffix_block:
                    continue
                # 落地词后面紧跟这些字，说明它只是名字的一部分
                if tail and tail in prefix_block.get(word, ()):
                    continue
                blocks.append(("avoid_custom", message))
                break
    return blocks


def evaluate_product(item, intent):
    hard_blocks = []
    soft_warnings = []
    max_budget = intent.get("budget_max")
    hard_preferences = set(intent.get("hard_preferences", []))
    occasion = intent.get("occasion")
    relation = intent.get("recipient_relation")
    item_type = _item_type(item)

    if item.get("status") != "active":
        hard_blocks.append(("inactive", "候选当前不可推荐"))
    if intent.get("gift_mode") != "all" and item_type != intent["gift_mode"]:
        hard_blocks.append(("gift_mode", f"用户只接受{intent['gift_mode']}礼物"))
    if max_budget is not None and _cost(item) > max_budget:
        hard_blocks.append(("over_budget", f"成本/价格 {_cost(item)} 元超过预算上限"))
    # 配送时效功能已整体下线（2026-11-14 用户决定）：目录里没有可靠的
    # delivery 数据，timing_risk 软警告永远建立在猜测上，已随 days_left
    # 输入框一并移除。days_left/deadline 字段若仍出现在旧请求里会被
    # 忽略，不影响其他规则。

    if item_type == "diy" and _has(item, "food") and not intent.get("accepts_food_diy", True):
        hard_blocks.append(("food_diy", "用户暂不接受自制食品"))
    if item_type == "diy" and _has(item, "fragrance") and not intent.get("accepts_fragrance_diy", True):
        hard_blocks.append(("fragrance_diy", "用户暂不接受香味类自制礼物"))

    preference_rules = {
        "non_alcoholic": ("alcohol", "用户选择了不饮酒"),
        "halal": ("non_halal", "用户选择了清真饮食"),
        "vegetarian": ("meat", "用户选择了素食"),
        "no_animal_products": ("animal_product", "用户希望避免动物制品"),
        "avoid_fragrance": ("fragrance", "用户希望避免香味"),
        "allergy_free": ("allergen", "候选存在需要确认的过敏原"),
    }
    for preference in hard_preferences:
        rule = preference_rules.get(preference)
        if rule and _has(item, rule[0]):
            hard_blocks.append((preference, rule[1]))
    hard_blocks.extend(_custom_avoid_blocks(item, intent.get("avoid_custom", "")))

    if occasion in {"wedding", "celebration"} and _has(item, "white_packaging"):
        soft_warnings.append(("celebration_packaging", "庆典场合可能对纯白包装有不同联想"))
    for tag, message in RULES["cultural_soft_warnings"].items():
        if _has(item, tag):
            soft_warnings.append((tag, message))
    if intent.get("occasion_custom") and any(
        token in intent["occasion_custom"] for token in ("探病", "慰问", "医院")
    ):
        if _has(item, "fragrance") or _has(item, "food"):
            soft_warnings.append(("custom_occasion", "探病或慰问场合需要先确认饮食、气味和身体状况"))
    if "avoid_fragrance_uncertain" in intent.get("soft_preferences", []) and _has(item, "fragrance"):
        soft_warnings.append(("uncertain_fragrance", "用户尚未确定香味偏好，建议先确认"))
    if relation in {"colleague", "manager"} and _has(item, "intimate"):
        soft_warnings.append(("too_personal", "对同事或上级可能过于私密"))
    if item_type == "diy" and item.get("allergens"):
        soft_warnings.append(("diy_safety", "自制礼物需要主动说明材料、过敏原和制作日期"))

    band = _reference_band(relation)
    if _cost(item) < band["min"] or _cost(item) > band["max"]:
        soft_warnings.append(
            ("reference_price_band", f"成本/价格 {_cost(item)} 元不在{band['label']}的参考区间 {band['min']}-{band['max']} 元内")
        )
    return hard_blocks, soft_warnings


def _custom_match_score(item, intent):
    score = 0
    custom_text = " ".join(
        [
            intent.get("occasion_custom", ""),
            intent.get("relation_custom", ""),
            intent.get("free_text", ""),
        ]
    ).lower()
    for keyword, tag in RULES["free_text_tags"].items():
        if keyword in custom_text and _has(item, tag):
            score += 14
    for keyword, tag in (
        ("乔迁", "home"),
        ("搬家", "home"),
        ("纪念", "memory"),
        ("回忆", "memory"),
        ("手作", "diy"),
        ("自制", "diy"),
        ("植物", "plant"),
        ("花", "flower"),
        ("茶", "tea"),
    ):
        if keyword in custom_text and _has(item, tag):
            score += 15
    return score


def _score(item, intent, strategy):
    score = 0.0
    occasion = intent["occasion"]
    relation = intent["recipient_relation"]
    budget_min = intent.get("budget_min")
    budget_max = intent.get("budget_max")

    if occasion in item.get("occasions", []):
        score += 30
    if relation in item.get("relations", []):
        score += 22
    score += _custom_match_score(item, intent)
    if budget_min is not None and budget_max is not None:
        mid = (budget_min + budget_max) / 2
        score += max(0, 18 - abs(_cost(item) - mid) / max(mid, 1) * 18)
    # 时效加分项已随时效功能下线移除（原：days_left 越紧、lead_days 越短加分）
    if "not_too_showy" in intent.get("soft_preferences", []) and _has(item, "low_key"):
        score += 12
    if "portable" in intent.get("soft_preferences", []) and _has(item, "portable"):
        score += 10

    if strategy == "safe":
        score += 20 if _has(item, "low_risk") else 0
        score += 8 if _item_type(item) == "purchased" else 0
        score -= item.get("risk_level", 1) * 2
    elif strategy == "heart":
        score += 12 if _has(item, "personal") else 0
        score += 8 if _has(item, "customizable") else 0
        score += 5 if _has(item, "memory") else 0
    elif strategy == "practical":
        score += 14 if _has(item, "low_key") else 0
        score += 10 if _has(item, "portable") else 0
        score += 10 if _item_type(item) == "purchased" else 0
    elif strategy == "diy":
        score += 28 if _item_type(item) == "diy" else -8
        score += 8 if _has(item, "customizable") else 0
    elif strategy == "memory":
        score += 22 if _has(item, "memory") else 0
        score += 12 if _has(item, "personal") else 0
        score += 5 if _item_type(item) == "diy" else 0
    elif strategy == "distinctive":
        score += 18 if _has(item, "distinctive") else 0
        score += 6 if _has(item, "personal") else 0
    return round(score, 2)


def _evidence(item, intent):
    evidence = []
    if intent.get("occasion") in item.get("occasions", []):
        evidence.append(f"适合{intent['occasion_label']}")
    if intent.get("recipient_relation") in item.get("relations", []):
        evidence.append(f"适合送给{intent['relation_label']}")
    if intent.get("occasion_custom"):
        evidence.append(f"已纳入自定义场合：{intent['occasion_custom']}")
    if intent.get("relation_custom"):
        evidence.append(f"已纳入自定义关系：{intent['relation_custom']}")
    # 「送达/制作约 N 天」证据已随时效功能下线移除（2026-11-14）
    evidence.append(f"{'预计成本' if _item_type(item) == 'diy' else '价格'} {_cost(item)} 元")
    return evidence[:5]


def _reason_paragraph(item, intent, strategy, warnings):
    user_text = " ".join(
        (
            intent.get("free_text", ""),
            intent.get("occasion_custom", ""),
            intent.get("relation_custom", ""),
        )
    )
    matched_interests = [
        (keyword, tag, explanation)
        for keyword, tag, explanation in (
            ("咖啡", "coffee", "有咖啡相关内容"),
            ("茶", "tea", "有茶饮相关内容"),
            ("健身", "fitness", "适合运动或健身场景"),
            ("便携", "portable", "便于携带"),
            ("带出国", "portable", "便于携带"),
            ("低调", "low_key", "设计取向较克制"),
            ("亲手做", "diy", "可以由你亲手完成"),
            ("自制", "diy", "属于自制方案"),
            ("手作", "diy", "属于手作方案"),
            ("回忆", "memory", "适合承载共同回忆"),
            ("纪念", "memory", "具有纪念记录属性"),
        )
        if keyword in user_text and _has(item, tag)
    ]
    parts = []
    if matched_interests:
        unique_explanations = list(dict.fromkeys(item[2] for item in matched_interests))
        parts.append("与你提到的需求对应：" + "、".join(unique_explanations) + "。")
    parts.append(item.get("description", "知识库暂未提供更具体的商品特点。"))
    if _item_type(item) == "diy":
        materials = item.get("materials", [])
        if materials and _has(item, "customizable"):
            parts.append(f"可通过{ '、'.join(materials[:3]) }调整外观或口味，做出个人化版本。")
    if not matched_interests:
        parts.append("符合既定预算与场合要求。")
    return " ".join(parts)


def _gift_meaning(item):
    text = f"{item.get('name', '')} {item.get('category', '')}".lower()
    meanings = (
        (("咖啡",), "通常传达分享日常、陪伴和一起享受片刻时间的意思。"),
        (("茶", "茶包"), "常用于表达敬意、款待和愿意慢下来交流的心意。"),
        (("笔记本",), "寓意记录、规划和把新的想法带入未来。"),
        (("绿植", "植物", "微景观"), "常被理解为成长、活力和持续陪伴，但需要对方愿意照料。"),
        (("坚果", "水果干"), "偏向分享、团聚和日常关照，适合轻量而不太正式的表达。"),
        (("保温杯",), "寓意把照顾放进日常，希望对方在通勤、学习或运动中被持续惦记。"),
        (("按摩球",), "表达对健康、休息和身体状态的关注，不依赖过强的情感表达。"),
        (("护手霜",), "偏向细致、温和的日常照顾，适合不想过分张扬的关系。"),
        (("香氛", "香薰", "蜡片"), "表达对生活氛围、审美和个人空间的关注，个性化较强。"),
        (("果酒",), "通常对应分享、庆祝和一起举杯的氛围，但必须先确认饮酒习惯。"),
        (("卡套", "皮"), "寓意稳重、实用和把祝福放进每天都会使用的物件里。"),
        (("钟",), "可以表达珍惜时间和长期陪伴，但部分语境会产生谐音联想。"),
        (("伞",), "常被赋予遮风挡雨、提供保护的寓意，但部分语境会联想到“散”。"),
        (("花束", "鲜花", "干花", "花材", "花篮"), "表达祝福、纪念和审美，但花材、颜色与场合需要具体核对。"),
        (("照片", "相框", "回忆"), "把共同经历保存下来，强调纪念、回看和关系中的具体故事。"),
        (("曲奇", "果酱"), "偏向分享、亲手照料和把个人口味转成一份可食用的心意。"),
        (("香囊",), "常承载节日祝福、传统手作和随身陪伴的意味，不等于医疗功效。"),
        (("帆布袋", "收纳", "收纳篮"), "寓意实用、整理和把个性设计融入日常使用。"),
        (("手工皂",), "表达亲手制作和生活照料，但成分与皮肤耐受需要单独确认。"),
        (("礼盒",), "通过多个小内容组合出一个主题，重点在于定制和逐项解释，而不是堆叠数量。"),
    )
    for keywords, meaning in meanings:
        if any(keyword in text for keyword in keywords):
            return meaning
    return "当前资料没有给出统一寓意，建议把重点放在收礼人的实际偏好、使用场景和你的具体表达上。"


def _taboo_keyword_matches(keyword, searchable):
    """Match taboo facts without treating explicit negation as evidence."""
    text = str(searchable or "").lower()
    for part in str(keyword or "").split("/"):
        token = part.strip().lower()
        if not token or token not in text:
            continue
        escaped = re.escape(token)
        if re.search(
            rf"(?:无|不含|不添加|不使用|不带|零|0)\s*.{{0,2}}{escaped}",
            text,
        ):
            continue
        # 「英国梨」「香梨」这类复合词是香型名/品种名/加工品名，不是水果梨——
        # 祖玛珑"英国梨与小苍兰"等香氛名、梨膏糖这类加工食品，
        # 不应命中"梨=分离"这条忌讳（忌讳针对的是作为水果的梨）。
        # 与凤梨特判（app.py _meaning_for_product）同类：名字带字≠实物。
        if token == "梨":
            # 复合词后缀：梨前有修饰字（英国梨/香梨/啤梨/雪梨…）→ 香型名或品种
            if re.search(r"(?:英国|香|啤|雪|酸|凤)梨", text):
                continue
            # 加工词后缀：梨后接膏/糖/干/脯/罐头 → 加工食品，不是送礼忌讳指的鲜果
            if re.search(r"梨(?:膏|糖|干|脯|罐头|汁|酥)", text):
                continue
        return True
    return False


def _item_taboo_notes(item, intent):
    searchable = " ".join(
        [
            item.get("name", ""),
            item.get("category", ""),
            item.get("description", ""),
            " ".join(item.get("tags", [])),
            intent.get("occasion_label", ""),
            intent.get("occasion_custom", ""),
        ]
    ).lower()
    notes = []
    for row in TABOOS:
        keyword = row.get("keyword", "")
        if keyword and _taboo_keyword_matches(keyword, searchable):
            notes.append(
                {
                    "keyword": keyword,
                    "note": row.get("reason", ""),
                    "scope": row.get("scope", "部分语境"),
                    "source_url": row.get("source_url", ""),
                }
            )
    regional = _regional_notes(intent)
    for row in regional:
        if row.get("topic") != "no_specific_match":
            notes.append(
                {
                    "keyword": f"{row.get('region', '地区')}地方提醒",
                    "note": row.get("note", ""),
                    "scope": "地方或家庭差异",
                    "source_url": row.get("source_url", ""),
                }
            )
    unique = []
    seen = set()
    for note in notes:
        key = (note["keyword"], note["note"])
        if key not in seen:
            seen.add(key)
            unique.append(note)
    return unique[:6]


def _card(item, intent, strategy, score, warnings):
    labels = {
        "safe": "最稳妥",
        "heart": "最有心意",
        "practical": "最实用",
        "diy": "最适合自制",
        "memory": "最有纪念感",
        "distinctive": "最有个性",
    }
    item_type = _item_type(item)
    watch_parts = []
    for part in (
        item.get("risk_notes", ""),
        item.get("safety_notes", ""),
        *(text for _, text in warnings),
    ):
        if part and part not in watch_parts:
            watch_parts.append(part)
    # 配送时效字段已随时效功能下线（2026-11-14 用户决定）全部移除：
    # delivery_days* / fulfillment_estimate / planning_label / region_note /
    # volatility_reasons / is_region_aware 全部不再产出。前端本就只在
    # 非空时渲染 fulfillment_estimate，清空后卡片自动不出「配送 N 天」。
    return {
        "product_id": item["id"],
        "title": item["name"],
        "strategy": strategy,
        "strategy_label": labels[strategy],
        "gift_type": item_type,
        "gift_type_label": "自制礼物" if item_type == "diy" else "购买礼物",
        "category": item["category"],
        "price": _cost(item),
        "cost": _cost(item),
        "fulfillment_estimate": "",
        "fulfillment_label": "制作" if item_type == "diy" else "送达",
        "difficulty": item.get("difficulty"),
        "materials": item.get("materials", []),
        "steps": item.get("steps", []),
        "preservation": item.get("preservation"),
        "allergens": item.get("allergens", []),
        "description": item["description"],
        "meaning": _gift_meaning(item),
        "why_this": _reason_paragraph(item, intent, strategy, warnings),
        "reason_evidence": _evidence(item, intent),
        "watch_out": "；".join(watch_parts) or "暂无特别提醒，但仍建议结合收礼人的实际偏好确认",
        "taboo_notes": _item_taboo_notes(item, intent),
        "soft_warnings": [{"code": code, "text": text} for code, text in warnings],
        "score": score,
        "source": item.get("source", item.get("source_title", "")),
        "source_url": item.get("source_url", ""),
        "source_title": item.get("source_title", ""),
    }


def _regional_notes(intent):
    region = (intent.get("recipient_region") or intent.get("user_region") or "").strip()
    if not region:
        return []
    matched = []
    for row in REGIONAL_VARIATIONS:
        scope = row.get("region_scope", "")
        if region in scope or scope in region:
            matched.append(
                {
                    "region": scope,
                    "topic": row.get("subtopic", ""),
                    "note": row.get("variation_statement", ""),
                    "action": row.get("recommended_action", "ask_preference"),
                    "source_url": row.get("source_url", ""),
                }
            )
    if not matched:
        return [
            {
                "region": region,
                "topic": "no_specific_match",
                "note": f"当前知识库没有检索到“{region}”的具体地方忌讳条目。",
                "action": "ask_preference",
                "source_url": "",
            }
        ]
    return matched[:4]


def build_taboo_notes(intent, cards):
    searchable = " ".join(
        [
            intent.get("occasion_label", ""),
            intent.get("occasion_custom", ""),
            intent.get("relation_label", ""),
            intent.get("free_text", ""),
        ]
        + [card.get("title", "") + " " + card.get("category", "") for card in cards]
    ).lower()
    general = []
    for row in TABOOS:
        keyword = row.get("keyword", "")
        if keyword and _taboo_keyword_matches(keyword, searchable):
            general.append(
                {
                    "keyword": keyword,
                    "note": row.get("reason", ""),
                    "severity": row.get("severity", "soft"),
                    "scope": row.get("scope", "部分中文送礼语境"),
                    "source_url": row.get("source_url", ""),
                }
            )
    regional = _regional_notes(intent)
    status = "found" if general or regional else "none_found"
    return {
        "status": status,
        "general": general[:8],
        "regional": regional,
        "confirmation_questions": [
            "收礼人本人或家里是否有特别避讳？",
            "这个场合是否有单位、家庭或地方习惯？",
            "食品、香味、植物和材质是否需要再确认？",
        ],
        "disclaimer": "地方习俗具有家庭和地区差异，以下仅作核对提醒，不是全国统一禁忌；未检索到也不代表不存在地方习惯。",
    }


def _summarize_blocks(blocks):
    counts = {}
    for item in blocks:
        for reason in item["reasons"]:
            counts[reason["code"]] = counts.get(reason["code"], 0) + 1
    labels = {
        "over_budget": "超过预算上限",
        "late_delivery": "无法按时送达或完成",
        "non_alcoholic": "含酒精",
        "halal": "不满足清真约束",
        "vegetarian": "不满足素食约束",
        "no_animal_products": "包含动物制品",
        "avoid_fragrance": "含香味",
        "allergy_free": "存在过敏原风险",
        "food_diy": "不接受自制食品",
        "fragrance_diy": "不接受香味类自制礼物",
        "avoid_custom": "命中用户自定义避开内容",
        "gift_mode": "礼物类型不符合选择",
        "inactive": "候选不可用",
    }
    return [
        {"code": code, "label": labels.get(code, code), "count": count}
        for code, count in sorted(counts.items(), key=lambda pair: -pair[1])
    ]


def recommend(raw_intent):
    intent = normalize_intent(raw_intent)
    excluded_ids = set(_as_list((raw_intent or {}).get("exclude_product_ids")))
    trace_id = f"gr-{uuid.uuid4().hex[:10]}"
    eligible = []
    hard_blocks = []
    soft_warning_map = {}
    stage_counts = {
        "catalog": len(CATALOG),
        "active": 0,
        "after_budget": 0,
        "after_deadline": 0,
        "after_explicit_preferences": 0,
        "eligible": 0,
    }

    for item in CATALOG:
        blocks, warnings = evaluate_product(item, intent)
        if item.get("status") == "active":
            stage_counts["active"] += 1
        if not any(code in {"over_budget", "gift_mode"} for code, _ in blocks):
            stage_counts["after_budget"] += 1
        # There is no hard deadline stage: timing never removes a candidate.
        stage_counts["after_deadline"] += 1
        explicit_codes = {
            "non_alcoholic",
            "halal",
            "vegetarian",
            "no_animal_products",
            "avoid_fragrance",
            "allergy_free",
            "avoid_custom",
            "food_diy",
            "fragrance_diy",
        }
        if not any(code in explicit_codes for code, _ in blocks):
            stage_counts["after_explicit_preferences"] += 1
        if blocks:
            hard_blocks.append(
                {
                    "product_id": item["id"],
                    "product_name": item["name"],
                    "reasons": [{"code": code, "text": text} for code, text in blocks],
                }
            )
            continue
        if item["id"] in excluded_ids:
            continue
        eligible.append(item)
        if warnings:
            soft_warning_map[item["id"]] = warnings
    stage_counts["eligible"] = len(eligible)

    cards = []
    strategies = ("safe", "heart", "practical", "diy", "memory", "distinctive")
    for item in eligible:
        strategy = max(strategies, key=lambda key: _score(item, intent, key))
        warnings = soft_warning_map.get(item["id"], [])
        cards.append(
            _card(item, intent, strategy, _score(item, intent, strategy), warnings)
        )
    cards.sort(key=lambda card: card["score"], reverse=True)

    result = {
        "trace_id": trace_id,
        "mode": "local_rule_mode",
        "intent": intent,
        "rule_result": {
            "eligible_product_ids": [item["id"] for item in eligible],
            "hard_blocks": hard_blocks,
            "soft_warnings": [
                {
                    "product_id": item_id,
                    "product_name": next(item["name"] for item in CATALOG if item["id"] == item_id),
                    "reasons": [{"code": code, "text": text} for code, text in warnings],
                }
                for item_id, warnings in soft_warning_map.items()
            ],
            "rules_version": RULES["version"],
        },
        "decision_trace": {
            "filter_steps": stage_counts,
            "unknown_notes": intent["unknown_notes"],
            "selection_mode": "constraint_first",
        },
        "candidates": cards,
        "excluded_summary": _summarize_blocks(hard_blocks),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    result["taboo_notes"] = build_taboo_notes(intent, cards)
    if not cards:
        result["mode"] = "no_safe_candidate"
        result["fallback_message"] = "当前条件下没有可用候选。建议微调预算区间或礼物类型，但请勿放宽明确的健康、宗教或过敏底线约束。"
    return result


def _baseline_card(item, intent, strategy, rank):
    card = _card(item, intent, strategy if strategy in {"safe", "heart", "practical", "diy", "memory", "distinctive"} else "safe", rank, [])
    card["strategy_label"] = "基线候选"
    card["why_this"] = "基线按通用相关性排序，未执行完整约束检查"
    card["watch_out"] = "此候选未经硬约束过滤，请人工核对预算、时效和禁忌"
    return card


def recommend_no_rules(raw_intent):
    intent = normalize_intent(raw_intent)
    active = [item for item in CATALOG if item.get("status") == "active"]
    cards = []
    used = set()
    for strategy in ("safe", "heart", "practical"):
        ranked = sorted(active, key=lambda item: _score(item, intent, strategy), reverse=True)
        selected = next((item for item in ranked if item["id"] not in used), None)
        if selected:
            used.add(selected["id"])
            cards.append(_baseline_card(selected, intent, strategy, _score(selected, intent, strategy)))
    return {
        "trace_id": f"ablation-{uuid.uuid4().hex[:10]}",
        "mode": "no_rules_ablation",
        "intent": intent,
        "rule_result": {"eligible_product_ids": [item["id"] for item in active], "hard_blocks": [], "soft_warnings": [], "rules_version": "disabled"},
        "decision_trace": {"filter_steps": {"catalog": len(CATALOG), "active": len(active), "after_budget": len(active), "after_deadline": len(active), "after_explicit_preferences": len(active), "eligible": len(active)}, "unknown_notes": intent["unknown_notes"], "selection_mode": "no_rules_ablation"},
        "candidates": cards,
        "excluded_summary": [],
    }


def recommend_heuristic(raw_intent):
    intent = normalize_intent(raw_intent)
    active = [item for item in CATALOG if item.get("status") == "active"]
    budget_max = intent.get("budget_max") or 300

    def heuristic_score(item):
        # _lead_days 项已随时效功能下线移除（基线对比基线模式保留原逻辑除外）
        return round(100 - abs(_cost(item) - min(budget_max, 200)) - item.get("risk_level", 1) * 5, 2)

    ranked = sorted(active, key=heuristic_score, reverse=True)
    cards = [_baseline_card(item, intent, "practical", heuristic_score(item)) for item in ranked[:3]]
    return {
        "trace_id": f"heuristic-{uuid.uuid4().hex[:10]}",
        "mode": "heuristic_baseline",
        "intent": intent,
        "rule_result": {"eligible_product_ids": [item["id"] for item in active], "hard_blocks": [], "soft_warnings": [], "rules_version": "disabled"},
        "decision_trace": {"filter_steps": {"catalog": len(CATALOG), "active": len(active), "after_budget": len(active), "after_deadline": len(active), "after_explicit_preferences": len(active), "eligible": len(active)}, "unknown_notes": intent["unknown_notes"], "selection_mode": "price_delivery_risk_heuristic"},
        "candidates": cards,
        "excluded_summary": [],
    }
