from django.test import SimpleTestCase

from . import analysis


class ActionsEndpointTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.result = analysis.analyze()
        cls.actions = cls.result["actions"]
        cls.ids = [a["vehicle_id"] for a in cls.actions]

    def test_endpoint_ok(self):
        r = self.client.get("/api/actions")
        self.assertEqual(r.status_code, 200)
        self.assertIn("actions", r.json())

    def test_top3_after_backfill(self):
        self.assertEqual(self.ids[:3], ["V-137", "V-108", "V-151"])
        self.assertEqual([a["priority"] for a in self.actions[:2]], ["Critical", "Critical"])

    def test_backfill_merged_and_flagged_late(self):
        d = analysis.load()
        self.assertEqual(d["alert_sources"].get("hamilton_backfill.csv"), 10)
        v108 = next(a for a in self.actions if a["vehicle_id"] == "V-108")
        self.assertTrue(v108["has_late_evidence"])
        p0524 = next(e for e in v108["evidence"] if e["source"] == "hamilton_backfill.csv:A-707191")
        self.assertEqual(p0524["event_time_utc"], "2026-09-21T19:41:00Z")
        self.assertEqual(p0524["received_time_utc"], "2026-09-21T22:47:00Z".replace("00Z", "15Z"))

    def test_action_vehicle_never_listed_as_excluded(self):
        excluded_ids = {e["vehicle_id"] for e in self.result["excluded"]}
        self.assertFalse(excluded_ids & set(self.ids))

    def test_capped_at_daily_capacity(self):
        self.assertLessEqual(len(self.actions), analysis.DAILY_CAPACITY)

    def test_score_equals_breakdown_sum(self):
        for a in self.actions:
            self.assertEqual(a["score"], sum(b["points"] for b in a["score_breakdown"]), a["vehicle_id"])

    def test_parked_vehicle_not_an_action(self):
        self.assertNotIn("V-102", self.ids)
        self.assertTrue(any(e["vehicle_id"] == "V-102" and "Parked" in e["reason"] for e in self.result["excluded"]))

    def test_duplicates_removed(self):
        d = analysis.load()
        self.assertEqual(len(d["duplicates"]), 134)
        self.assertEqual(len(d["alerts"]), d["raw_count"] - 134)

    def test_reference_urgency_not_vendor_severity(self):
        # P0524 is vendor MEDIUM but reference Critical -> V-137 must be Critical.
        v137 = self.actions[0]
        self.assertIn("vendor severity said MEDIUM", v137["reason"])
        self.assertTrue(any(b["rule"] == "repair_did_not_hold" for b in v137["score_breakdown"]))

    def test_resolved_fault_excluded_with_wo_evidence(self):
        resolved = [e for e in self.result["excluded"] if e["vehicle_id"] == "V-149" and "P0300" in e["item"]]
        self.assertTrue(resolved)
        self.assertTrue(any(ev["source"] == "work_orders.csv:WO-5534" for ev in resolved[0]["evidence"]))

    def test_booking_reduces_but_does_not_remove(self):
        v158 = next(a for a in self.actions if a["vehicle_id"] == "V-158")
        self.assertTrue(any(b["rule"] == "booked_soon" and b["points"] < 0 for b in v158["score_breakdown"]))
        self.assertEqual(v158["priority"], "High")

    def test_every_evidence_item_cites_a_source(self):
        for a in self.actions:
            for ev in a["evidence"]:
                self.assertRegex(ev["source"], r"^(alerts|hamilton_backfill|work_orders|dtc_reference|vehicles)\.csv")


from unittest import mock

from . import brief


class ShiftBriefTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.facts = brief.top3_facts(analysis.analyze())

    def test_valid_brief_passes(self):
        text = ("V-137 and V-108 are Critical tonight; V-151 is High. V-137 threw P0524 at 96 kPa after WO-5540 cleared it. "
                "Do not dispatch V-137 or V-108, and inspect V-151 before next dispatch.")
        self.assertEqual(brief.validate(text, self.facts), [])

    def test_rejects_unknown_vehicle_number_and_priority(self):
        errs = brief.validate("V-999 is Critical. V-151 is Critical with 75 kPa.", self.facts)
        joined = " ".join(errs)
        self.assertIn("V-999", joined)
        self.assertIn("75", joined)
        self.assertIn("priority 'Critical'", joined)

    def test_fallback_when_ai_unavailable(self):
        with mock.patch.object(brief.ai, "_raw_completion", side_effect=RuntimeError("no key")):
            out = brief.build_brief()
        self.assertEqual(out["source"], "fallback")
        self.assertIn("V-137", out["brief"])

    def test_fallback_when_ai_hallucinates(self):
        with mock.patch.object(brief.ai, "_raw_completion", return_value='{"brief": "V-150 is Critical."}'):
            out = brief.build_brief()
        self.assertEqual(out["source"], "fallback")
        self.assertFalse(out["validation"]["passed"])

    def test_ai_brief_used_when_valid(self):
        good = '{"brief": "V-137 and V-108 are Critical tonight. Do not dispatch either; inspect V-151 before next dispatch."}'
        with mock.patch.object(brief.ai, "_raw_completion", return_value=good):
            out = brief.build_brief()
        self.assertEqual(out["source"], "ai")

    def test_brief_endpoint_ok_and_actions_unchanged(self):
        self.assertEqual(self.client.get("/api/brief").status_code, 200)
        ids = [a["vehicle_id"] for a in self.client.get("/api/actions").json()["actions"][:3]]
        self.assertEqual(ids, ["V-137", "V-108", "V-151"])
