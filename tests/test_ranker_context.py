import json
import unittest

from dify.ranker_context import main


class RankerContextTests(unittest.TestCase):
    def test_keeps_eligible_facts_and_drops_repeated_estimates(self):
        source = {
            "intent": {"occasion": "birthday", "hard_preferences": ["avoid_fragrance"], "free_text": "喜欢手写"},
            "rule_result": {"eligible_products": ["tea-1"], "hard_blocks": [{"product_id": "scent-1"}], "soft_warnings": ["到货待确认"]},
            "eligible_products": [{"product_id": "tea-1", "title": "茶具", "gift_type": "purchased", "price": 120, "tags": ["tea"], "description": "适合泡茶", "risk_notes": "易碎", "price_estimate": {"reminder": "价格待核实"}, "delivery_estimate": {"reminder": "到货待确认"}}],
        }
        result = main(json.dumps(source, ensure_ascii=False), [], [])
        compact = json.loads(result["ranker_context"])
        self.assertEqual(compact["intent"], source["intent"])
        self.assertEqual(compact["rule_result"]["eligible_products"], ["tea-1"])
        self.assertEqual(compact["eligible_products"][0]["risk_notes"], "易碎")
        self.assertNotIn("price_estimate", result["ranker_context"])
        self.assertNotIn("delivery_estimate", result["ranker_context"])
        self.assertNotIn("scent-1", result["ranker_context"])

    def test_knowledge_keeps_all_short_safety_content_without_metadata(self):
        occasion = [{"content": "生日没有统一禁忌", "metadata": {"segment_id": "secret-meta"}}]
        taboo = [{"content": "钟表是软提醒，不可当成绝对禁忌", "metadata": {"score": 0.7}}]
        result = main("{}", occasion, taboo)
        self.assertIn("生日没有统一禁忌", result["knowledge_context"])
        self.assertIn("钟表是软提醒，不可当成绝对禁忌", result["knowledge_context"])
        self.assertNotIn("secret-meta", result["knowledge_context"])
        self.assertNotIn("score", result["knowledge_context"])


if __name__ == "__main__":
    unittest.main()
