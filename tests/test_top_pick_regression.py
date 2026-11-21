# -*- coding: utf-8 -*-
"""P0-3 回归：没有模型文案的卡不得标「最贴合这次送礼」。

审计原文（第二轮复测 P0-3）：
    「最贴合」只能给能覆盖用户关键偏好且没有明显关系/审美风险的候选……
    不要用更强文案掩盖排序问题。

当时的实际故障是：后端无条件把 `is_llm_copy` 写成 True，再用本地寓意表 /
理由模板补空，于是模板文案被当成模型文案展示，前端据此给卡挂上
「最贴合这次送礼」——而顾问对同一件商品的回答可能是「不是最贴合」。
更强的文案掩盖了排序问题，这正是审计要求禁止的那件事。

修复后 `is_llm_copy` 的判定是：
    llm_copy = llm_selected and (has_llm_meaning or llm_why_from_model)

这组测试锁住这个判定，防止有人把默认值改回 True、或让模板文案顶替模型文案。
它管不到「顾问说了什么」——顾问回答是自然语言，解析它不稳定，故不做。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
from app import enrich_dify_result


# 一个真实存在于本地目录里的成品，保证 enrich 能补全事实字段。
PRODUCT_ID = "p-001"


def _catalog_id():
    for item in app.CATALOG:
        for key in ("id", "product_id", "item_id", "offer_id"):
            value = str(item.get(key) or "")
            if value:
                return value
    return None


class NoReasonMeansNoTopPickTest(unittest.TestCase):
    """「最贴合」的必要条件：模型既选中了卡，又真的写了文案。"""

    def setUp(self):
        self.pid = _catalog_id()
        if not self.pid:
            self.skipTest("本地目录为空，无法构造成品卡")
        self.payload = {
            "occasion": "birthday",
            "recipient_relation": "friend",
            "gift_mode": "purchased",
        }

    def _enrich(self, candidate, llm_selected):
        result = {"intent": {"gift_mode": "purchased"}, "candidates": [candidate]}
        enriched = enrich_dify_result(result, self.payload, llm_selected=llm_selected)
        return enriched["candidates"][0]

    def test_local_rule_path_is_never_llm_copy(self):
        """本地规则挑出的卡，即便补了寓意表文案，也不算模型文案。"""
        card = self._enrich(
            {"product_id": self.pid, "strategy": "heart"},
            llm_selected=False,
        )
        self.assertFalse(
            card.get("is_llm_copy"),
            "本地规则路径不得标成模型文案——否则前端会挂「最贴合这次送礼」",
        )
        self.assertEqual(card.get("copy_source"), "template")
        self.assertEqual(card.get("meaning"), "", "模板寓意不得冒充模型文案")

    def test_llm_selected_without_copy_is_not_llm_copy(self):
        """模型只排了序、没写任何文案时，它仍是一张规则卡。"""
        card = self._enrich(
            {"product_id": self.pid, "strategy": "heart"},
            llm_selected=True,
        )
        self.assertFalse(
            card.get("is_llm_copy"),
            "模型没给 meaning / why_this 时不得算模型文案",
        )
        self.assertEqual(card.get("copy_source"), "template")

    def test_llm_selected_with_real_meaning_is_llm_copy(self):
        """正向对照：模型真写了寓意，才允许标模型文案。"""
        card = self._enrich(
            {
                "product_id": self.pid,
                "strategy": "heart",
                "meaning": "这套摆件放在桌上，提醒她那天有人惦记着她",
            },
            llm_selected=True,
        )
        self.assertTrue(
            card.get("is_llm_copy"),
            "模型真的写了寓意，应算模型文案",
        )
        self.assertEqual(card.get("copy_source"), "llm")

    def test_placeholder_meaning_does_not_qualify(self):
        """占位话术不算文案——它是模板，不是模型写的。"""
        card = self._enrich(
            {
                "product_id": self.pid,
                "strategy": "heart",
                "meaning": "价格在预算内，候选通过当前硬约束检查",
            },
            llm_selected=True,
        )
        self.assertFalse(
            card.get("is_llm_copy"),
            "占位话术不得让卡升级成模型文案",
        )


class FrontendTopPickGateTest(unittest.TestCase):
    """前端的「最贴合」标签只在 is_llm_copy 且确有文案时出现。

    这里用文本断言而不是跑浏览器：app.js 没有模块导出，跑它需要整套 DOM。
    断言的是那段判定代码本身还在，且没有退化成宽松的真值判断。
    """

    def setUp(self):
        self.js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8"
        )

    def test_llm_copy_requires_strict_true(self):
        self.assertIn(
            "card.is_llm_copy === true",
            self.js,
            "is_llm_copy 必须严格等于 true；`!== false` 会让漏标的卡冒充 AI 精选",
        )

    def test_top_pick_label_requires_ai_state(self):
        self.assertIn(
            'cardState === "ai"',
            self.js,
            "「最贴合这次送礼」只能在 cardState 为 ai 时出现",
        )

    def test_ai_state_requires_reason(self):
        self.assertIn(
            "isLlmCard && hasReason",
            self.js,
            "ai 态必须同时满足「模型文案」与「确有理由」，否则只是备选卡",
        )


if __name__ == "__main__":
    unittest.main()
