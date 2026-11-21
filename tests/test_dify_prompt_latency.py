"""Contract checks for exported Dify workflow after latency edits."""

import os
import unittest
import zipfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DSL = ROOT / "deliverables/performance_2026-11-16/GiftRight_draft_compact_import.ifpkg"


def _workflow():
    path = Path(os.getenv("GIFTRIGHT_DIFY_DSL", str(DEFAULT_DSL)))
    with zipfile.ZipFile(path) as archive:
        return yaml.safe_load(archive.read("app.yaml"))["workflow"]["graph"]


class DifyPromptLatencyContract(unittest.TestCase):
    def test_need_parser_does_not_read_candidate_catalog(self):
        graph = _workflow()
        parser = next(node for node in graph["nodes"] if node["id"] == "1792292308073")
        prompts = parser["data"]["prompt_template"]
        user = next(item["text"] for item in prompts if item["role"] == "user")
        system = next(item["text"] for item in prompts if item["role"] == "system")
        self.assertIn("gift_request", user)
        self.assertNotIn("candidate_products", user)
        self.assertNotIn("candidate_products", system)
        self.assertIn("search_query", system)

    def test_ranker_does_not_repeat_rule_nodes_product_knowledge(self):
        graph = _workflow()
        ranker = next(node for node in graph["nodes"] if node["id"] == "1792294044029")
        user = next(item["text"] for item in ranker["data"]["prompt_template"] if item["role"] == "user")
        self.assertIn("{{#ranker_context_20261116.ranker_context#}}", user)
        self.assertIn("{{#ranker_context_20261116.knowledge_context#}}", user)
        self.assertNotIn("{{#1792293113071.result#}}", user)
        self.assertNotIn("{{#1792830184658.result#}}", user)
        self.assertNotIn("{{#1792316098509.result#}}", user)
        self.assertNotIn("{{#1792831342745.result#}}", user)
        self.assertNotIn("{{#1792833782604.result#}}", user)

    def test_card_writer_returns_only_short_text(self):
        graph = _workflow()
        card = next(node for node in graph["nodes"] if node["id"] == "1792296553557")
        prompts = card["data"]["prompt_template"]
        system = next(item["text"] for item in prompts if item["role"] == "system")
        user = next(item["text"] for item in prompts if item["role"] == "user")
        self.assertIn("只输出祝福语纯文本", system)
        self.assertIn("不要返回 JSON", user)
        self.assertNotIn("完整原样返回", user)


if __name__ == "__main__":
    unittest.main()
