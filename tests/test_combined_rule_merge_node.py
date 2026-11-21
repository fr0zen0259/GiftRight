import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dify.combined_rule_merge_node import main


class CombinedRuleMergeNodeTests(unittest.TestCase):
    def run_merge(self, recipient_region="广东省广州市", estimates=None, days_left=5):
        request = {
            "gift_mode": "purchased",
            "budget_min": 30,
            "budget_max": 100,
            "days_left": days_left,
            "user_region": "浙江省杭州市",
            "recipient_region": recipient_region,
        }
        product = {
            "id": "859791451454",
            "name": "活动礼品商务定制logo实用办公室小礼物",
            "price": 39.8,
            "currency": "CNY",
            "origin": "广东 深圳",
            "category": "其他",
            "status": "active",
        }
        result = main(
            structured_output=json.dumps(request, ensure_ascii=False),
            purchased_knowledge=json.dumps([product], ensure_ascii=False),
            diy_knowledge="[]",
            delivery_estimates=json.dumps(estimates or [], ensure_ascii=False),
        )
        return json.loads(result["result"])

    def test_price_range_is_added_from_snapshot_and_unknown_route_stays_null(self):
        result = self.run_merge()
        candidate = result["eligible_products"][0]

        self.assertEqual(candidate["price_estimate"]["average_price"], 39.8)
        self.assertEqual(candidate["price_estimate"]["range_min"], 35.82)
        self.assertEqual(candidate["price_estimate"]["range_max"], 43.78)
        self.assertIsNone(candidate["delivery_estimate"]["average_days"])
        self.assertIsNone(candidate["delivery_estimate"]["range_min_days"])
        self.assertIsNone(candidate["delivery_estimate"]["range_max_days"])
        self.assertIn("快递公司小程序或官网", candidate["delivery_estimate"]["reminder"])
        self.assertNotIn("soft_warnings", candidate)
        self.assertFalse(any("时效暂未确认" in item for item in result["soft_warnings"]))

    def test_route_estimate_uses_minus_one_plus_two_range(self):
        estimates = [
            {
                "product_id": "859791451454",
                "average_days": 2,
                "confidence": "low",
                "basis": "深圳至广州路线经验估算",
                "origin": "广东 深圳",
                "destination": "广东省广州市",
            }
        ]
        result = self.run_merge(estimates=estimates)
        estimate = result["eligible_products"][0]["delivery_estimate"]

        self.assertEqual(estimate["average_days"], 2)
        self.assertEqual(estimate["range_min_days"], 1)
        self.assertEqual(estimate["range_max_days"], 4)
        self.assertIn("非商家承诺", estimate["reminder"])

    def test_tight_timeline_warns_but_keeps_candidate(self):
        estimates = [
            {
                "product_id": "859791451454",
                "average_days": 4,
                "origin": "广东 深圳",
                "destination": "广东省广州市",
            }
        ]
        result = self.run_merge(estimates=estimates, days_left=3)

        self.assertEqual(len(result["eligible_products"]), 1)
        self.assertTrue(
            any(
                "建议尽快准备并发货" in item
                for item in result["eligible_products"][0]["soft_warnings"]
            )
        )

    def test_route_estimate_is_not_used_without_destination(self):
        estimates = [
            {
                "product_id": "859791451454",
                "average_days": 2,
                "origin": "广东 深圳",
            }
        ]
        result = self.run_merge(recipient_region="", estimates=estimates)
        estimate = result["eligible_products"][0]["delivery_estimate"]

        self.assertIsNone(estimate["average_days"])
        self.assertIsNone(estimate["range_min_days"])
        self.assertIsNone(estimate["range_max_days"])

    def test_route_estimate_for_different_destination_is_rejected(self):
        estimates = [
            {
                "product_id": "859791451454",
                "average_days": 2,
                "origin": "广东 深圳",
                "destination": "浙江省杭州市",
            }
        ]
        result = self.run_merge(estimates=estimates)
        estimate = result["eligible_products"][0]["delivery_estimate"]

        self.assertIsNone(estimate["average_days"])
        self.assertIsNone(estimate["range_min_days"])
        self.assertIsNone(estimate["range_max_days"])

    def test_budget_lower_bound_is_enforced_by_hard_rule_node(self):
        from dify.hard_rule_node import main as hard_rule_main

        request = {
            "occasion": "birthday",
            "gift_mode": "purchased",
            "budget_min": 100,
            "budget_max": 300,
        }
        products = [
            {
                "product_id": "low",
                "name": "低价礼盒",
                "gift_type": "purchased",
                "price": 68,
                "status": "active",
            },
            {
                "product_id": "in-range",
                "name": "预算内礼盒",
                "gift_type": "purchased",
                "price": 168,
                "status": "active",
            },
        ]
        result = json.loads(
            hard_rule_main(
                raw_gift_request=json.dumps(request, ensure_ascii=False),
                candidate_products=json.dumps(products, ensure_ascii=False),
            )["result"]
        )
        self.assertEqual(
            [item["product_id"] for item in result["eligible_products"]],
            ["in-range"],
        )
        self.assertEqual(result["excluded"][0]["product_id"], "low")
        self.assertIn("预算下限", result["excluded"][0]["reasons"][0])


if __name__ == "__main__":
    unittest.main()
