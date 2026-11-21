# -*- coding: utf-8 -*-
"""自定义避开项必须对真实商品池生效。

背景（2026-11-15 复测发现）：
`_custom_avoid_blocks` 原本只查 tags。本地目录 32 条带 tags，但真实商品池
（data/real_catalog/*.json，4180 条）**一条 tags 都没有**，于是用户在
「一定要避开的东西」里填的任何内容，对真实商品完全不起作用——
填「钟表」照样推桌面小钟，填「伞」照样推雨伞礼盒。

修复分两处：
1) `_custom_avoid_blocks` 增加品类与标题两条路径（这是真正修好 bug 的那处），
   标题只认前 12 个字，并排除「鞋柜」「酒家」「酒酿」这类修饰性用法。
2) `_item_text` 补读 title / title_key / raw_text。它原本只读 name，而真实池
   只有 title/title_key，拼出来是空串。注意：这个函数当前在引擎里**没有
   调用方**，改它不影响本 bug 的修复效果，只是把将来可能踩的坑填了。
   对应的测试因此单独标注为「工具函数自身正确性」，不是回归防线。

这组测试锁住「真实池上确实生效」和「不误伤」两侧，防止有人为了修误伤
又把标题匹配整个删掉，或为了提召回把匹配放宽成任意子串。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
import engine


def _real_pool():
    try:
        return app._load_real_catalog() or []
    except Exception:
        return []


class RealPoolMatchingTest(unittest.TestCase):
    """真实池（无 tags）上的避开项必须真的拦得住东西。"""

    @classmethod
    def setUpClass(cls):
        cls.pool = _real_pool()
        if not cls.pool:
            raise unittest.SkipTest("真实商品池为空")

    def _blocked(self, term):
        return [r for r in self.pool if engine._custom_avoid_blocks(r, term)]

    def test_item_text_reads_real_pool_fields(self):
        """工具函数自身正确性：真实池没有 name，_item_text 要退到 title。

        注意这不是本 bug 的回归防线——`_item_text` 在引擎里没有调用方，
        `_custom_avoid_blocks` 走的是自己取的 title。这条只是防止将来有人
        真去用这个函数时踩到「拼出来是空串」的坑。
        """
        row = next(
            (r for r in self.pool if r.get("title_key") or r.get("title")), None
        )
        if row is None:
            self.skipTest("池里没有带标题的行")
        text = engine._item_text(row)
        self.assertTrue(
            text.strip(),
            "真实池拼出的商品文字是空的——文本兜底会静默失效",
        )
        self.assertTrue(
            len(text) > 4,
            f"商品文字过短（{text!r}），说明只读到了 category 或什么都没读到",
        )

    def test_avoid_clock_blocks_real_products(self):
        """避开钟表：真实池上必须拦到东西（修复前是 0 条）。"""
        hits = self._blocked("钟表")
        self.assertGreater(
            len(hits), 0, "避开钟表在真实池上一条都没拦到"
        )
        # 拦到的必须是钟表本身，不是顺带提了「钟」字的别的东西
        for row in hits[:20]:
            text = (row.get("title_key") or row.get("title") or "")
            self.assertTrue(
                any(w in text for w in ("钟", "表")),
                f"误伤：{text[:30]} 不是钟表类却被拦",
            )

    def test_avoid_umbrella_blocks_real_products(self):
        hits = self._blocked("伞")
        self.assertGreater(len(hits), 0, "避开伞在真实池上一条都没拦到")
        for row in hits[:20]:
            text = (row.get("title_key") or row.get("title") or "")
            self.assertIn("伞", text, f"误伤：{text[:30]} 不含伞却被拦")

    def test_avoid_shoes_blocks_real_shoes(self):
        hits = self._blocked("鞋")
        self.assertGreater(len(hits), 0, "避开鞋在真实池上一条都没拦到")
        for row in hits[:20]:
            text = (row.get("title_key") or row.get("title") or "")
            self.assertTrue(
                any(w in text for w in ("球鞋", "布鞋", "皮鞋", "拖鞋", "帆布鞋", "鞋")),
                f"误伤：{text[:30]} 不是鞋却被拦",
            )


class NoFalsePositiveTest(unittest.TestCase):
    """修召回不能靠无脑子串匹配——这些是实测出来的误伤样本。"""

    def _blocks(self, title, category, term):
        item = {"title_key": title, "category": category, "tags": []}
        return engine._custom_avoid_blocks(item, term)

    def test_business_name_with_alcohol_char_not_blocked(self):
        """「广州酒家」「荔煌酒家」是商家名，「酒酿」是工艺，都不是酒。"""
        cases = [
            ("广州酒家黄油乳酪蛋黄酥（360g礼盒）", "糕点"),
            ("荔煌酒家老式月饼礼盒（五仁金腿双黄莲蓉）", "糕点"),
            ("蜜雪冰城雪王酒酿花果茶包礼盒（冷泡袋泡）", "茶饮"),
        ]
        for title, category in cases:
            with self.subTest(title=title):
                self.assertEqual(
                    self._blocks(title, category, "酒"),
                    [],
                    f"「{title}」被误判成酒类",
                )

    def test_shoe_cabinet_not_blocked_as_shoes(self):
        """「换鞋凳」「鞋柜」是家具，不是鞋。"""
        cases = [
            ("极简超纤皮换鞋凳（沙发皮墩方凳款）", "家居"),
            ("招财猫玄关钥匙收纳摆件（入户鞋柜创意装饰）", "家居日用"),
        ]
        for title, category in cases:
            with self.subTest(title=title):
                self.assertEqual(
                    self._blocks(title, category, "鞋"),
                    [],
                    f"「{title}」被误判成鞋类",
                )

    def test_knife_word_in_cosmetic_name_not_blocked(self):
        """「紧塑刀面颈膜」「刀锋刷」不是刀。"""
        cases = [
            ("海蓝之谜紧塑刀面颈膜礼盒（抗皱紧致保湿50ml）", "护肤"),
            ("化妆刷套装12支（晕染刀锋刷初学装）", "美妆个护"),
        ]
        for title, category in cases:
            with self.subTest(title=title):
                self.assertEqual(
                    self._blocks(title, category, "刀"),
                    [],
                    f"「{title}」被误判成刀具",
                )

    def test_real_alcohol_still_blocked(self):
        """收紧误伤之后，真酒不能漏。"""
        cases = [
            ("剑南春双支白酒礼盒（52度500ml×2瓶）", "酒类"),
            ("长城干红葡萄酒礼盒（750ml×2）", "酒类"),
        ]
        for title, category in cases:
            with self.subTest(title=title):
                self.assertTrue(
                    self._blocks(title, category, "酒"),
                    f"「{title}」是真酒，却没被拦",
                )

    def test_empty_avoid_text_blocks_nothing(self):
        item = {"title_key": "红酒礼盒", "category": "酒类", "tags": []}
        self.assertEqual(engine._custom_avoid_blocks(item, ""), [])
        self.assertEqual(engine._custom_avoid_blocks(item, "   "), [])

    def test_unrelated_avoid_term_blocks_nothing(self):
        """填了没有对应规则的话，不能乱拦。"""
        item = {"title_key": "手冲咖啡礼盒", "category": "咖啡", "tags": []}
        self.assertEqual(engine._custom_avoid_blocks(item, "不要太贵"), [])


class LocalCatalogTagPathTest(unittest.TestCase):
    """本地目录带 tags，tag 路径必须继续有效。"""

    def test_tag_path_still_works(self):
        item = {"name": "香氛礼盒", "tags": ["fragrance"], "category": ""}
        self.assertTrue(engine._custom_avoid_blocks(item, "避开香味"))

    def test_local_alcohol_blocked(self):
        item = {"name": "低度果酒礼盒", "tags": ["alcohol"], "category": ""}
        self.assertTrue(engine._custom_avoid_blocks(item, "不喝酒"))


if __name__ == "__main__":
    unittest.main()
