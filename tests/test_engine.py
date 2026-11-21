import json
import os
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (
    DISPLAY_CANDIDATE_LIMIT,
    DIFY_CANDIDATE_LIMIT,
    answer_gift_chat,
    build_fast_preview,
    build_dify_request,
    enrich_dify_result,
    get_dify_candidates,
    merge_dify_with_preview,
    normalize_dify_response,
    _parse_api_payload,
    start_dify_job,
    _read_dify_stream,
    _reference_price_range,
    _local_reason_facts,
    _meaning_for_product,
    _strip_chat_reasoning,
)
from dify.hard_rule_node import main as hard_rule_main
from dify.rule_node_final import has_fragrance
from engine import recommend


class GiftRightEngineTests(unittest.TestCase):
    def test_fragrance_rule_ignores_negative_safety_examples(self):
        self.assertFalse(has_fragrance({
            "title": "手作茶包礼盒",
            "tags": ["diy", "tea"],
            "safety_notes": "不要把药材、精油或未经确认的植物混入食品茶包。",
        }))
        self.assertTrue(has_fragrance({
            "title": "香薰蜡片",
            "tags": ["diy", "fragrance"],
        }))

    def test_chat_reasoning_tags_are_removed_from_final_answer(self):
        leaked = (
            "<think><!--dify-deepseek-reasoning-->私有推理草稿。</think>"
            "给老师选一束简洁的向日葵或洋桔梗即可。"
        )
        self.assertEqual(
            _strip_chat_reasoning(leaked),
            "给老师选一束简洁的向日葵或洋桔梗即可。",
        )

    def test_unclosed_chat_reasoning_block_is_removed_fail_closed(self):
        leaked = "<think>不应展示的推理，没有闭合标签"
        self.assertEqual(_strip_chat_reasoning(leaked), "")

    def test_provider_reasoning_marker_is_removed(self):
        self.assertEqual(
            _strip_chat_reasoning(
                "<!--dify-deepseek-reasoning-->最终建议：选择小束浅色花。"
            ),
            "最终建议：选择小束浅色花。",
        )

    def test_api_payload_requires_bounded_json_object(self):
        self.assertEqual(_parse_api_payload(b'{"gift_mode":"diy"}'), {"gift_mode": "diy"})
        with self.assertRaisesRegex(ValueError, "顶层必须是对象"):
            _parse_api_payload(b'[]')
        with self.assertRaisesRegex(ValueError, "格式无效"):
            _parse_api_payload(b'\xff')

    def test_api_payload_rejects_oversized_body(self):
        from app import MAX_REQUEST_BODY_BYTES

        with self.assertRaisesRegex(ValueError, "请求体过大"):
            _parse_api_payload(b" " * (MAX_REQUEST_BODY_BYTES + 1))

    def test_reference_price_range_uses_approximately_ten_percent(self):
        self.assertEqual(_reference_price_range("168.00"), "约 151–185 元")
        self.assertEqual(_reference_price_range("1.00"), "价格待核实")
        self.assertEqual(
            _reference_price_range("0.01", "suspect_low_entry_price"),
            "价格待核实",
        )
        self.assertEqual(_reference_price_range("not-a-price"), "价格待核实")

    def test_local_reason_facts_do_not_treat_huajiao_as_flowers(self):
        facts = _local_reason_facts(
            {
                "name": "干货礼盒拼花胶鲍鱼元贝海鲜组合套餐",
                "category": "食品饮品",
                "description": "",
                "tags": [],
            },
            {
                "occasion_label": "中秋",
                "occasion_custom": "",
                "recipient_relation": "friend",
                "relation_label": "朋友",
                "free_text": "",
            },
            "约 148–182 元",
        )
        self.assertTrue(any("中秋" in fact for fact in facts))
        self.assertFalse(any("花束" in fact or "花材" in fact for fact in facts))

    def test_product_meaning_uses_whole_terms_not_single_character_matches(self):
        pineapple = _meaning_for_product({"name": "凤梨酥礼盒", "category": "食品饮品"})
        huajiao = _meaning_for_product({"name": "花胶鲍鱼干货礼盒", "category": "食品饮品"})
        self.assertIn("不应拆解为“梨”的寓意", pineapple)
        self.assertIn("不应解释为花束或花材", huajiao)

    def test_title_bound_holiday_items_are_excluded_from_birthday(self):
        result = hard_rule_main(
            gift_request=json.dumps(
                {
                    "occasion": "birthday",
                    "occasion_label": "生日",
                    "gift_mode": "purchased",
                    "budget_max": 300,
                },
                ensure_ascii=False,
            ),
            candidate_products=json.dumps(
                [
                    {
                        "id": "holiday-1",
                        "name": "中秋节礼品火腿礼盒",
                        "gift_type": "purchased",
                        "price": 168,
                        "status": "active",
                    },
                    {
                        "id": "general-1",
                        "name": "日常茶礼盒",
                        "gift_type": "purchased",
                        "price": 168,
                        "status": "active",
                    },
                ],
                ensure_ascii=False,
            ),
        )
        parsed = json.loads(result["result"])
        self.assertEqual([item["id"] for item in parsed["eligible_products"]], ["general-1"])
        self.assertIn("holiday-1", [item["product_id"] for item in parsed["excluded"]])

    def test_dify_rule_keeps_unknown_or_late_delivery_eligible(self):
        result = hard_rule_main(
            gift_request=json.dumps(
                {
                    "occasion": "birthday",
                    "gift_mode": "purchased",
                    "budget_max": 100,
                    "days_left": 2,
                },
                ensure_ascii=False,
            ),
            candidate_products=json.dumps(
                [
                    {
                        "id": "unknown-delivery",
                        "name": "实用办公室小礼物",
                        "gift_type": "purchased",
                        "price": 39.8,
                        "delivery_days": None,
                        "delivery_days_status": "unknown",
                        "status": "active",
                    },
                    {
                        "id": "late-delivery",
                        "name": "桌面实用摆件",
                        "category": "家居日用",
                        "gift_type": "purchased",
                        "price": 50,
                        "delivery_days": 5,
                        "delivery_days_status": "known",
                        "status": "active",
                    },
                ],
                ensure_ascii=False,
            ),
        )
        parsed = json.loads(result["result"])
        eligible = {item["id"] for item in parsed["eligible_products"]}
        blocked = {item["product_id"] for item in parsed["excluded"]}
        self.assertEqual(eligible, {"unknown-delivery", "late-delivery"})
        self.assertEqual(blocked, set())
        self.assertTrue(any("时效未知" in warning for warning in parsed["rule_warnings"]))
        self.assertTrue(any("尽快准备并发货" in warning for warning in parsed["rule_warnings"]))

    def test_opening_items_are_excluded_from_birthday_but_kept_for_opening(self):
        birthday_conflict = hard_rule_main(
            gift_request=json.dumps(
                {"occasion": "birthday", "occasion_label": "生日", "gift_mode": "purchased"},
                ensure_ascii=False,
            ),
            candidate_products=json.dumps(
                [{"id": "opening-1", "name": "开业礼物招财摆件", "gift_type": "purchased", "price": 168, "status": "active"}],
                ensure_ascii=False,
            ),
        )
        birthday_result = json.loads(birthday_conflict["result"])
        self.assertFalse(birthday_result["eligible_products"])

        opening_match = hard_rule_main(
            gift_request=json.dumps(
                {"occasion": "other", "occasion_label": "开业", "gift_mode": "purchased"},
                ensure_ascii=False,
            ),
            candidate_products=json.dumps(
                [{"id": "opening-1", "name": "开业礼物招财摆件", "gift_type": "purchased", "price": 168, "status": "active"}],
                ensure_ascii=False,
            ),
        )
        opening_result = json.loads(opening_match["result"])
        self.assertEqual([item["id"] for item in opening_result["eligible_products"]], ["opening-1"])

    def test_occasion_family_filter_covers_multiple_specialized_scenarios(self):
        cases = [
            ("毕业礼物保温杯", "birthday"),
            ("商务客户伴手礼", "birthday"),
            ("周年纪念相册", "birthday"),
            ("探病慰问营养礼盒", "birthday"),
        ]
        for title, occasion in cases:
            with self.subTest(title=title):
                self.assertIsNotNone(
                    __import__("dify.hard_rule_node", fromlist=["title_occasion_conflict"])
                    .title_occasion_conflict(
                        title,
                        {"occasion": occasion, "occasion_label": "生日"},
                    )
                )

    def test_collection_query_does_not_define_product_occasion(self):
        conflict = __import__(
            "dify.hard_rule_node", fromlist=["title_occasion_conflict"]
        ).title_occasion_conflict(
            "办公室实用小礼物",
            {"occasion": "birthday", "occasion_label": "生日"},
            "探病慰问礼物",
        )
        self.assertIsNone(conflict)

    def test_product_family_merges_same_brand_and_type_but_keeps_different_type(self):
        from app import _product_family_key

        nut = _product_family_key(
            {"title": "三只松鼠每日坚果混合坚果礼盒", "category": "食品饮品"}
        )
        nut_variant = _product_family_key(
            {"title": "三只松鼠高档纯坚果礼盒", "category": "食品饮品"}
        )
        bread = _product_family_key(
            {"title": "三只松鼠面包礼盒", "category": "食品饮品"}
        )
        self.assertEqual(nut, nut_variant)
        self.assertNotEqual(nut, bread)

    def test_rule_node_allows_multiple_products_per_family_with_cap(self):
        result = hard_rule_main(
            gift_request=json.dumps(
                {"occasion": "birthday", "gift_mode": "purchased", "budget_max": 300},
                ensure_ascii=False,
            ),
            candidate_products=json.dumps(
                [
                    {"id": "n1", "name": "三只松鼠坚果礼盒A", "gift_type": "purchased", "price": 100, "status": "active", "product_family_key": "三只松鼠|坚果"},
                    {"id": "n2", "name": "三只松鼠坚果礼盒B", "gift_type": "purchased", "price": 120, "status": "active", "product_family_key": "三只松鼠|坚果"},
                    {"id": "b1", "name": "三只松鼠面包礼盒", "gift_type": "purchased", "price": 120, "status": "active", "product_family_key": "三只松鼠|面包"},
                ],
                ensure_ascii=False,
            ),
        )
        parsed = json.loads(result["result"])
        self.assertEqual(
            {item["id"] for item in parsed["eligible_products"]},
            {"n1", "n2", "b1"},
        )

    def test_placeholder_ranker_reason_is_replaced_with_local_facts(self):
        result = {
            "intent": {"gift_mode": "purchased"},
            "candidates": [
                {
                    "product_id": "p-1",
                    "why_this": "参考价位",
                    "reason_evidence": ["参考价位"],
                }
            ],
        }
        payload = {
            "occasion": "holiday",
            "occasion_label": "中秋",
            "recipient_relation": "friend",
            "relation_label": "朋友",
            "gift_mode": "purchased",
        }
        source = {
            "id": "p-1",
            "name": "正宗金华火腿礼盒中秋节礼品",
            "category": "食品饮品",
            "price": 165,
            "price_quality": "observed_search_card_price",
            "gift_type": "purchased",
            "risk_notes": "价格和库存需复核",
            "description": "",
            "tags": [],
        }
        with patch("app.CATALOG", [source]), patch(
            "app.get_dify_candidates", return_value=[]
        ):
            # 占位理由（「参考价位」）必须被识别并替换：本地路径不再用
            # 模板句补 why_this（模板文案冒充模型文案的教训），正确行为
            # 是置空 + copy_source=template，前端备选卡不渲染文案块。
            enriched = __import__("app").enrich_dify_result(result, payload)
        self.assertNotEqual(enriched["candidates"][0]["why_this"], "参考价位")
        self.assertEqual(enriched["candidates"][0]["why_this"], "")
        self.assertEqual(enriched["candidates"][0]["copy_source"], "template")
        self.assertFalse(enriched["candidates"][0]["is_llm_copy"])

    def test_non_alcoholic_constraint(self):
        output = recommend(
            {
                "occasion": "thank_you",
                "occasion_label": "答谢",
                "recipient_relation": "mentor",
                "relation_label": "导师/老师",
                "budget_min": 100,
                "budget_max": 300,
                "days_left": 3,
                "hard_preferences": ["non_alcoholic"],
            }
        )
        self.assertTrue(output["candidates"])
        self.assertTrue(
            all("alcohol" not in card["title"].lower() for card in output["candidates"])
        )
        blocked_ids = {
            item["product_id"] for item in output["rule_result"]["hard_blocks"]
        }
        self.assertIn("g-010", blocked_ids)
        tea = next(card for card in output["candidates"] if card["product_id"] == "g-002")
        self.assertFalse(
            any(note["keyword"] == "酒精" for note in tea["taboo_notes"])
        )

    def test_deadline_input_is_ignored_and_does_not_filter(self):
        # 时效功能已下线（2026-11-14）：days_left 不再产生任何软警告，
        # 候选不因天数被拦、也不被标记。
        output = recommend(
            {
                "occasion": "birthday",
                "recipient_relation": "friend",
                "budget_min": 100,
                "budget_max": 350,
                "days_left": 1,
            }
        )
        blocked_ids = {
            item["product_id"] for item in output["rule_result"]["hard_blocks"]
        }
        eligible_ids = {
            item["product_id"] for item in output["candidates"]
        }
        self.assertNotIn("g-016", blocked_ids)
        self.assertIn("g-016", eligible_ids)
        self.assertNotIn("g-016", output["rule_result"].get("soft_warnings", {}))
        # 卡片不再携带任何时效字段
        card = next(c for c in output["candidates"] if c["product_id"] == "g-016")
        self.assertNotIn("delivery_days", card)
        self.assertEqual(card.get("fulfillment_estimate", ""), "")

    def test_soft_warning_does_not_block_reference_price(self):
        output = recommend(
            {
                "occasion": "thank_you",
                "recipient_relation": "mentor",
                "budget_min": 100,
                "budget_max": 350,
                "days_left": 4,
            }
        )
        warning_ids = {
            item["product_id"] for item in output["rule_result"]["soft_warnings"]
        }
        self.assertIn("g-017", warning_ids)

    def test_dify_request_uses_real_bounded_taobao_candidates(self):
        request = build_dify_request(
            {
                "occasion": "thank_you",
                "gift_mode": "purchased",
                "budget_min": 30,
                "budget_max": 300,
            }
        )
        candidates = json.loads(request["inputs"]["candidate_products"])
        # gift_request 现在是「payload JSON + 一行收礼人偏向说明」。
        # 后半行是给 LLM 的显式提示（见 build_dify_request 的注释），
        # 所以这里断言 payload 仍是完整前缀、且偏向说明按未指定写死。
        gift_request = request["inputs"]["gift_request"]
        self.assertTrue(
            gift_request.startswith(
                '{"occasion": "thank_you", "gift_mode": "purchased", "budget_min": 30, "budget_max": 300}'
            ),
            gift_request,
        )
        self.assertIn("收礼人偏向=未指定", gift_request)
        self.assertTrue(candidates)
        # 2026-11-15：Dify 候选池 24 → 36，配合「AI 精选 8 件」。
        # 断言跟着常量走，避免下次调参又要改测试。
        self.assertLessEqual(len(candidates), DIFY_CANDIDATE_LIMIT)
        # 2026-11-16：`id` 与 `product_id` 完全重复，瘦身时删掉了 `id`，
        # 统一用 `product_id`。断言跟着字段约定走。
        self.assertTrue(all(item["product_id"].isdigit() for item in candidates))
        self.assertTrue(all(item["currency"] == "CNY" for item in candidates))
        self.assertTrue(all(item["delivery_days"] is None for item in candidates))
        self.assertFalse(any(item["product_id"].startswith(("g-", "TEST-")) for item in candidates))
        self.assertFalse(any("merchant" in item for item in candidates))
        self.assertFalse(any("observed_at" in item for item in candidates))
        self.assertTrue(all(float(item["price"]) >= 30 for item in candidates))

    def test_dify_stream_returns_finished_workflow_outputs(self):
        output = {
            "candidates": [{"product_id": "p-1"}],
            "rule_result": {"hard_blocks": []},
            "decision_trace": {"filter_steps": {"eligible": 1}},
        }
        event = {
            "event": "workflow_finished",
            "data": {
                "status": "succeeded",
                "outputs": {"text": json.dumps(output)},
            },
        }
        stream = BytesIO(
            (
                "event: workflow_finished\n"
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            ).encode("utf-8")
        )

        response = _read_dify_stream(stream)
        normalized = normalize_dify_response(response)
        self.assertEqual(normalized["candidates"][0]["product_id"], "p-1")
        self.assertEqual(normalized["rule_result"], output["rule_result"])
        self.assertEqual(normalized["decision_trace"], output["decision_trace"])

    def test_dify_candidates_follow_purchase_and_diy_modes(self):
        purchased = get_dify_candidates({"gift_mode": "purchased"})
        diy = get_dify_candidates({"gift_mode": "diy"})
        combined = get_dify_candidates({"gift_mode": "all"})

        self.assertTrue(purchased)
        self.assertTrue(all(item["gift_type"] == "purchased" for item in purchased))
        self.assertTrue(all(float(item["price"]) > 1 for item in purchased))
        self.assertTrue(diy)
        self.assertTrue(all(item["gift_type"] == "diy" for item in diy))
        self.assertTrue(any(item["gift_type"] == "purchased" for item in combined))
        self.assertTrue(any(item["gift_type"] == "diy" for item in combined))

    def test_dify_request_keeps_diy_identity_and_title(self):
        request = build_dify_request({"gift_mode": "diy", "budget_min": 20, "budget_max": 80})
        candidates = json.loads(request["inputs"]["candidate_products"])
        self.assertTrue(candidates)
        self.assertTrue(all(item["gift_type"] == "diy" for item in candidates))
        self.assertTrue(all(item.get("product_id") for item in candidates))
        self.assertTrue(all(item.get("title") for item in candidates))

    def test_invalid_dify_output_is_rejected(self):
        self.assertIsNone(normalize_dify_response({"data": {"outputs": {"text": "ok"}}}))

    def test_dify_output_without_decision_trace_is_accepted(self):
        structured = {
            "intent": {"gift_mode": "diy"},
            "rule_result": {
                "eligible_products": ["diy-001"],
                "hard_blocks": [],
                "soft_warnings": [],
                "excluded_summary": [],
                "rules_version": "rules-v5",
            },
            "eligible_products": [
                {"product_id": "diy-001", "making_days": 3}
            ],
            "candidates": [
                {"product_id": "diy-001", "strategy": "heart", "rank": 1}
            ],
            "ranking_version": "ranker-v5",
            "card_message": "纪念日快乐。",
        }
        response = {
            "data": {
                "outputs": {
                    "text": json.dumps(structured, ensure_ascii=False)
                }
            }
        }

        normalized = normalize_dify_response(response)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["candidates"][0]["product_id"], "diy-001")
        self.assertEqual(
            normalized["decision_trace"],
            {
                "filter_steps": {},
                "selection_mode": "dify_workflow",
            },
        )

    def test_nested_dify_output_is_normalized(self):
        structured = {
            "candidates": [{"product_id": "g-001"}],
            "rule_result": {"hard_blocks": []},
            "decision_trace": {"filter_steps": {"catalog": 18}},
        }
        response = {"data": {"outputs": {"text": json.dumps(structured)}}}
        normalized = normalize_dify_response(response)
        self.assertEqual(normalized["candidates"][0]["product_id"], "g-001")
        self.assertEqual(normalized["rule_result"], structured["rule_result"])
        self.assertEqual(normalized["decision_trace"], structured["decision_trace"])

    def test_dify_output_accepts_empty_candidates_and_string_fields(self):
        response = {
            "data": {
                "outputs": {
                    "ranked_candidates": "[]",
                    "rule_result": "{\"hard_blocks\": []}",
                    "decision_trace": "{\"filter_steps\": {}}",
                }
            }
        }
        normalized = normalize_dify_response(response)
        self.assertEqual(normalized["candidates"], [])
        self.assertEqual(normalized["rule_result"]["hard_blocks"], [])
        self.assertEqual(normalized["decision_trace"]["filter_steps"], {})

    def test_dify_text_wrapper_and_display_metadata_are_normalized(self):
        structured = {
            "trace_id": "gr-test",
            "mode": "dify_workflow",
            "intent": {"gift_mode": "purchased"},
            "rule_result": {"eligible_product_ids": ["p-1"]},
            "decision_trace": {},
            "candidates": [
                {
                    "product_id": "p-1",
                    "title": "测试商品",
                    "strategy": "safe",
                    "price": "168.00",
                    "reason_evidence": ["价格在预算内"],
                }
            ],
        }
        text = (
            "<think>internal note</think>\n```json\n"
            + json.dumps(structured, ensure_ascii=False)
            + "\n```"
        )
        normalized = normalize_dify_response(
            {"data": {"outputs": {"text": text}}}
        )
        self.assertEqual(normalized["candidates"][0]["gift_type"], "purchased")
        self.assertEqual(normalized["candidates"][0]["title"], "测试商品")
        self.assertEqual(normalized["candidates"][0]["strategy_label"], "稳妥款")

    def test_dify_candidate_facts_are_restored_from_local_source(self):
        result = {
            "intent": {"gift_mode": "diy"},
            "candidates": [
                {
                    "product_id": "diy-001",
                    "strategy": "heart",
                    "reason_evidence": ["可个性化"],
                }
            ],
        }
        enriched = enrich_dify_result(
            result,
            {
                "gift_mode": "diy",
                "occasion": "birthday",
                "recipient_relation": "friend",
            },
        )
        card = enriched["candidates"][0]
        self.assertEqual(card["gift_type"], "diy")
        self.assertTrue(card["materials"])
        self.assertTrue(card["steps"])
        self.assertTrue(card["preservation"])
        # meaning 现在只在模型真的写了文案时才有值（llm_selected=True 且
        # 模型给了 meaning）；本地 enrich 路径不再用寓意表补——模板文案
        # 冒充模型文案的教训（2026-11-13）。这个测试没带模型文案，断言为空。
        self.assertEqual(card["meaning"], "")
        self.assertTrue(card["watch_out"])
        self.assertIn("source_url", card)
        self.assertEqual(card["cost"], "约 40–50 元")
        # price_note 现在只在「价格疑似偏低」的单品上挂一句核对提醒，
        # 不再往每张卡塞「按参考价上下约 10% 估算」那段免责话——那是
        # 所有卡共用的口径，收拢到页底 page_notices 里了。所以这里断言
        # 的是「不发散单位数」而不是某句固定文案。
        self.assertNotIn("¥", str(card.get("price_note") or ""))

    def test_fast_preview_returns_all_rule_eligible_candidates_without_model(self):
        payload = {
            "occasion": "birthday",
            "recipient_relation": "friend",
            "gift_mode": "purchased",
            "budget_min": 30,
            "budget_max": 300,
        }
        preview = build_fast_preview(payload)
        self.assertGreater(len(preview["candidates"]), 3)
        self.assertEqual(
            preview["decision_trace"]["selection_mode"],
            "instant_deterministic_preview",
        )
        self.assertTrue(
            all(card["gift_type"] == "purchased" for card in preview["candidates"])
        )
        self.assertTrue(
            all(
                card["price"] == "价格待核实"
                or card["price"].startswith("约 ")
                for card in preview["candidates"]
            )
        )
        # 价格口径不再逐卡重复，统一在 page_notices 里说一次。
        self.assertTrue(preview.get("page_notices"))
        self.assertFalse(any("¥" in str(card) for card in preview["candidates"]))

    def test_preview_candidate_limit_is_independent_from_dify_limit(self):
        payload = {
            "occasion": "birthday",
            "recipient_relation": "friend",
            "gift_mode": "purchased",
            "budget_min": 30,
            "budget_max": 300,
        }
        with patch("app.PREVIEW_CANDIDATE_LIMIT", 120), patch(
            "app.DIFY_CANDIDATE_LIMIT", 12
        ):
            preview = build_fast_preview(payload)
        self.assertGreater(len(get_dify_candidates(payload)), 0)
        # 2026-11-15：展示上限从 20 提到 30（AI 精选 8 + 更多选项 22），
        # 这里断言的是「展示上限独立于 Dify 池」，所以跟 DISPLAY_CANDIDATE_LIMIT 对齐。
        self.assertLessEqual(len(preview["candidates"]), DISPLAY_CANDIDATE_LIMIT)

    def test_unconfigured_dify_does_not_start_a_background_job(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(start_dify_job({"gift_mode": "purchased"}))

    def test_health_contract_includes_version_and_dify_status(self):
        from app import APP_VERSION, DIFY_MAX_RETRIES, dify_is_configured

        self.assertTrue(APP_VERSION.startswith("gift-right-"))
        self.assertGreaterEqual(DIFY_MAX_RETRIES, 0)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(dify_is_configured())

    def test_dify_results_keep_other_fast_eligible_candidates(self):
        payload = {
            "occasion": "birthday",
            "recipient_relation": "friend",
            "gift_mode": "purchased",
            "budget_min": 30,
            "budget_max": 300,
        }
        preview = build_fast_preview(payload)
        expected_count = len(preview["candidates"])
        picked = dict(preview["candidates"][0])
        picked["strategy"] = "safe"
        picked["strategy_label"] = "稳妥款"
        invented = {
            "product_id": "not-in-local-eligible-pool",
            "title": "模型新增候选",
            "strategy": "heart",
        }
        merged = merge_dify_with_preview(
            {
                "candidates": [picked, invented],
                "decision_trace": {"filter_steps": {}},
            },
            payload,
        )
        self.assertEqual(len(merged["candidates"]), expected_count)
        self.assertEqual(
            merged["candidates"][0]["product_id"],
            picked["product_id"],
        )
        self.assertNotIn(
            "not-in-local-eligible-pool",
            [card["product_id"] for card in merged["candidates"]],
        )
        self.assertEqual(
            merged["rule_result"]["eligible_product_ids"],
            [card["product_id"] for card in preview["candidates"]],
        )

    def test_custom_fields_are_preserved_and_diy_candidates_include_steps(self):
        output = recommend(
            {
                "occasion": "other",
                "occasion_label": "春节家庭聚会",
                "occasion_custom": "春节去亲戚家",
                "recipient_relation": "family",
                "relation_label": "长辈/亲戚",
                "relation_custom": "不太熟的长辈",
                "budget_min": 20,
                "budget_max": 100,
                "gift_mode": "diy",
                "recipient_region": "广东",
                "avoid_custom": "不要太高调",
            }
        )
        self.assertEqual(output["intent"]["occasion_custom"], "春节去亲戚家")
        self.assertEqual(output["intent"]["relation_custom"], "不太熟的长辈")
        self.assertTrue(output["candidates"])
        self.assertTrue(all(card["gift_type"] == "diy" for card in output["candidates"]))
        self.assertTrue(any(card["steps"] for card in output["candidates"]))
        self.assertIn("regional", output["taboo_notes"])

    def test_chat_is_limited_to_gift_knowledge(self):
        refusal = answer_gift_chat("帮我写一首诗")
        self.assertTrue(refusal["refusal"])
        unrelated = answer_gift_chat("帮我写一段与礼物无关的诗")
        self.assertTrue(unrelated["refusal"])
        answer = answer_gift_chat("手工黄油曲奇需要什么材料？")
        self.assertFalse(answer["refusal"])
        self.assertTrue(answer["related_gifts"])

    def test_recommendation_reason_uses_specific_facts_not_generic_copy(self):
        output = recommend(
            {
                "occasion": "birthday",
                "occasion_label": "生日",
                "recipient_relation": "friend",
                "relation_label": "朋友",
                "budget_max": 300,
                "free_text": "他喜欢喝咖啡",
            }
        )
        coffee = next(card for card in output["candidates"] if card["product_id"] == "g-001")
        self.assertIn("咖啡", coffee["why_this"])
        self.assertNotIn("表达目的相符", coffee["why_this"])
        self.assertNotIn("分寸相对合适", coffee["why_this"])

    def test_candidate_order_is_stable_for_same_request(self):
        intent = {
            "occasion": "birthday",
            "recipient_relation": "friend",
            "budget_max": 300,
        }
        initial = recommend(intent)
        repeated = recommend(intent)
        self.assertEqual(
            [card["product_id"] for card in initial["candidates"]],
            [card["product_id"] for card in repeated["candidates"]],
        )

    def test_partner_relation_is_supported_and_explained(self):
        output = recommend(
            {
                "occasion": "birthday",
                "occasion_label": "生日",
                "recipient_relation": "partner",
                "relation_label": "情侣/配偶",
                "budget_min": 100,
                "budget_max": 1000,
            }
        )
        self.assertTrue(output["candidates"])
        self.assertEqual(output["intent"]["relation_label"], "情侣/配偶")
        self.assertTrue(
            any("情侣/配偶" in evidence for card in output["candidates"] for evidence in card["reason_evidence"])
        )

    def test_recommendation_returns_every_eligible_candidate(self):
        output = recommend(
            {
                "occasion": "birthday",
                "recipient_relation": "friend",
                "budget_min": 0,
                "budget_max": 1000,
            }
        )
        self.assertEqual(
            len(output["candidates"]),
            len(output["rule_result"]["eligible_product_ids"]),
        )
        self.assertGreater(len(output["candidates"]), 6)
        candidate_ids = [card["product_id"] for card in output["candidates"]]
        self.assertEqual(len(candidate_ids), len(set(candidate_ids)))

    def test_fulfillment_estimate_is_removed_everywhere(self):
        # 时效功能已下线（2026-11-14）：卡片不再携带任何时效/地区估算字段。
        output = recommend(
            {
                "occasion": "birthday",
                "recipient_relation": "friend",
                "gift_mode": "purchased",
                "budget_max": 300,
                "recipient_region": "广东",
                "user_region": "北京",
            }
        )
        for card in output["candidates"]:
            self.assertNotIn("delivery_days", card)
            self.assertNotIn("delivery_days_min", card)
            self.assertNotIn("delivery_days_max", card)
            self.assertNotIn("recommended_prepare_days", card)
            self.assertNotIn("planning_label", card)
            self.assertNotIn("region_note", card)
            self.assertEqual(card.get("fulfillment_estimate", ""), "")


class RecipientGenderTests(unittest.TestCase):
    """收礼人偏向：用户选了才影响排序，没选完全不变。"""

    def test_gender_score_is_zero_when_unspecified(self):
        from app import _gender_score

        for title in ("Urovia蓝风铃之泪香水", "凌豹K98机械键盘", "三只松鼠坚果礼盒"):
            for gender in ("", None, "unspecified", "unknown"):
                self.assertEqual(
                    _gender_score({"title": title}, {"recipient_gender": gender}),
                    0,
                    f"{title} / {gender!r} 不该被加减分",
                )

    def test_gender_score_direction(self):
        from app import _gender_score

        perfume = {"title": "Urovia蓝风铃之泪香水（法式清冷款）"}
        keyboard = {"title": "凌豹K98机械键盘（热插拔RGB宏编程）"}
        neutral = {"title": "三只松鼠臻悦礼纯坚果礼盒"}

        self.assertLess(_gender_score(perfume, {"recipient_gender": "male"}), 0)
        self.assertGreater(_gender_score(perfume, {"recipient_gender": "female"}), 0)
        self.assertGreater(_gender_score(keyboard, {"recipient_gender": "male"}), 0)
        self.assertLess(_gender_score(keyboard, {"recipient_gender": "female"}), 0)
        self.assertEqual(_gender_score(neutral, {"recipient_gender": "male"}), 0)

    def test_male_pool_has_fewer_female_coded_items(self):
        """偏男性候选集里女性向商品必须少于不指定——这是用户报的那个问题。

        2026-11-17：这个测试原来的女性向词表里有「护肤」「水乳」，把
        「欧莱雅男士护肤礼盒」「AHC男士水乳面膜三件套」也算成女性向，
        于是男性池里永远有 2 件「女性向」，断言 strict less 必然失败。

        那两件标题里明写「男士」，正是该推荐给男性的东西。所以判据改成
        跟 app._gender_mismatch 一致：词命中且标题没有同性别豁免词才算。
        """
        from app import get_dify_candidates

        female_words = ("香水", "香氛", "护肤", "水乳", "口红", "项链", "吊坠",
                        "耳环", "手链", "花束", "玫瑰", "手账", "玩偶")
        male_exempt = ("男士", "男款", "男式", "男友", "爸爸", "父亲",
                       "送老公", "男朋友")
        base = {
            "occasion": "birthday",
            "occasion_label": "生日",
            "recipient_relation": "friend",
            "relation_label": "朋友",
            "gift_mode": "purchased",
            "budget_min": 50,
            "budget_max": 300,
        }

        def is_female_coded(title):
            if any(w in title for w in male_exempt):
                return False
            return any(w in title for w in female_words)

        def female_count(gender):
            pool = get_dify_candidates(dict(base, recipient_gender=gender))
            titles = [c.get("title") or "" for c in pool]
            return sum(1 for t in titles if is_female_coded(t)), len(pool)

        male_n, male_total = female_count("male")
        none_n, none_total = female_count("")
        self.assertGreater(male_total, 0)
        self.assertLess(
            male_n / male_total,
            none_n / none_total,
            f"偏男性女性向占比 {male_n}/{male_total} 未低于不指定 {none_n}/{none_total}",
        )


if __name__ == "__main__":
    unittest.main()
