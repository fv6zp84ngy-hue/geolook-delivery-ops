import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import internal_feedback_benchmark as B


CASE_ROOT = Path(os.environ.get("GEO_CONTROLLED_CASE_ROOT", "__controlled_cases_not_configured__")).expanduser()


class TestInternalFeedbackBenchmark(unittest.TestCase):
    def cases_available(self):
        return (CASE_ROOT / "product-labs-saas").is_dir() and (CASE_ROOT / "product-labs-hardware").is_dir()

    def _formal_fixture(self, tmp):
        """Build exactly 30 unique pairs while retaining normal gates."""
        aliases = [(f"case-{i}", CASE_ROOT / "product-labs-saas") for i in range(8)]
        B.prepare(aliases, tmp, persona_profiles=["founder"], persona_model="persona-8b", seed=31)
        pairs_path = Path(tmp) / "pairs.jsonl"
        results_path = Path(tmp) / "dummy_results.jsonl"
        pairs = [json.loads(line) for line in pairs_path.read_text(encoding="utf-8").splitlines()]
        # Seven complete cases (28) plus two tasks from the eighth case.
        selected = [row for row in pairs if row["case_id"] != "case-7"]
        selected.extend(row for row in pairs if row["case_id"] == "case-7" and row["task_id"] in {"T-001", "T-002"})
        self.assertEqual(len(selected), 30)
        selected_ids = {row["pair_id"] for row in selected}
        results = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines() if json.loads(line)["pair_id"] in selected_ids]
        pairs_path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in selected) + "\n", encoding="utf-8")
        results_path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in results) + "\n", encoding="utf-8")
        # Keep Ground Truth coverage at 100% for the selected 30 pairs.
        truth_path = Path(tmp) / "truth" / "case-7.json"
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        truth["tasks"] = [row for row in truth["tasks"] if row["task_id"] in {"T-001", "T-002"}]
        truth_path.write_text(json.dumps(truth, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return pairs_path, results_path

    def test_truth_adapter_is_deterministic_and_does_not_edit_source(self):
        if not self.cases_available():
            self.skipTest("controlled case repository is not available")
        case = CASE_ROOT / "product-labs-saas"
        before = (case / "tasks.json").read_bytes()
        snapshot = B.load_case(case, "saas")
        truth = B.build_truth(snapshot)
        self.assertEqual(snapshot["schema_version"], B.SCHEMA_VERSION)
        self.assertEqual(snapshot["cycle_id"], "2026-08-05-product-labs-saas")
        self.assertTrue(snapshot["runtime_baseline"]["exists"])
        self.assertTrue(snapshot["runtime_baseline"]["scope_matches_project"])
        self.assertTrue(truth["deterministic"])
        self.assertEqual(len(truth["tasks"]), 4)
        self.assertTrue(all(row["close_ready"]["value"] for row in truth["tasks"]))
        self.assertTrue(all(row["evidence_status"]["complete"] for row in truth["tasks"]))
        self.assertEqual((case / "tasks.json").read_bytes(), before)

    def test_next_action_is_fail_closed_when_runtime_has_only_text(self):
        if not self.cases_available():
            self.skipTest("controlled case repository is not available")
        truth = B.build_truth(B.load_case(CASE_ROOT / "product-labs-hardware", "hardware"))
        self.assertFalse(truth["tasks"][0]["next_action"]["scorable"])
        self.assertEqual(
            truth["tasks"][0]["next_action"]["reason"],
            "runtime_has_no_canonical_next_action_code",
        )
        self.assertTrue(truth["tasks"][0]["next_action"]["owner_role_scorable"])

    def test_prepare_analyze_publish_local_dummy_smoke(self):
        if not self.cases_available():
            self.skipTest("controlled case repository is not available")
        with tempfile.TemporaryDirectory() as tmp:
            result = B.prepare(
                [("saas", CASE_ROOT / "product-labs-saas"), ("hardware", CASE_ROOT / "product-labs-hardware")],
                tmp,
                cohort_id="cohort-smoke",
                persona_id="persona-smoke",
                persona_model="dummy-local",
                seed=11,
            )
            self.assertEqual(result["pair_count"], 8)
            summary = B.analyze(Path(tmp) / "manifest.json")
            self.assertEqual(summary["valid_pairs"], 8)
            self.assertEqual(summary["pair_coverage"], 1.0)
            self.assertTrue(summary["parity_valid"])
            self.assertTrue(summary["ground_truth_deterministic"])
            self.assertEqual(summary["ground_truth_coverage"], 1.0)
            self.assertFalse(summary["publishable"])
            self.assertFalse(summary["next_action"]["scorable"])
            publication = B.publish(Path(tmp) / "summary.json")
            self.assertFalse(publication["publishable"])
            self.assertIn("未达到正式发布门槛", publication["title"])

    def test_fixed_persona_cohort_is_paired_and_frozen(self):
        if not self.cases_available():
            self.skipTest("controlled case repository is not available")
        with tempfile.TemporaryDirectory() as tmp:
            result = B.prepare(
                [("saas", CASE_ROOT / "product-labs-saas")], tmp,
                persona_profiles=["founder", "technical_buyer", "security_reviewer"],
                persona_model="persona-8b",
                seed=23,
            )
            self.assertEqual(result["pair_count"], 12)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([p["id"] for p in manifest["runner"]["persona_cohort"]], ["founder", "technical_buyer", "security_reviewer"])
            pairs = [json.loads(line) for line in (Path(tmp) / "pairs.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row["baseline"]["stimulus"]["information_parity_hash"] == row["treatment"]["stimulus"]["information_parity_hash"] for row in pairs))
            self.assertTrue(all(row["baseline"]["stimulus"]["persona_id"] == row["treatment"]["stimulus"]["persona_id"] for row in pairs))

    def test_external_runner_protocol_records_failures(self):
        if not self.cases_available():
            self.skipTest("controlled case repository is not available")
        with tempfile.TemporaryDirectory() as tmp:
            B.prepare([("saas", CASE_ROOT / "product-labs-saas")], tmp, persona_profiles=["founder"], dummy=False)
            runner = Path(tmp) / "runner.py"
            runner.write_text("import json,sys; req=json.load(sys.stdin); print(json.dumps({'response': {'close_ready': True, 'reopen_required': False, 'evidence_complete': True, 'owner_role': 'content_owner', 'next_action': None}}))", encoding="utf-8")
            run = B.run_trials(Path(tmp) / "manifest.json", f"{sys.executable} {runner}")
            self.assertEqual(run["trial_count"], 8)
            self.assertEqual(run["error_count"], 0)
            summary = B.analyze(Path(tmp) / "manifest.json")
            self.assertEqual(summary["valid_pairs"], 4)
            self.assertTrue(summary["parity_valid"])

    def test_persona_runner_openai_compatible_protocol(self):
        request_seen = {}
        request = {
            "schema_version": B.PERSONA_REQUEST_SCHEMA_VERSION,
            "persona": {"id": "founder", "label": "Founder", "goal": "test", "constraints": []},
            "prompt": "test",
            "seed": 4,
        }
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        import persona_runner

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit):
                return json.dumps({"choices": [{"message": {"content": json.dumps({
                    "close_ready": True, "reopen_required": False,
                    "evidence_complete": True, "owner_role": "content_owner",
                    "next_action": None, "rationale": "test",
                })}}]}).encode()

        def fake_urlopen(req, timeout):
            request_seen.update(json.loads(req.data))
            return FakeResponse()

        original = persona_runner.urlopen
        persona_runner.urlopen = fake_urlopen
        try:
            response = persona_runner.call_provider(request, url="http://127.0.0.1:1/v1/chat/completions", api_key="secret", model="persona-8b", timeout=5)
        finally:
            persona_runner.urlopen = original
        self.assertTrue(response["close_ready"])
        self.assertEqual(request_seen["temperature"], 0)
        self.assertEqual(request_seen["seed"], 4)

    def test_parity_mismatch_is_not_publishable(self):
        if not self.cases_available():
            self.skipTest("controlled case repository is not available")
        with tempfile.TemporaryDirectory() as tmp:
            B.prepare([("saas", CASE_ROOT / "product-labs-saas")], tmp, seed=3)
            results = Path(tmp) / "dummy_results.jsonl"
            rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()]
            rows[0]["information_parity_hash"] = "broken"
            results.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            summary = B.analyze(Path(tmp) / "manifest.json")
            self.assertFalse(summary["parity_valid"])
            self.assertFalse(summary["publishable"])
            self.assertGreater(summary["invalid_trial_count"], 0)

    def test_formal_gate_29_valid_pairs_cannot_publish(self):
        if not self.cases_available():
            self.skipTest("controlled case repository is not available")
        with tempfile.TemporaryDirectory() as tmp:
            pairs_path, results_path = self._formal_fixture(tmp)
            rows = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()]
            drop_pair = rows[0]["pair_id"]
            rows = [row for row in rows if row["pair_id"] != drop_pair]
            results_path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
            summary = B.analyze(Path(tmp) / "manifest.json")
            self.assertEqual(summary["formal_gates"]["min_valid_pairs"], 30)
            self.assertEqual(summary["valid_pairs"], 29)
            self.assertFalse(summary["publishable"])

    def test_formal_gate_30_valid_pairs_may_publish_when_other_gates_pass(self):
        if not self.cases_available():
            self.skipTest("controlled case repository is not available")
        with tempfile.TemporaryDirectory() as tmp:
            self._formal_fixture(tmp)
            summary = B.analyze(Path(tmp) / "manifest.json")
            self.assertEqual(summary["formal_gates"]["min_valid_pairs"], 30)
            self.assertEqual(summary["valid_pairs"], 30)
            self.assertEqual(summary["pair_coverage"], 1.0)
            self.assertEqual(summary["ground_truth_coverage"], 1.0)
            self.assertTrue(summary["parity_valid"])
            self.assertTrue(summary["publishable"])

    def test_geo_cli_exposes_benchmark_commands(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "scripts" / "geo.py"), "feedback-benchmark", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("prepare", result.stdout)
        self.assertIn("analyze", result.stdout)
        self.assertIn("publish", result.stdout)


if __name__ == "__main__":
    unittest.main()
