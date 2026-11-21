import json
import re

RANKING_VERSION = "ranker-v5"
TIMING_REMINDER = "无法计算具体路线时效，建议到对应快递公司小程序或官网查询"


def parse_value(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return {}
    text = re.sub(r"<think>[\s\S]*?</think>", "", value, flags=re.I)
    text = re.sub(r"```(?:json)?", "", text, flags=re.I).replace("```", "").strip()
    if not text:
        return {}
    for _ in range(6):
        try:
            parsed = json.loads(text)
        except Exception:
            starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
            if not starts:
                return {}
            start = min(starts)
            end = max(text.rfind("}"), text.rfind("]"))
            if end <= start:
                return {}
            try:
                parsed = json.loads(text[start : end + 1])
            except Exception:
                return {}
        if isinstance(parsed, (dict, list)):
            return parsed
        if isinstance(parsed, str):
            text = parsed.strip()
            continue
        return {}
    return {}


def text(value):
    return str(value or "").strip()


def number(value):
    if value in (None, "", False):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    result = float(match.group(0))
    return int(result) if result.is_integer() else result


def as_list(value):
    parsed = parse_value(value)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("candidate_products", "products", "items", "records", "estimates"):
            nested = parse_value(parsed.get(key))
            if isinstance(nested, list):
                return nested
        result = parsed.get("result")
        if result not in (None, ""):
            nested = parse_value(result)
            if isinstance(nested, list):
                return nested
            if isinstance(nested, dict) and nested:
                return as_list(nested)
    return []


def find_intent(value, depth=0):
    if depth > 12:
        return {}
    parsed = parse_value(value)
    if isinstance(parsed, dict):
        keys = {
            "occasion",
            "recipient_relation",
            "gift_mode",
            "budget_min",
            "budget_max",
            "hard_preferences",
            "soft_preferences",
            "user_region",
            "recipient_region",
            "days_left",
        }
        if keys.intersection(parsed):
            return dict(parsed)
        for nested in parsed.values():
            found = find_intent(nested, depth + 1)
            if found:
                return found
    elif isinstance(parsed, list):
        for nested in parsed:
            found = find_intent(nested, depth + 1)
            if found:
                return found
    return {}


def find_named_value(value, name, depth=0):
    if depth > 12:
        return None
    parsed = parse_value(value)
    if isinstance(parsed, dict):
        candidate = parsed.get(name)
        if candidate not in (None, ""):
            return candidate
        for nested in parsed.values():
            found = find_named_value(nested, name, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(parsed, list):
        for nested in parsed:
            found = find_named_value(nested, name, depth + 1)
            if found not in (None, ""):
                return found
    return None


def product_id(item):
    if not isinstance(item, dict):
        return ""
    for key in ("product_id", "商品ID", "id", "gift_id", "diy_id"):
        if item.get(key) not in (None, ""):
            return text(item[key])
    return ""


def product_name(item):
    if not isinstance(item, dict):
        return product_id(item)
    for key in ("name", "title", "商品名称", "标题", "名称", "description", "描述"):
        if item.get(key):
            return text(item[key])
    return product_id(item)


def normalize_product(item):
    if not isinstance(item, dict):
        return None
    result = dict(item)
    pid = product_id(result)
    if not pid:
        return None
    result["product_id"] = pid
    result.setdefault("name", product_name(result))
    result.setdefault("title", product_name(result))
    result.setdefault("status", "active")
    result["gift_type"] = text(
        result.get("gift_type") or result.get("type") or "purchased"
    ).lower()
    if result["gift_type"] not in {"purchased", "diy"}:
        result["gift_type"] = "purchased"
    return result


def products(value):
    output = []
    seen = set()
    for item in as_list(value):
        normalized = normalize_product(item)
        if normalized and normalized["product_id"] not in seen:
            seen.add(normalized["product_id"])
            output.append(normalized)
    return output


def product_text(item):
    values = []
    for key in (
        "product_id",
        "name",
        "title",
        "category",
        "类别",
        "description",
        "描述",
        "tags",
        "risk_notes",
        "safety_notes",
        "allergens",
    ):
        value = item.get(key)
        if isinstance(value, list):
            values.extend(text(x) for x in value)
        elif isinstance(value, dict):
            values.extend(text(x) for x in value.values())
        elif value not in (None, ""):
            values.append(text(value))
    return " ".join(values).lower()


def product_tags(item):
    value = item.get("tags", item.get("标签", []))
    parsed = parse_value(value)
    if isinstance(parsed, list):
        return {text(x).lower() for x in parsed}
    return {part.strip().lower() for part in re.split(r"[,;，；]", text(value)) if part.strip()}


def price_value(item):
    for key in ("price", "价格", "cost", "成本", "estimated_cost", "cost_estimate_cny"):
        value = number(item.get(key))
        if value is not None:
            return value
    return None


def price_estimate(item):
    price = price_value(item)
    if price is None:
        return {
            "average_price": None,
            "range_min": None,
            "range_max": None,
            "currency": text(item.get("currency") or "CNY"),
            "confidence": "low",
            "basis": "输入未提供可用的价格或成本参考值",
            "reminder": "价格或成本未知，请向商家核实",
        }
    label = "成本参考值" if item.get("gift_type") == "diy" else "价格快照"
    return {
        "average_price": round(float(price), 2),
        "range_min": round(float(price) * 0.9, 2),
        "range_max": round(float(price) * 1.1, 2),
        "currency": text(item.get("currency") or "CNY"),
        "confidence": "low",
        "basis": f"基于输入中的{label}按上下约10%估算，非已确认成交价或实际成本",
        "reminder": "价格或成本为估算，购买或制作前请核实",
    }


def route_records(value):
    parsed = parse_value(value)
    if isinstance(parsed, dict):
        if isinstance(parsed.get("estimates"), list):
            parsed = parsed["estimates"]
        elif isinstance(parsed.get("result"), (dict, list, str)):
            return route_records(parsed["result"])
        elif any(key in parsed for key in ("product_id", "average_days", "avg_days")):
            parsed = [parsed]
    return parsed if isinstance(parsed, list) else []


def route_estimate(raw, item, request):
    pid = product_id(item)
    for record in route_records(raw):
        if not isinstance(record, dict):
            continue
        rid = text(record.get("product_id") or record.get("商品ID") or record.get("id"))
        if rid and rid != pid:
            continue
        average = number(
            record.get("average_days")
            or record.get("avg_days")
            or record.get("delivery_days")
        )
        if average is not None:
            return {
                "average_days": average,
                "range_min_days": max(1, int(average - 1)),
                "range_max_days": int(average + 2),
                "confidence": text(record.get("confidence") or "low"),
                "basis": text(record.get("basis") or "根据路线时效资料估算，非商家承诺"),
                "reminder": text(record.get("reminder") or TIMING_REMINDER),
            }
    embedded = item.get("delivery_estimate")
    if isinstance(embedded, dict):
        average = number(
            embedded.get("average_days")
            or embedded.get("avg_days")
            or embedded.get("delivery_days")
        )
        if average is not None:
            return {
                "average_days": average,
                "range_min_days": max(1, int(average - 1)),
                "range_max_days": int(average + 2),
                "confidence": text(embedded.get("confidence") or "low"),
                "basis": text(embedded.get("basis") or "商品记录中的路线时效参考"),
                "reminder": text(embedded.get("reminder") or TIMING_REMINDER),
            }
    if item.get("gift_type") == "diy":
        prep = number(item.get("prep_days") or item.get("制作天数"))
        if prep is not None:
            return {
                "average_days": prep,
                "range_min_days": max(1, int(prep - 1)),
                "range_max_days": int(prep + 2),
                "confidence": "low",
                "basis": "商品记录中的制作天数参考，未包含具体快递路线时效",
                "reminder": "制作时间为参考值，非商家承诺；如需准确配送时间，请向商家或快递公司确认",
            }
    return {
        "average_days": None,
        "range_min_days": None,
        "range_max_days": None,
        "confidence": "low",
        "basis": "输入未提供可用的路线时效参考，无法计算具体路线时效",
        "reminder": TIMING_REMINDER,
    }


def has_alcohol(item):
    value = product_text(item)
    safe = ("无酒精", "不含酒精", "非酒精", "零酒精", "无醇", "non_alcoholic", "non-alcoholic")
    if any(word in value for word in safe):
        return False
    return bool(
        product_tags(item).intersection({"wine", "beer", "alcohol", "alcoholic", "liquor"})
        or any(word in value for word in ("酒类", "红酒", "葡萄酒", "白酒", "啤酒", "酒精", "wine", "beer"))
    )


def has_nuts(item):
    value = product_text(item)
    safe = (
        "无坚果", "不含坚果", "未添加坚果", "无花生", "不含花生", "未添加花生",
        "nut_free", "nut-free", "peanut_free", "peanut-free",
    )
    if any(word in value for word in safe):
        return False
    return bool(
        product_tags(item).intersection({"nut", "nuts", "peanut", "tree_nut", "allergen"})
        or any(
            word in value
            for word in (
                "坚果", "花生", "核桃", "腰果", "杏仁", "开心果", "榛子",
                "夏威夷果", "巴旦木", "松子", "peanut", "nut",
            )
        )
    )


def product_attribute_text(item):
    """只取能说明「这件商品本身是什么」的字段。

    与 product_text 的区别：这里排除 safety_notes / allergens / risk_notes。
    那三个字段装的是**否定性提醒**（「不要把精油混入食品茶包」「含坚果，过敏者慎用」），
    它们描述的是「要小心什么」，不是「这件商品含什么」。把它们拼进判定文本，
    会把「手作茶包礼盒」因为一句「不要混入精油」判成香氛商品 —— 2026-11-16 实测误杀
    diy-003，用户明确要避香氛时整张卡被丢掉。

    阳性判定仍然完整保留：标题/名称/类别/描述/材料/标签里出现「香薰」「精油」照样命中。
    """
    values = []
    for key in (
        "product_id",
        "name",
        "title",
        "category",
        "类别",
        "description",
        "描述",
        "tags",
        "materials",
        "材料",
    ):
        value = item.get(key)
        if isinstance(value, list):
            values.extend(text(x) for x in value)
        elif isinstance(value, dict):
            values.extend(text(x) for x in value.values())
        elif value not in (None, ""):
            values.append(text(value))
    return " ".join(values).lower()


def has_fragrance(item):
    """香味类商品判定：只看商品自身属性，否定性安全提醒不算证据。

    覆盖淘宝采集卡（tags 常为空）与手作卡（tags 带 fragrance）两条路。
    """
    value = product_attribute_text(item)
    safe = (
        "无香", "不含香", "未添加香", "无味", "无火", "不添加香精",
        "fragrance_free", "fragrance-free", "unscented",
    )
    if any(word in value for word in safe):
        return False
    tags = product_tags(item)
    if tags.intersection({"fragrance", "scented", "aroma", "perfume", "candle"}):
        return True
    return any(
        word in value
        for word in (
            "香薰", "香氛", "香水", "香囊", "香包", "香薰蜡烛", "香薰蜡片",
            "扩香", "精油", "线香", "檀香", "香味蜡烛", "香膏",
            "perfume", "scented", "aroma",
        )
    )


def main(candidate_products=None, gift_request=None, purchased_knowledge=None,
         diy_knowledge=None, delivery_estimates=None, structured_output=None, **kwargs):
    all_inputs = {
        "structured_output": structured_output,
        "gift_request": gift_request,
        "candidate_products": candidate_products,
        "purchased_knowledge": purchased_knowledge,
        "diy_knowledge": diy_knowledge,
        "delivery_estimates": delivery_estimates,
        **kwargs,
    }
    # Batch tests and older Dify nodes may wrap all inputs inside result/data.
    wrapped_sources = {
        "structured_output": structured_output,
        "gift_request": gift_request,
        "candidate_products": candidate_products,
        "purchased_knowledge": purchased_knowledge,
        "diy_knowledge": diy_knowledge,
        "delivery_estimates": delivery_estimates,
    }
    for name, current in list(wrapped_sources.items()):
        current_is_valid = (
            bool(find_intent(current))
            if name in {"structured_output", "gift_request"}
            else bool(products(current))
            if name in {"candidate_products", "purchased_knowledge", "diy_knowledge"}
            else bool(route_records(current))
        )
        if not current_is_valid:
            nested = find_named_value(all_inputs, name)
            if nested is not None and nested is not current:
                wrapped_sources[name] = nested
    structured_output = wrapped_sources["structured_output"]
    gift_request = wrapped_sources["gift_request"]
    candidate_products = wrapped_sources["candidate_products"]
    purchased_knowledge = wrapped_sources["purchased_knowledge"]
    diy_knowledge = wrapped_sources["diy_knowledge"]
    delivery_estimates = wrapped_sources["delivery_estimates"]
    all_inputs.update(wrapped_sources)

    parsed_intent = find_intent(structured_output)
    raw_intent = find_intent(gift_request)
    intent = dict(parsed_intent)
    # The user's explicit structured fields are authoritative; the LLM only
    # fills fields absent from the start-node request.
    for key, value in raw_intent.items():
        if value is not None and value != "" and value != []:
            intent[key] = value
    if not intent:
        intent = find_intent(all_inputs)
    input_error = not bool(intent)
    if input_error:
        intent = {
            "occasion": None,
            "recipient_relation": None,
            "gift_mode": "all",
            "budget_min": None,
            "budget_max": None,
            "hard_preferences": [],
            "soft_preferences": [],
            "avoid_custom": "",
            "free_text": "",
            "unknown_notes": ["未收到可解析的 gift_request 或 structured_output"],
        }
    mode = text(intent.get("gift_mode") or "all").lower()
    if mode not in {"purchased", "diy", "all"}:
        mode = "all"
    budget_min = number(intent.get("budget_min"))
    budget_max = number(intent.get("budget_max"))
    hard = {text(x).lower() for x in (intent.get("hard_preferences") or [])}
    avoid = " ".join(
        (
            text(intent.get("avoid_custom")),
            text(intent.get("free_text")),
            " ".join(hard),
        )
    ).lower()
    avoid_alcohol = bool(
        hard.intersection({"non_alcoholic", "no_alcohol", "avoid_alcohol"})
        or any(x in avoid for x in ("不要酒", "不要酒精", "不喝酒", "不饮酒", "收礼人不饮酒", "饮酒"))
    )
    avoid_nuts = bool(
        hard.intersection({"allergy_free", "nut_free", "no_nuts", "avoid_nuts", "no_allergen"})
        or any(x in avoid for x in ("坚果", "花生", "过敏原", "过敏"))
    )
    # 香味约束：英文 token（勾选框）、中文 hard_preferences 原文（需求解析输出）、
    # 自定义避开文本、以及「不接受香味类手作」开关，四条路任一命中即启用。
    accepts_fragrance_diy = intent.get("accepts_fragrance_diy")
    avoid_fragrance = bool(
        hard.intersection({"avoid_fragrance", "no_fragrance", "fragrance_free"})
        or any(x in avoid for x in ("香薰", "香氛", "香水", "香味", "香囊", "香包", "气味"))
        or accepts_fragrance_diy is False
    )

    candidates = []
    seen = set()
    for source in (candidate_products, purchased_knowledge, diy_knowledge):
        for item in products(source):
            if item["product_id"] not in seen:
                seen.add(item["product_id"])
                candidates.append(item)

    if input_error:
        candidates = []

    eligible = []
    blocks = []
    route_warnings = []
    for item in candidates:
        pid = item["product_id"]
        name = product_name(item)
        kind = item["gift_type"]
        value = product_text(item)
        tags = product_tags(item)
        reasons = []
        if mode in {"purchased", "diy"} and kind != mode:
            reasons.append("礼物类型与用户选择不一致")
        price = price_value(item)
        # DIY 卡的 price 是物料成本估算：比预算下限便宜是特性不是违规，只拦上限。
        # purchased 才保留下限检查（防异常低价入场品）。
        if kind != "diy" and price is not None and budget_min is not None and price < budget_min:
            reasons.append("低于预算下限")
        if price is not None and budget_max is not None and price > budget_max:
            reasons.append("超过预算上限")
        if avoid_alcohol and has_alcohol(item):
            reasons.append("命中明确禁忌：酒类或含酒精商品")
        if avoid_nuts and has_nuts(item):
            reasons.append("命中明确禁忌：坚果或花生类商品（用户声明避开坚果类过敏原）")
        if avoid_fragrance and has_fragrance(item):
            reasons.append("命中明确禁忌：香味/香氛类商品（用户声明避开香味）")
        if "vegan" in hard and (
            tags.intersection({"dairy", "milk", "egg", "meat"})
            or any(word in value for word in ("奶油", "乳制品", "牛奶", "奶酪", "鸡蛋", "蛋类", "肉类", "肉制品"))
        ):
            reasons.append("命中明确饮食约束：含动物性成分，不符合纯素要求")
        if "vegetarian" in hard and (
            tags.intersection({"meat", "fish"})
            or any(word in value for word in ("肉类", "肉制品", "鱼", "海鲜", "猪肉", "牛肉", "鸡肉"))
        ):
            reasons.append("命中明确饮食约束：不符合素食要求")
        # 清真约束（前端勾选框，engine 用 non_halal 标签；此处只查肉/鱼/猪，
        # 不拦酒精——酒精有独立的 non_alcoholic 选项，engine 的 halal 也不拦酒）
        if "halal" in hard and (
            tags.intersection({"meat", "fish", "pork", "non_halal"})
            or any(word in value for word in ("猪肉", "猪油", "腊肠", "火腿", "培根"))
        ):
            reasons.append("命中明确饮食约束：不符合清真饮食要求")
        # 避免动物制品约束（前端勾选框，与 engine 的 animal_product 标签一致）
        if "no_animal_products" in hard and (
            tags.intersection({"animal_product", "leather", "wool", "silk", "fur", "feather", "bone", "horn"})
            or any(word in value for word in ("皮革", "真皮", "皮草", "羊毛", "丝绸", "蚕丝", "羽绒", "动物制品"))
        ):
            reasons.append("命中明确禁忌：包含动物制品（用户希望避免动物制品）")
        if "allergy_free" in hard and (
            item.get("allergens") or "allergen" in tags
        ):
            reasons.append("候选存在需要确认的过敏原")
        if text(item.get("status") or "active").lower() not in {"active", "unknown"}:
            reasons.append("商品状态不是 active")
        if reasons:
            blocks.append({"product_id": pid, "name": name, "reasons": reasons})
            continue

        estimate = route_estimate(delivery_estimates, item, intent)
        enriched = dict(item)
        enriched["price_estimate"] = price_estimate(item)
        enriched["delivery_estimate"] = estimate
        if estimate["average_days"] is None and kind == "purchased":
            if TIMING_REMINDER not in route_warnings:
                route_warnings.append(TIMING_REMINDER)
        days_left = number(intent.get("days_left"))
        if days_left is not None and estimate["average_days"] is not None:
            if estimate["range_max_days"] > days_left:
                warning = (
                    f"预计约{estimate['average_days']}天，范围"
                    f"{estimate['range_min_days']}-{estimate['range_max_days']}天，"
                    f"当前剩余{days_left}天，存在超时风险"
                )
                if warning not in route_warnings:
                    route_warnings.append(warning)
        eligible.append(enriched)

    route = {
        "average_days": None,
        "range_min_days": None,
        "range_max_days": None,
        "confidence": "low",
        "basis": "输入未提供可用的路线时效参考，无法计算具体路线时效",
        "reminder": TIMING_REMINDER,
    }
    for item in eligible:
        candidate_route = item.get("delivery_estimate", {})
        if candidate_route.get("average_days") is not None:
            route = dict(candidate_route)
            break

    output = {
        "intent": intent,
        "rule_result": {
            "eligible_products": [x["product_id"] for x in eligible],
            "hard_blocks": blocks,
            "soft_warnings": route_warnings,
            "excluded_summary": [],
            "rules_version": "rules-v5",
        },
        "status": "input_error" if input_error else ("ok" if eligible else "no_eligible_candidates"),
        "input_diagnostics": (
            ["未从开始节点 gift_request 或需求解析 structured_output 中解析到需求字段；已停止推荐，检查规则节点映射。"]
            if input_error
            else []
        ),
        "eligible_products": eligible,
        "route_estimate": route,
        "ranking_version": RANKING_VERSION,
    }
    return {"result": json.dumps(output, ensure_ascii=False)}
