import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cost_model import ACTION_STEPS, recommend, recommend_consolidation, score  # noqa: E402


class CostModelTests(unittest.TestCase):
    def test_keeps_well_used_fast_materialization(self):
        row = {
            "MATERIALIZATION_RUNS": 20,
            "MATERIALIZATION_CREDITS": 1,
            "MATCHED_MATERIALIZED_READS": 200,
            "MATCHED_READ_P95_SEC": 1.2,
            "P95_SEC": 1.2,
        }
        self.assertEqual(recommend(row)["ACTION"], "Keep materialization")

    def test_retunes_low_utilization_instead_of_removing(self):
        row = {
            "MATERIALIZATION_RUNS": 100,
            "MATERIALIZATION_CREDITS": 5,
            "MATCHED_MATERIALIZED_READS": 2,
            "P95_SEC": 10,
        }
        result = recommend(row)
        self.assertEqual(result["ACTION"], "Retune materialization")
        self.assertNotIn("Remove", result["ACTION"])

    def test_controls_force_investigation(self):
        row = {
            "MATERIALIZATION_RUNS": 30,
            "MATCHED_MATERIALIZED_READS": 100,
            "HAS_CONTROLS": True,
            "CONTROL_TARGETS_RESOLVED": False,
        }
        self.assertEqual(recommend(row)["ACTION"], "Investigate materialization")

    def test_removal_requires_measured_counterfactual(self):
        base = {
            "MATERIALIZATION_RUNS": 30,
            "MATERIALIZATION_CREDITS": 8,
            "MATCHED_MATERIALIZED_READS": 1,
            "P95_SEC": 1,
        }
        self.assertNotIn("Remove", recommend(base)["ACTION"])
        measured = {
            **base,
            "COUNTERFACTUAL_P95_SEC": 3,
            "COUNTERFACTUAL_CREDITS": .25,
        }
        self.assertEqual(
            recommend(measured, {"min_monthly_savings": 10})["ACTION"],
            "Remove materialization candidate",
        )
        self.assertEqual(
            recommend({
                **measured,
                "HAS_CONTROLS": True,
                "CONTROL_TARGETS_RESOLVED": False,
            })["ACTION"],
            "Investigate materialization",
        )
        self.assertEqual(
            recommend({**measured, "LINEAGE_COMPLETE": False})["ACTION"],
            "Investigate materialization",
        )

    def test_optimizes_intrinsically_expensive_query(self):
        row = {
            "RUNS": 5, "CREDITS": 1, "P95_SEC": 20,
            "MAX_BYTES": 5_000_000_000,
        }
        self.assertEqual(recommend(row)["ACTION"], "Optimize query/model")

    def test_adds_only_when_repeated_and_nontrivial(self):
        row = {"RUNS": 100, "CREDITS": 3, "P95_SEC": 4}
        self.assertEqual(recommend(row)["ACTION"], "Add materialization candidate")
        self.assertEqual(
            recommend(row, {"freshness": "real-time"})["ACTION"], "Monitor"
        )

    def test_qas_credits_are_included_in_thresholds(self):
        row = {"RUNS": 20, "CREDITS": 0, "QAS_CREDITS": 1, "P95_SEC": 1}
        self.assertEqual(recommend(row)["ACTION"], "Add materialization candidate")

    def test_data_model_materialization_requires_resolved_evidence(self):
        row = {
            "OBJECT": "/data-model/example",
            "RUNS": 100,
            "CREDITS": 5,
            "P95_SEC": 4,
        }
        self.assertEqual(recommend(row)["ACTION"], "Investigate materialization")

    def test_missing_read_evidence_never_retunes_or_removes(self):
        row = {
            "HAS_EXISTING_SCHEDULE": True, "READ_EVIDENCE_AVAILABLE": False,
            "MATERIALIZATION_RUNS": 100, "MATERIALIZATION_CREDITS": 5,
            "MATCHED_MATERIALIZED_READS": 0, "REFRESHES_PER_DAY": 24,
        }
        result = recommend(row)
        self.assertEqual(result["ACTION"], "Investigate materialization")
        self.assertIn("cannot be measured", result["WHY"])

    def test_sigma_only_schedule_is_investigated(self):
        row = {"HAS_EXISTING_SCHEDULE": True, "READ_EVIDENCE_AVAILABLE": False}
        self.assertEqual(recommend(row)["ACTION"], "Investigate materialization")

    def test_every_result_has_impact_and_steps(self):
        rows = [
            {"MATERIALIZATION_RUNS": 20, "MATERIALIZATION_CREDITS": 1,
             "MATCHED_MATERIALIZED_READS": 200, "P95_SEC": 1.2},
            {"RUNS": 100, "CREDITS": 3, "P95_SEC": 4},
            {"RUNS": 1, "CREDITS": 0, "P95_SEC": 1},
            {"RUNS": 5, "CREDITS": 1, "P95_SEC": 20, "MAX_BYTES": 5_000_000_000},
        ]
        for row in rows:
            result = recommend(row)
            self.assertIn(result["IMPACT"], {"High", "Medium", "Low"})
            self.assertTrue(2 <= len(result["FIX_STEPS"]) <= 4)
        for action, steps in ACTION_STEPS.items():
            self.assertTrue(2 <= len(steps) <= 4, action)

    def test_impact_tiers(self):
        big = recommend({"RUNS": 100, "CREDITS": 30, "P95_SEC": 4})
        self.assertEqual(big["IMPACT"], "Medium")
        self.assertEqual(
            recommend({"RUNS": 100, "CREDITS": 30, "P95_SEC": 4},
                      {"impact_medium_monthly": 1000})["IMPACT"], "Low")
        slow = recommend({"RUNS": 5, "CREDITS": 1, "P95_SEC": 20,
                          "MAX_BYTES": 5_000_000_000})
        self.assertEqual(slow["IMPACT"], "High")
        keep = recommend({"MATERIALIZATION_RUNS": 20, "MATERIALIZATION_CREDITS": 1,
                          "MATCHED_MATERIALIZED_READS": 200, "P95_SEC": 1.2})
        self.assertEqual(keep["IMPACT"], "Low")

    def _group(self, match, credits, n=2, **extra):
        members = [{"OBJECT": f"/workbook/wb{i}", "ELEMENT": "master",
                    "CREDITS": credits, "REFRESHES_PER_DAY": 24} for i in range(n)]
        return {"MATCH": match, "MEMBERS": members, "MEMBER_COUNT": n, **extra}

    def test_identical_build_consolidation_savings(self):
        result = recommend_consolidation(self._group("identical-build", 10), days=30,
                                         credit_price=3.0)
        self.assertEqual(result["ACTION"], "Consolidate into data model")
        self.assertEqual(result["CONFIDENCE"], "medium")
        self.assertEqual(result["SAVINGS_HIGH"], 30.0)
        self.assertEqual(result["SAVINGS_EXPECTED"], 24.0)
        self.assertEqual(result["SAVINGS_LOW"], 15.0)
        self.assertEqual(result["IMPACT"], "Medium")

    def test_same_base_tables_is_low_confidence_and_lower_savings(self):
        result = recommend_consolidation(self._group("same-base-tables", 10))
        self.assertEqual(result["CONFIDENCE"], "low")
        self.assertEqual(result["SAVINGS_LOW"], 0.0)
        self.assertEqual(result["SAVINGS_EXPECTED"], 9.0)
        self.assertEqual(result["SAVINGS_HIGH"], 18.0)

    def test_live_query_and_no_cost_groups(self):
        live = recommend_consolidation(self._group("identical-live-query", 10))
        self.assertEqual(live["SAVINGS_EXPECTED"], 9.0)
        self.assertEqual(live["SAVINGS_HIGH"], 15.0)
        none = recommend_consolidation(self._group("identical-sql", 0))
        self.assertEqual(none["SAVINGS_EXPECTED"], 0.0)
        self.assertIn("No cost data", none["WHY"])
        self.assertEqual(none["CONFIDENCE"], "low")

    def test_three_members_mention_warehouse_table(self):
        self.assertIn("dynamic table",
                      recommend_consolidation(self._group("identical-build", 1, n=3))["WHY"])
        self.assertNotIn("dynamic table",
                         recommend_consolidation(self._group("identical-build", 1, n=2))["WHY"])

    def test_logic_matches_and_data_model_source(self):
        g = self._group("identical-logic", 10, SOURCE_REF="data-model:dm1/el1")
        result = recommend_consolidation(g)
        self.assertEqual(result["CONFIDENCE"], "medium")
        self.assertEqual(result["SAVINGS_EXPECTED"], 24.0)
        self.assertIn("data-model element dm1/el1", result["WHY"])
        self.assertIn("data-model", " ".join(result["FIX_STEPS"]))
        same = recommend_consolidation(self._group("same-source", 10))
        self.assertEqual(same["CONFIDENCE"], "low")
        self.assertEqual(same["SAVINGS_EXPECTED"], 9.0)

    def test_consolidate_outranks_investigate(self):
        group = {**self._group("identical-build", 1), "ACTION": "Consolidate into data model"}
        row = {"ACTION": "Investigate materialization", "CREDITS": 0}
        self.assertGreater(score(group), score(row))


if __name__ == "__main__":
    unittest.main()
