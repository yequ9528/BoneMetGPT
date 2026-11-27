"""Offline presentation checks using fixed model responses and synthetic records."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from product_workflow import chainlit_ui as ui
from product_workflow.presentation import display_number, error_summary, job_status_text, recorded_chat_reply, stiffness_table
from product_workflow.contracts import STIFFNESS_NAMES, target_from_physical_9d
from product_workflow.runtime_manager import RuntimeExecutionManager
from tests.test_product_chainlit_mesh_upload import _UiProbe
from tests.test_product_chat_tools import SyntheticQuery
from tests.test_product_job_state import _job


class ControlledJobFactsTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            'job_id': 'job-' + 'A' * 24, 'revision': 11, 'status': 'VIEWER_READY',
            'candidate_confirmation': 'CONFIRMED',
            'selection': {'selected_candidate_id': 'candidate-2', 'selected_rank': 2},
            'comparison_facts': {'p10_recommended_candidate_id': 'candidate-1'},
            'target': {'physical_9d_gpa': {'C11': 20.}},
            'allowed_actions': [],
            'candidates': [
                {'candidate_id': 'candidate-1', 'rank': 1, 'eligible': True, 'score': .006916,
                 'relative_density': .3678985, 'nmse': .000073898, 'predicted_stiffness': {'C11': 20.1}},
                {'candidate_id': 'candidate-2', 'rank': 2, 'eligible': True, 'score': .007084,
                 'relative_density': .36667988, 'nmse': .000076894, 'predicted_stiffness': {'C11': 19.9}},
                {'candidate_id': 'candidate-3', 'rank': 3, 'eligible': True, 'score': .0071,
                 'relative_density': .366973, 'nmse': .000077, 'predicted_stiffness': {'C11': 19.8}},
            ],
        }

    def reply(self, **kwargs):
        from product_workflow.presentation import build_job_fact_reply
        before = copy.deepcopy(self.job)
        text = build_job_fact_reply(self.job, **kwargs)
        self.assertEqual(self.job, before)
        return text

    def test_confirmed_choice_has_lower_density_and_higher_errors_without_invented_motivation(self):
        text = self.reply(focus='selection')
        self.assertIn('Confirmed selection: Rank 2', text)
        self.assertIn('relative density is lower', text)
        self.assertIn('P-10 score is higher', text)
        self.assertIn('NMSE is higher', text)
        self.assertIn('NMSE does not determine the recommendation', text)
        self.assertNotIn('because of your preference', text)
        self.assertNotIn('higher relative density', text)

    def test_selection_comparison_also_explains_negative_stiffness_differences(self):
        text = self.reply(focus='selection')
        self.assertIn('Confirmed selection: Rank 2', text)
        self.assertIn('prediction minus target in GPa', text)
        self.assertIn('does not mean negative stiffness', text)

    def test_csv_minima_are_separate_from_the_p10_recommendation(self):
        for candidate, density, nmse in zip(self.job['candidates'], [.043512, .043571, .042998], [.003842, .003822, .003830]):
            candidate.update(relative_density=density, nmse=nmse)
        text = self.reply()
        self.assertIn('Lowest recorded relative density among all eligible candidates: Rank 3', text)
        self.assertIn('Lowest recorded NMSE among all eligible candidates: Rank 2', text)
        self.assertIn('P-10 score is higher', text)
        self.assertIn('NMSE is lower', text)
        self.assertIn('Global P-10 recommendation: Rank 1', text)

    def test_explicit_scope_does_not_substitute_the_global_density_minimum(self):
        text = self.reply(ranks=[3, 1], focus='density')
        self.assertIn('Lowest recorded relative density among the specified eligible candidates: Rank 3', text)
        self.assertNotIn('Rank 2', text.split('Confirmed selection:')[0])
        for kwargs in ({'ranks': [4]}, {'ranks': [1, 1]}, {'ranks': [1], 'candidate_ids': ['candidate-1']},
                       {'candidate_ids': ['absent']}, {'ranks': []}, {'focus': 'selection', 'ranks': [2]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.reply(**kwargs)

    def test_subset_baseline_is_not_relabelled_as_global_recommendation(self):
        text = self.reply(candidate_ids=['candidate-3', 'candidate-2'])
        self.assertIn('Global P-10 recommendation: Rank 1', text)
        self.assertIn('Comparison baseline within the specified candidates: Rank 2', text)
        self.assertIn('This baseline does not change the global recommendation', text)

    def test_missing_tied_and_excluded_values_remain_explicit(self):
        self.job['candidate_confirmation'] = 'UNCONFIRMED'
        self.job['candidates'][1].update(relative_density=.3678985, nmse=None)
        self.job['candidates'][2].update(eligible=False, relative_density=.01, nmse=.000001,
                                        exclusion_reasons=['CAD_READY_REQUIRED'])
        text = self.reply()
        self.assertIn('tied', text)
        self.assertIn('Not recorded', text)
        self.assertIn('CAD_READY_REQUIRED', text)
        self.assertIn('No candidate has been confirmed', text)
        self.assertNotIn('Lowest recorded NMSE among all eligible candidates: Rank 3', text)
        for candidate in self.job['candidates']:
            candidate['eligible'] = False
        self.assertIn('No eligible candidate', self.reply())

    def test_metric_and_component_explanations_keep_prediction_boundaries(self):
        self.job['candidates'][0]['nmse'] = .0025
        metrics = self.reply(focus='metrics', ranks=[1])
        self.assertIn('sum(target^2)', metrics)
        self.assertIn('NMSE (%) = 100', metrics)
        self.assertIn('relative L2 error 5%', metrics)
        delta = self.reply(focus='component_delta', component='C11', ranks=[2])
        self.assertIn('prediction minus target', delta)
        self.assertIn('-0.1', delta)
        self.assertIn('does not mean negative stiffness', delta)
        self.assertIn('GPa', delta)
        component_values, comparison = delta.split('Global P-10 recommendation:', 1)
        self.assertNotIn('candidate-1', component_values)
        self.assertIn('candidate-1', comparison)

    def test_read_result_explains_recorded_state_without_inventing_selection_or_values(self):
        self.job['candidate_confirmation'] = 'UNCONFIRMED'
        self.assertIn('No candidate has been confirmed', self.reply(focus='read_result'))
        self.assertIn('Rank 1', self.reply(focus='read_result'))
        self.job['candidate_confirmation'] = 'CONFIRMED'
        self.job['candidates'][1]['nmse'] = None
        self.assertIn('NMSE comparison: Not recorded', self.reply(focus='read_result'))
        self.job.update(candidates=[], candidate_confirmation='UNCONFIRMED', status='TARGET_PROPOSED')
        text = self.reply(focus='read_result')
        self.assertIn('No candidate has been confirmed', text)
        self.assertNotIn('prediction minus target', text)

    def test_result_requires_actual_matching_verified_read_and_records_available_summary(self):
        result = {'status': 'verified', 'job_id': self.job['job_id'], 'revision': 11,
                  'selected_candidate_id': 'candidate-2', 'files': {'b31': {'size_bytes': 10}},
                  'summary': {'counts': {'nodes': 8}, 'warnings': ['fixture warning']},
                  'viewer_display_verified': False}
        text = self.reply(focus='result', design_result=result)
        self.assertIn('result package was verified', text)
        self.assertIn('b31', text)
        self.assertIn('nodes', text)
        self.assertIn('fixture warning', text)
        self.assertIn('Viewer display has not been verified', text)
        for bad in (None, dict(result, status='error'), dict(result, revision=10),
                    dict(result, job_id='different'), dict(result, selected_candidate_id='candidate-1')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.reply(focus='result', design_result=bad)

    def test_receipt_is_short_and_never_upgrades_status_to_a_verified_package(self):
        self.assertEqual(self.reply(receipt={'action': 'select_candidate'}),
                         'Selected Rank 2 · `candidate-2`.')
        text = self.reply(receipt={'action': 'start_conformal'})
        self.assertNotIn('package was verified', text)
        self.assertNotIn('recommending', text)

    def test_program_marker_preserves_body_and_scopes_only_comparison_attachments(self):
        body = self.reply(focus='density', ranks=[3, 1])
        result = {'status': 'answered', 'answer_basis': 'workflow', 'workflow_intent': 'compare_candidates',
                  'answer_origin': 'program_job_facts', 'text': body,
                  'fact_request': {'focus': 'density', 'ranks': [3, 1]}}
        text = recorded_chat_reply(result, {'job': self.job})
        self.assertTrue(text.startswith(body))
        self.assertNotIn('| 2 |', text)
        self.assertIn('| 1 |', text)
        result['fact_request'] = {'focus': 'selection'}
        self.assertEqual(recorded_chat_reply(result, {'job': self.job}), body)


class PresentationTests(unittest.IsolatedAsyncioTestCase):
    def test_diagnostic_details_keep_safe_evidence_while_hiding_private_paths(self):
        error = {"code": "P09", "stage": "TARGET_CONFIRMED", "message": "P09 rejected",
                 "details": {"scaler_path": "I:\\private\\scaler_Y.pt"}}
        before = copy.deepcopy(error)
        detail = json.dumps(ui._product_error_evidence(error), ensure_ascii=False)
        self.assertIn("Target P-09 validation did not pass", detail)
        self.assertNotIn("P09 rejected", detail)
        self.assertIn("TARGET_CONFIRMED", detail)
        self.assertNotIn("I:\\\\private", detail)
        self.assertEqual(error, before)

    async def test_failed_and_interrupted_job_cards_match_records_and_offer_existing_refresh(self):
        for status, stage, expected in (("FAILED", "TARGET_CONFIRMED", "was not started"),
                                        ("FAILED", "INVERSE_RUNNING", "computation failed"),
                                        ("HOLD_RUNTIME_INTERRUPTED", "CONFORMAL_RUNNING", "interrupted")):
            with self.subTest(status=status, stage=stage):
                job = _job(status)
                job.update(workflow_disposition="HOLD", failure={"stage": stage, "message": "fixed retained evidence"})
                if status == "HOLD_RUNTIME_INTERRUPTED":
                    job["mesh_binding"] = {"mesh_binding_sha256": "A" * 64}
                    job["attempts"] = [{"attempt_id": f"{job['job_id']}-attempt-0001", "status": status}]
                    job["failure"]["code"] = "RUNTIME_PROCESS_INTERRUPTED"
                before = copy.deepcopy(job)
                sent = []
                async def capture(*args, **kwargs):
                    sent.append((args[2], kwargs))
                with patch.object(ui, "_send_job_stage_card", side_effect=capture):
                    await ui.render_product_job(job)
                text, extras = sent[-1]
                self.assertIn(expected, text)
                self.assertTrue(any(a.name == "product_refresh_job" for a in extras["actions"]))
                self.assertNotIn("product_start_conformal", [a.name for a in extras["actions"]])
                self.assertTrue(extras["terminal"])
                self.assertEqual(job, before)

    def test_failed_validation_is_distinct_from_failed_computation(self):
        self.assertIn("was not started", job_status_text("FAILED", {"stage": "TARGET_CONFIRMED"}))
        self.assertNotIn("computation failed", job_status_text("FAILED", {"stage": "TARGET_CONFIRMED"}))
        self.assertIn("computation failed", job_status_text("FAILED", {"stage": "INVERSE_RUNNING"}))
        self.assertIn("recorded failure", job_status_text("FAILED"))
        self.assertIn("interrupted", job_status_text("HOLD_RUNTIME_INTERRUPTED"))

    async def test_unranked_eligibility_hold_does_not_claim_every_candidate_failed_p09(self):
        job = {"job_id": "job-" + "A" * 24, "revision": 3, "target": {},
               "candidate_set": {"candidates": [
                   {"rank": None, "candidate_id": "excluded-1", "score": None,
                    "p09_validation": {"status": "PASS"}, "normalized_diameter": None,
                    "eligible": False, "exclusion_reasons": ["CAD_READY_REQUIRED"]}]}}
        sent = []
        async def capture(*args, **kwargs):
            if args[1] == "candidates":
                sent.append((args[2], kwargs))
        before = copy.deepcopy(job)
        with _UiProbe(), patch.object(ui, "_send_job_stage_card", side_effect=capture), \
                patch.object(ui, "_confirmed_hash", return_value=None):
            await ui._show_candidates(job)
        text, extras = sent[0]
        model = extras["model"]
        self.assertIn("no eligible candidate", model["message"])
        self.assertFalse(model["candidates"][0]["eligible"])
        self.assertIsNone(model["candidates"][0]["action"])
        self.assertNotIn("No candidate passed P-09", json.dumps(model))
        self.assertNotIn("Rank None", json.dumps(model))
        self.assertEqual(model["candidates"][0]["name"], "Unranked candidate 1")
        self.assertTrue(any(a.name == "product_refresh_job" for a in extras["actions"]))
        self.assertIn("CAD_READY_REQUIRED", str(extras["evidence"]))
        self.assertEqual(job, before)

    async def test_verified_result_read_is_deferred_until_final_evidence_check(self):
        result = {"status": "verified", "job_id": "job-" + "A" * 24,
                  "viewer_url": "http://127.0.0.1:5173/product.html?job_id=job-" + "A" * 24,
                  "viewer_display_verified": False, "files": {}}
        before = copy.deepcopy(result)
        with _UiProbe() as probe:
            await ui._present_chat_tool("get_design_result", result)
            text = "\n".join(m["output"] for m in probe.messages.values())
        self.assertEqual(text, '')
        self.assertEqual(result, before)

    async def test_query_identifiers_are_exact_while_measurements_use_display_precision(self):
        query = SyntheticQuery()
        query.rows[0]["sample_id"] = 1234567
        result = query.execute_request({"schema_version": "latticegpt_database_read_request_v1", "kind": "records",
                                        "filters": [{"field": "sample_id", "op": "eq", "value": 1234567}], "order_by": [], "limit": 1})
        with _UiProbe() as probe:
            await ui._show_database_result(result, question="one record")
            text = "\n".join(m["output"] for m in probe.messages.values())
        self.assertIn("1234567", text)
        self.assertNotIn("1.23457e+06", text)
        self.assertEqual(result["data"]["rows"][0]["sample_id"], 1234567)

    async def test_replacing_a_stage_removes_old_details_and_preserves_new_projected_measurement(self):
        job_id = "job-" + "A" * 24
        with _UiProbe() as probe:
            first = await ui._send_job_stage_card(job_id, "target", "first", card_revision=1,
                                                  evidence=ui._quality_details({"geometry_quality": {"minimum_scaled_jacobian": 0.123456789123}}))
            old_id = first.elements[0].id
            updated = await ui._send_job_stage_card(job_id, "target", "second", card_revision=2,
                                                    evidence=ui._quality_details({"geometry_quality": {"minimum_scaled_jacobian": 0.987654321987}}))
            self.assertIs(first, updated)
            self.assertNotIn(old_id, probe.elements)
            self.assertEqual(len(probe.elements), 1)
            detail = next(iter(probe.elements.values()))
            self.assertEqual(detail["display"], "side")
            self.assertIn("0.987654321987", detail["content"])
            self.assertNotIn("0.123456789123", detail["content"])

    async def test_failed_evidence_transport_does_not_grant_confirmation(self):
        job_id = "job-" + "A" * 24
        with _UiProbe() as probe:
            action = ui.cl.Action(name="product_confirm_target", label="Confirm target and generate 3 candidates",
                                  payload={"job_id": job_id, "expected_revision": 1})
            probe.fail_event = "element"
            with self.assertRaises(RuntimeError):
                await ui._send_job_stage_card(job_id, "target", "target ready", card_revision=1,
                                             actions=[action], evidence=ui._quality_details({"geometry_quality": {"minimum_scaled_jacobian": 1.0}}))
            self.assertNotIn(action.id, ui._job_ui_state(job_id).get("workflow_confirmations", {}))

    def test_error_summary_uses_recorded_code_and_keeps_unknown_reason_unconfirmed(self):
        error = {"code": "DATABASE_UNAVAILABLE", "message_zh": "保留原始错误", "diagnostic_id": "fixture"}
        before = copy.deepcopy(error)
        self.assertIn("could not be read", error_summary(error))
        self.assertEqual(error, before)
        self.assertEqual(error_summary({"code": "UNKNOWN"}, fallback="Check the recorded failure."), "Check the recorded failure.")
        self.assertNotIn("computation failed", error_summary(error))

    def test_fixed_chat_failures_are_english_and_do_not_claim_computation_failed(self):
        for status in ("busy", "budget_exhausted", "timeout", "limit_reached", "incomplete", "waiting_for_user", "empty", "error"):
            with self.subTest(status=status):
                result = {"status": status, "text": "原始错误仅用于诊断"}
                before = copy.deepcopy(result)
                text = recorded_chat_reply(result, {})
                self.assertNotRegex(text, r"[\u3400-\u9fff]")
                self.assertNotIn("computation failed", text.lower())
                self.assertEqual(result, before)

    async def test_workflow_followup_uses_controlled_component_explanation(self):
        job = _job("CANDIDATES_READY")
        def model(messages):
            context = json.loads(messages[-1]["content"])
            self.assertEqual(context.get('phase'), 'planning')
            return {'kind': 'plan', 'task': 'workflow',
                    'arguments': {'intent': 'compare_candidates', 'focus': 'component_delta'}}
        with _UiProbe() as probe, patch.object(ui, "_chat_model", side_effect=model), \
                patch.object(ui, "_chat_read_job", return_value=job):
            probe.session.set("product_job_id", job["job_id"])
            result = await ui._run_product_chat("What do the signed differences mean?")
            visible = "\n".join(m["output"] for m in probe.messages.values())
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result['answer_origin'], 'program_job_facts')
        self.assertTrue(visible.startswith(result['text']))
        self.assertIn('prediction minus target in GPa', result['text'])

    def test_confirmed_selection_points_to_existing_upload_without_changing_actions(self):
        snapshot = {"job": {"status": "CANDIDATES_READY", "allowed_actions": ["select_candidate", "request_mesh_upload"]}}
        before = copy.deepcopy(snapshot)
        text = recorded_chat_reply({"status": "answered", "answer_basis": "workflow"}, snapshot)
        self.assertIn('use "Upload hexahedral mesh (.inp)"', text)
        self.assertEqual(snapshot, before)

    def test_candidates_without_allowed_actions_do_not_prompt_for_confirmation(self):
        snapshot = {"job": {"status": "CANDIDATES_READY", "allowed_actions": []}}
        text = recorded_chat_reply({"status": "answered", "answer_basis": "receipt"}, snapshot)
        self.assertIn("no next-stage action", text)
        self.assertIn("Refresh Job status", text)
        self.assertNotIn("confirm a candidate", text.lower())

    def test_missing_numeric_fields_and_small_values_are_display_only(self):
        values = {"C11": 1e-12, "C12": None, "C13": float("inf"), "C22": True}
        self.assertEqual(display_number(1e-12), "1e-12")
        self.assertEqual(display_number(None), "Not recorded")
        text = stiffness_table(values)
        self.assertIn("1e-12", text)
        self.assertIn("| C12 | Not recorded |", text)
        self.assertIn("| C66 | Not recorded |", text)
        self.assertIn("GPa", text)

    async def test_query_main_card_is_compact_and_full_precision_is_preserved_in_details(self):
        query = SyntheticQuery()
        query.rows[0]["C11"] = 0.012345678912345
        result = query.execute_request({"schema_version": "latticegpt_database_read_request_v1", "kind": "records", "filters": [], "order_by": [], "limit": 1})
        before = copy.deepcopy(result)
        with _UiProbe() as probe:
            await ui._show_database_result(result, question="one record")
            visible = "\n".join(m["output"] for m in probe.messages.values())
            details = "\n".join(str(e.get("content", "")) for e in probe.elements.values())
        self.assertNotIn("source_sha256", visible)
        self.assertNotIn("0.012345678912345", visible)
        self.assertIn("0.0123457", visible)
        self.assertIn("0.012345678912345", details)
        self.assertIn("source_sha256", details)
        self.assertEqual(result, before)
        self.assertEqual(len(probe.actions), 1)

    async def test_candidates_show_recorded_gpa_predictions_without_mutation(self):
        job = {"job_id": "job-" + "A" * 24, "revision": 3, "target": {},
               "candidate_set": {"candidates": [
                   {"rank": 1, "candidate_id": "candidate-1", "score": 0.123456789,
                    "p09_validation": {"status": "PASS"}, "normalized_diameter": 0.123456789,
                    "predicted_stiffness": {"C11": 1.234567891234}, "eligible": False},
                   {"rank": 2, "candidate_id": "candidate-2", "score": None,
                    "p09_validation": {"status": "HOLD", "evaluated": False},
                    "normalized_diameter": None, "eligible": False}]}}
        before = copy.deepcopy(job)
        sent = []
        async def capture(*args, **kwargs):
            if args[1] == "candidates":
                sent.append((kwargs["model"], kwargs.get("evidence", "")))
        with _UiProbe(), patch.object(ui, "_send_job_stage_card", side_effect=capture), \
                patch.object(ui, "_confirmed_hash", return_value=None):
            await ui._show_candidates(job)
        model = sent[0][0]
        self.assertEqual(model["comparison"]["unit"], "GPa")
        self.assertEqual(model["comparison"]["rows"][0]["name"], "C11")
        self.assertEqual(model["comparison"]["rows"][0]["predictions"], ["1.23457", "Not recorded"])
        self.assertEqual(model["candidates"][0]["score"], "0.123457")
        self.assertEqual(model["candidates"][0]["diameter"], "0.123457")
        self.assertNotIn("1.234567891234", json.dumps(model))
        self.assertIn("1.234567891234", str(sent[0][1]))
        self.assertEqual(job, before)

    async def test_database_card_is_not_overridden_by_incorrect_model_units_or_claims(self):
        query = SyntheticQuery()
        invented = "C11=999 GPa; NOT_FEA_VALIDATION means FEA passed."

        def model(messages):
            context = json.loads(messages[-1]["content"])
            if context.get("phase") == "planning":
                return {"kind": "plan", "task": "records", "arguments": {"limit": 1}}
            return {"kind": "answer", "text": invented}

        before = copy.deepcopy(query.rows)
        with _UiProbe() as probe, patch.object(ui, "_query_service", return_value=query), \
                patch.object(ui, "_chat_model", side_effect=model):
            probe.session.set("product_job_id", None)
            result = await ui._run_product_chat("show one reference record")
            visible = "\n".join(m["output"] for m in probe.messages.values())
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(query.requests), 1)
        self.assertEqual(query.rows, before)
        self.assertNotIn(invented, visible)
        self.assertIn("C/Eb", visible)
        self.assertIn("0.01", visible)

    async def test_workflow_reply_cannot_replace_a_failed_record_with_success(self):
        job = _job("FAILED")
        job.update(workflow_disposition="HOLD", failure={"stage": "INVERSE_RUNNING", "message": "synthetic failure"})
        invented = "All calculations and FEA passed; enter prepare_workflow_action JSON now."

        def model(messages):
            context = json.loads(messages[-1]["content"])
            return ({"kind": "plan", "task": "workflow"} if context.get("phase") == "planning"
                    else {"kind": "answer", "text": invented})

        before = copy.deepcopy(job)
        with _UiProbe() as probe, patch.object(ui, "_chat_model", side_effect=model), \
                patch.object(ui, "_chat_read_job", return_value=job), \
                patch.object(ui, "_query_service", side_effect=AssertionError("no database read")):
            probe.session.set("product_job_id", job["job_id"])
            result = await ui._run_product_chat("explain the current task")
            visible = "\n".join(m["output"] for m in probe.messages.values())
        self.assertEqual(result["status"], "answered")
        self.assertEqual(job, before)
        self.assertNotIn(invented, visible)
        self.assertTrue("失败" in visible or "failed" in visible.lower())

    async def test_runtime_acceptance_does_not_claim_a_stage_has_started_or_passed(self):
        notices = []
        job_id = "job-" + "A" * 24
        result = {"job_id": job_id, "revision": 8, "status": "FAILED",
                  "failure": {"stage": "ADAPTER_RUNNING", "message": "synthetic failure"}}

        async def capture(content, **kwargs):
            notices.append(content)
            return True

        with _UiProbe(), patch.object(ui, "_consume_workflow_confirmation", new=AsyncMock(return_value=True)), \
                patch.object(ui, "_workflow", return_value=SimpleNamespace(start_conformal=lambda *a, **kw: result)), \
                patch.object(ui, "_RUNTIME_MANAGER", RuntimeExecutionManager()), \
                patch.object(ui, "_send_runtime_status", side_effect=capture), \
                patch.object(ui, "_continue_product_chat", new=AsyncMock()):
            await ui.start_conformal(SimpleNamespace(payload={"job_id": job_id, "expected_revision": 7}))
        self.assertTrue(notices)
        self.assertNotIn("预检 ✓", notices[0])
        self.assertNotIn("Adapter运行中", notices[0])
        self.assertNotIn("preflight passed", notices[0].lower())
        self.assertNotIn("adapter is running", notices[0].lower())
        self.assertIn("waiting for the job record", notices[0].lower())

    def test_target_display_keeps_physical_values_and_full_source_unchanged(self):
        values = {name: 1.234567891234 for name in STIFFNESS_NAMES}
        target = target_from_physical_9d(values, source_kind="csv_upload", source_sha256="A" * 64, source_name="fixture.csv")
        before = copy.deepcopy(target)
        text = ui._target_card(target, "job-" + "A" * 24)
        self.assertIn("GPa", text)
        self.assertEqual(target, before)


if __name__ == "__main__":
    unittest.main()
