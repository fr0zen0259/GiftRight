import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


class DifyBudgetDisplayGuardTests(unittest.TestCase):
    def test_dify_price_is_not_used_for_display(self):
        payload = {"budget_min": 100, "budget_max": 300}
        preview_card = {
            "product_id": "valid-001",
            "title": "预算内礼盒",
            "gift_type": "purchased",
            "gift_type_label": "购买礼物",
            "category": "礼品",
            "price": "约 151–185 元",
            "price_band": "约 151–185 元",
            "price_note": "本地价格说明",
            "source": "catalog",
        }
        dify_result = {
            "candidates": [
                {
                    "product_id": "valid-001",
                    "title": "错误标题",
                    "price": "约 61–75 元",
                    "price_band": "约 61–75 元",
                    "source_url": "https://example.invalid/item",
                    "why_this": "Dify 排序理由",
                },
                {
                    "product_id": "low-001",
                    "title": "低于预算的商品",
                    "price": "约 30 元",
                },
            ]
        }

        with patch.object(
            app,
            "build_fast_preview",
            return_value={"candidates": [preview_card], "rule_result": {}},
        ):
            result = app.merge_dify_with_preview(dify_result, payload)

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["product_id"], "valid-001")
        self.assertEqual(candidate["title"], "预算内礼盒")
        self.assertEqual(candidate["price"], "约 151–185 元")
        self.assertEqual(candidate["why_this"], "Dify 排序理由")

    def test_empty_dify_ranking_keeps_local_preview(self):
        payload = {"budget_min": 100, "budget_max": 300}
        fallback = {"candidates": [{"product_id": "valid-001"}]}
        empty_preview = {"candidates": [], "rule_result": {}}
        with patch.object(app, "maybe_call_dify", return_value={"result": "{}"}), patch.object(
            app, "normalize_dify_response", return_value={"candidates": []}
        ), patch.object(
            app,
            "build_fast_preview",
            side_effect=[empty_preview, fallback],
        ):
            app._run_dify_job("job-empty", payload)

        with app.DIFY_JOBS_LOCK:
            job = dict(app.DIFY_JOBS["job-empty"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["payload"]["source"], "local_rule_mode")
        self.assertEqual(job["payload"]["dify_status"], "empty_result_fallback")
        self.assertEqual(job["payload"]["result"], fallback)

    def test_invalid_dify_output_keeps_local_preview(self):
        payload = {"budget_min": 100, "budget_max": 300}
        fallback = {"candidates": [{"product_id": "valid-001"}]}
        with patch.object(app, "maybe_call_dify", return_value={"data": {"outputs": "bad"}}), patch.object(
            app, "normalize_dify_response", return_value=None
        ), patch.object(app, "build_fast_preview", return_value=fallback):
            app._run_dify_job("job-invalid", payload)

        with app.DIFY_JOBS_LOCK:
            job = dict(app.DIFY_JOBS["job-invalid"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["payload"]["source"], "local_rule_mode")
        self.assertEqual(
            job["payload"]["dify_status"],
            "invalid_structured_output_fallback",
        )
        self.assertEqual(job["payload"]["result"], fallback)


if __name__ == "__main__":
    unittest.main()
