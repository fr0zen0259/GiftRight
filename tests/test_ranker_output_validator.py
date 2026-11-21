import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dify.ranker_output_validator import main


class RankerOutputValidatorTests(unittest.TestCase):
    def test_preserves_rule_audit_and_full_eligible_products(self):
        rules = {
            "intent": {
                "occasion": "birthday",
                "recipient_relation": "friend",
                "gift_mode": "purchased",
                "budget_min": 30,
                "budget_max": 100,
                "hard_preferences": ["non_alcoholic"],
            },
            "rule_result": {
                "eligible_products": ["tea-1"],
                "hard_blocks": [
                    {
                        "product_id": "wine-1",
                        "name": "红酒礼盒",
                        "reasons": ["命中明确禁忌：酒类或含酒精商品"],
                    }
                ],
                "soft_warnings": ["route unknown"],
                "excluded_summary": [],
                "rules_version": "rules-v5",
            },
            "eligible_products": [
                {"product_id": "tea-1", "name": "茶礼", "price_estimate": {
                    "average_price": 68, "range_min": 61.2, "range_max": 74.8,
                    "currency": "CNY",
                }},
            ],
            "route_estimate": {"average_days": None},
        }
        ranked = {"candidates": [{"product_id": "tea-1", "strategy": "safe"}]}

        result = json.loads(main(arg1=json.dumps(ranked), arg2=json.dumps(rules))["result"])

        self.assertEqual(result["intent"], rules["intent"])
        self.assertEqual(result["rule_result"], rules["rule_result"])
        self.assertEqual(result["eligible_products"], rules["eligible_products"])
        self.assertEqual([x["product_id"] for x in result["candidates"]], ["tea-1"])

    def test_rejects_ineligible_ids_and_restores_estimates_from_rules(self):
        price_estimate = {
            "average_price": 39.8,
            "range_min": 35.82,
            "range_max": 43.78,
            "currency": "CNY",
            "confidence": "low",
            "basis": "from snapshot",
            "reminder": "check price",
        }
        delivery_estimate = {
            "average_days": 2,
            "range_min_days": 1,
            "range_max_days": 4,
            "confidence": "low",
            "basis": "route estimate",
            "reminder": "check carrier",
        }
        ranked = {
            "candidates": [
                {
                    "product_id": "valid",
                    "strategy": "heart",
                    "rank": 9,
                    "reason_evidence": ["title fact", "category fact", "extra"],
                    "risk_note": "unsupported model guess",
                    "price_estimate": {"average_price": 1},
                    "delivery_estimate": {"average_days": 99},
                    "soft_warnings": ["check carrier", "配送/制作时效暂未确认"],
                },
                {"product_id": "not-eligible", "strategy": "safe", "rank": 10},
            ]
        }
        rules = {
            "eligible_products": [
                {
                    "product_id": "valid",
                    "price_estimate": price_estimate,
                    "delivery_estimate": delivery_estimate,
                }
            ]
        }

        result = json.loads(
            main(ranker_output=json.dumps(ranked), rule_result=json.dumps(rules))["result"]
        )

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["rank"], 1)
        self.assertEqual(candidate["reason_evidence"], ["title fact", "category fact"])
        self.assertEqual(candidate["risk_note"], "")
        self.assertEqual(candidate["price_estimate"], price_estimate)
        self.assertEqual(candidate["delivery_estimate"], delivery_estimate)
        self.assertEqual(candidate["soft_warnings"], [])

    def test_keeps_only_explicit_product_risk(self):
        ranked = {
            "candidates": [
                {
                    "product_id": "p1",
                    "risk_note": "刺激性气味",
                    "reason_evidence": ["标题明确描述"],
                }
            ]
        }
        rules = {
            "eligible_products": [
                {
                    "product_id": "p1",
                    "risk_notes": "刺激性气味，使用前确认偏好",
                }
            ]
        }

        result = json.loads(
            main(ranker_output=json.dumps(ranked), rule_result=json.dumps(rules))["result"]
        )
        self.assertEqual(result["candidates"][0]["risk_note"], "刺激性气味")

    def test_normalizes_incomplete_estimate_objects(self):
        ranked = {
            "candidates": [
                {
                    "product_id": "p1",
                    "reason_evidence": ["事实"],
                    "price_estimate": {"average_price": 1},
                    "delivery_estimate": {"average_days": 99},
                }
            ]
        }
        rules = {
            "eligible_products": [
                {
                    "product_id": "p1",
                    "price_estimate": {"average_price": 42, "currency": "CNY"},
                    "delivery_estimate": {"average_days": None, "range_min_days": None, "range_max_days": None},
                }
            ]
        }
        result = json.loads(
            main(ranker_output=json.dumps(ranked), rule_result=json.dumps(rules))["result"]
        )
        candidate = result["candidates"][0]
        self.assertIsNone(candidate["price_estimate"]["average_price"])
        self.assertIsNone(candidate["price_estimate"]["range_min"])
        self.assertIsNone(candidate["price_estimate"]["range_max"])
        self.assertIn("完整的价格或成本估算", candidate["price_estimate"]["basis"])
        self.assertIsNone(candidate["delivery_estimate"]["average_days"])
        self.assertIn("无法计算具体路线时效", candidate["delivery_estimate"]["reminder"])

    def test_final_validator_drops_products_below_budget_minimum(self):
        rules = {
            "intent": {"budget_min": 100, "budget_max": 300},
            "eligible_products": [
                {"product_id": "low", "price": 68},
                {"product_id": "valid", "price": 168},
            ],
        }
        ranked = {
            "candidates": [
                {"product_id": "low", "strategy": "safe"},
                {"product_id": "valid", "strategy": "heart"},
            ]
        }
        result = json.loads(
            main(ranker_output=json.dumps(ranked), rule_result=json.dumps(rules))["result"]
        )
        self.assertEqual(
            [item["product_id"] for item in result["eligible_products"]],
            ["valid"],
        )
        self.assertEqual(
            [item["product_id"] for item in result["candidates"]],
            ["valid"],
        )


if __name__ == "__main__":
    unittest.main()
