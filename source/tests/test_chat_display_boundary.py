"""User-visible wire content, with a synthetic Job and isolated Chainlit transport."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from product_workflow import chainlit_ui as ui
from product_workflow.presentation import recorded_chat_reply
from tests.test_product_chainlit_mesh_upload import _UiProbe, _bound_job
from tests.test_product_preview_integration import candidate_job

MARK = "SYNTHETIC_INTERNAL_MARK"
PRIVATE = {"runner": {"cwd": "Z:/private/" + MARK, "stderr": MARK},
           "debug_json": json.dumps({"credential_fixture": MARK})}


def wire(probe):
    return json.dumps([probe.messages, probe.elements], default=str)


class DisplayBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_initial_and_refresh_send_only_public_fields(self):
        job = dict(candidate_job(), status="VIEWER_READY", attempts=[], reproduction=PRIVATE)
        before = copy.deepcopy(job)
        with _UiProbe() as probe, patch.object(ui, "_start_preview_task"):
            for _ in range(2):
                await ui._show_readonly_result(job)
                self.assertNotIn(MARK, wire(probe))
                self.assertIn("Checking recorded result files", wire(probe))
        self.assertEqual(job, before)

    async def test_current_job_explanation(self):
        job = dict(candidate_job(), formal_bundle_binding=PRIVATE)
        job["target"]["physical_9d_gpa"] = {"C11": 7.5}
        store = SimpleNamespace(get_consistent=lambda *_: job)
        with _UiProbe() as probe, patch.object(ui, "_job_store", return_value=store), patch.object(
                ui, "build_readonly_product_job_projection", return_value={"internal": PRIVATE}):
            await ui._answer_current_job()
            self.assertIn("7.5", wire(probe))
            self.assertNotIn(MARK, wire(probe))

    async def test_candidates_keep_metrics_rank_eligibility_and_target(self):
        job = candidate_job()
        c = job["candidate_set"]["candidates"][1]
        c.update(nmse=.012, relative_density=.23, predicted_stiffness={"C11": 8.125},
                 solver=PRIVATE)
        job["target"].update(physical_9d_gpa={"C11": 8}, extra=PRIVATE)
        before = copy.deepcopy(job)
        with _UiProbe() as probe, patch.object(ui, "_start_preview_task"):
            await ui._show_candidates(job)
            model = ui._job_ui_state(job["job_id"])["cards"]["candidates"]["model"]
            cell = model["candidates"][0]
            self.assertEqual((cell["id"], cell["rank"], cell["eligible"], cell["nmse"], cell["density"]),
                             ("candidate-1", 1, True, "0.012", "0.23"))
            self.assertEqual(model["comparison"]["unit"], "GPa")
            self.assertEqual(model["comparison"]["rows"][0]["predictions"][0], "8.125")
            self.assertNotIn(MARK, wire(probe))
        self.assertEqual(job, before)

    async def test_confirmation_reference_only_in_action(self):
        result = dict(action="select_candidate", candidate_id="candidate-1",
                      confirmation_ref="test-action-reference", proposal_ref=MARK, extra=PRIVATE)
        before = copy.deepcopy(result)
        with _UiProbe() as probe:
            await ui._present_chat_confirmation(result)
            self.assertEqual(next(iter(probe.actions.values()))["payload"], {"token": "test-action-reference"})
            self.assertIn("candidate-1", wire(probe))
            self.assertNotIn("test-action-reference", wire(probe))
            self.assertNotIn(MARK, wire(probe))
        self.assertEqual(result, before)

    async def test_mesh_error_all_three_channels(self):
        job = _bound_job()
        with _UiProbe(), patch.object(ui, "_close_mesh_entry", new=AsyncMock()), patch.object(
                ui, "build_product_job_projection", return_value={"view_state": "mesh"}), patch.object(
                ui, "normalize_job_mesh_binding", side_effect=OSError(str(PRIVATE))), patch.object(
                ui, "_send_job_stage_card", new_callable=AsyncMock) as send:
            await ui.render_product_job(job)
            args, kwargs = send.await_args
            for view in (args[2], kwargs["evidence"], kwargs["model"]["message"]):
                self.assertNotIn(MARK, str(view))
                self.assertIn("mesh", str(view).lower())
                self.assertIn("reopen", str(view).lower())

    async def test_side_text_rejects_unprojected_input_on_fallback(self):
        with _UiProbe() as probe, patch.object(ui, "_job_ui_state", side_effect=RuntimeError):
            for raw in (PRIVATE, json.dumps(PRIVATE)):
                await ui._send_job_stage_card("fixture", "conformal", "Check status", evidence=raw)
                self.assertNotIn(MARK, wire(probe))

    async def test_displayed_action_protocol_remains_single_use_and_job_bound(self):
        from product_workflow.chat_tools import ChatTools, ChatToolError
        from tests.test_product_chat_tools import SyntheticQuery
        for scenario in ("valid", "expired", "old_job", "source_changed"):
            with self.subTest(scenario=scenario), _UiProbe() as probe:
                now, current, query = [1.0], [None], SyntheticQuery()
                tools = ChatTools(query_service=lambda: query, read_job=lambda _: None, read_result=lambda _: None,
                                  current_job_id=lambda: current[0], session_id="synthetic", clock=lambda: now[0])
                records = await tools.execute("query_records", {"limit": 1})
                proposal = await tools.execute("prepare_design_target", {"result_ref": records["result_ref"], "position": 1})
                before = copy.deepcopy(tools.pending)
                await ui._present_chat_confirmation(proposal)
                self.assertEqual(tools.pending, before)
                ref = next(iter(probe.actions.values()))["payload"]["token"]
                self.assertNotIn(ref, wire(probe))
                if scenario == "valid":
                    self.assertEqual((await tools.consume_action(ref))["target"]["source"]["sample_id"], 17)
                elif scenario == "expired":
                    now[0] += 901
                elif scenario == "old_job":
                    current[0] = "job-" + "B" * 24
                else:
                    query.source_sha256 = "F" * 64
                with self.assertRaises(ChatToolError):
                    await tools.consume_action(ref)

    def test_verified_result_explanation_keeps_counts_only(self):
        from product_workflow.presentation import build_job_fact_reply
        job = dict(job_id="job-" + "A" * 24, status="VIEWER_READY", revision=5, candidates=[
            dict(candidate_id="cell", rank=1, eligible=True)], candidate_confirmation="CONFIRMED",
            selection={"selected_candidate_id": "cell", "selected_rank": 1})
        result = dict(status="verified", job_id=job["job_id"], revision=5, selected_candidate_id="cell",
                      files={"b31": {}, MARK: {}}, summary={"counts": {"nodes": 24, "extra": PRIVATE},
                      "warnings": [json.dumps(PRIVATE)], "statistics": {"stdout": MARK}})
        before = copy.deepcopy(result)
        canonical = build_job_fact_reply(job, focus="result", design_result=result)
        self.assertIn(MARK, canonical)
        text = recorded_chat_reply(dict(status="answered", answer_origin="program_job_facts",
            text=canonical, fact_request={"focus": "result"}, design_result=result), {"job": job})
        self.assertNotIn(MARK, text)
        self.assertIn("24", text)
        self.assertIn("b31", text)
        self.assertIn("warnings: 1", text)
        self.assertEqual(result, before)

    async def test_csv_picker_projects_rows_on_create_and_update(self):
        values = {name: 2.25 for name in ui.STIFFNESS_NAMES}
        values["unknown"] = PRIVATE
        parsed = dict(filename=r"Z:/private/target.csv", record_count=2, valid_count=1, invalid_count=1,
                      rows=[dict(record_index=1, values=values, error=None),
                            dict(record_index=2, values=None, error=json.dumps(PRIVATE))])
        before = copy.deepcopy(parsed)
        with _UiProbe() as probe:
            state = await ui._begin_csv_import()
            state["parsed"] = parsed
            for _ in range(2):
                await ui._show_csv_picker(state)
                props = state["component"].props
                self.assertNotIn(MARK, wire(probe))
                self.assertEqual(props["filename"], "target.csv")
                self.assertEqual(set(props["rows"][0]["values"]), set(ui.STIFFNESS_NAMES))
                self.assertEqual(props["rows"][0]["values"]["C11"], 2.25)
                self.assertTrue(props["rows"][1]["error"])
        self.assertEqual(parsed, before)

    async def test_database_detail_retains_exact_values_without_row_or_provenance_extras(self):
        from tests.test_product_chat_tools import SyntheticQuery
        query = SyntheticQuery()
        response = query.execute_request(dict(schema_version=ui.DATABASE_REQUEST_SCHEMA, kind="records",
                                               filters=[], order_by=[], limit=1))
        response["data"]["rows"][0].update(C11=.123456789123, debug=PRIVATE)
        response["provenance"]["command"] = json.dumps(PRIVATE)
        response["result_ref"] = "synthetic-result-ref"
        before = copy.deepcopy(response)
        with _UiProbe() as probe:
            await ui._show_database_result(response, question="reference search")
            self.assertIn("0.123456789123", wire(probe))
            self.assertNotIn(MARK, wire(probe))
            self.assertNotIn("synthetic-result-ref", wire(probe))
            self.assertTrue(probe.actions)
        self.assertEqual(response, before)

    def test_quality_and_range_keep_measurements_without_extra_diagnostics(self):
        from tests.test_product_ui_feedback import FeedbackTests
        target = FeedbackTests.range_target()
        target["range_check"].update(extra=PRIVATE, criterion=json.dumps(PRIVATE))
        target["range_check"]["fields"][0]["extra"] = PRIVATE
        with _UiProbe():
            text = ui._target_validation_evidence(target)
            self.assertIn("0.11651468508929674", text)
            self.assertNotIn(MARK, text)
            quality = ui._quality_details(dict(status="passed", geometry_quality={
                "minimum_scaled_jacobian": .7932, "stderr": MARK}, raw=PRIVATE))
            self.assertIn("0.7932", quality)
            self.assertIn("passed", quality)
            self.assertNotIn(MARK, quality)

    def test_bound_mesh_model_contains_counts_only(self):
        domain = {"excluded_types": {"C3D4": {"count": 12, "element_ids": [3, 7], "debug": PRIVATE}},
                  "selected_element_count": 4, "excluded_element_count": 12}
        mesh = {"original_filename": "Z:/private/mesh.inp", "precheck_status": "passed",
                "design_domain": domain, "node_count": 32, "element_count": 4}
        before = copy.deepcopy(mesh)
        view = ui._mesh_model(mesh)
        self.assertEqual(view["fileName"], "mesh.inp")
        self.assertEqual(view["excludedTypes"], {"C3D4": 12})
        self.assertEqual(view["quality"], "passed")
        self.assertNotIn(MARK, json.dumps(view))
        self.assertEqual(mesh, before)

    def test_free_form_reply_and_clarification_keep_science_without_raw_diagnostics(self):
        for status in ("answered", "needs_clarification"):
            args = {"status": status, "answer_basis": "conversation", "clarification_origin": "model"}
            self.assertEqual(recorded_chat_reply(dict(args, text="Compare C11 in GPa?"), {}), "Compare C11 in GPa?")
            for raw in ("File Z:/private/" + MARK, json.dumps(PRIVATE), "confirmation_ref=" + MARK,
                        "4a" * 24, "api_key=" + MARK):
                self.assertNotIn(raw, recorded_chat_reply(dict(args, text=raw), {}))

    def test_error_fields_cannot_override_mapped_prose(self):
        for stage in ("result_read", "action_prepare"):
            value = recorded_chat_reply(dict(status="error", failure=dict(stage=stage, code="FILE_MISSING",
                message=MARK, next_step=MARK), job_status="VIEWER_READY"), {})
            self.assertNotIn(MARK, value)

