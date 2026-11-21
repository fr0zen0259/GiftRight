import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import PRODUCTS, evaluate_product, recommend


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "eval" / "scenarios.json"


def main():
    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    total = 0
    violations = 0
    expected_hits = 0
    expected_total = 0
    results = []

    for scenario in scenarios:
        output = recommend(scenario["intent"])
        candidate_ids = {item["product_id"] for item in output["candidates"]}
        candidate_products = [item for item in PRODUCTS if item["id"] in candidate_ids]
        forbidden = set(scenario.get("must_not_recommend_tags", []))
        for product in candidate_products:
            total += 1
            if forbidden.intersection(product.get("tags", [])):
                violations += 1

        expected = set(scenario.get("must_include_any", []))
        expected_total += 1
        if candidate_ids.intersection(expected):
            expected_hits += 1

        results.append(
            {
                "id": scenario["id"],
                "label": scenario["label"],
                "candidate_ids": sorted(candidate_ids),
                "hard_block_count": len(output["rule_result"]["hard_blocks"]),
                "soft_warning_count": len(output["rule_result"]["soft_warnings"]),
                "trace_id": output["trace_id"],
            }
        )

    summary = {
        "scenario_count": len(scenarios),
        "candidate_count": total,
        "forbidden_tag_violations": violations,
        "forbidden_tag_violation_rate": round(violations / total, 4) if total else 0,
        "scenario_hit_rate": round(expected_hits / expected_total, 4) if expected_total else 0,
        "note": "这是本地规则模式的回归结果，不是用户测试结果，也不是 Dify 运行结果。",
    }
    output = {"summary": summary, "runs": results}
    path = ROOT / "eval" / "latest_results.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
