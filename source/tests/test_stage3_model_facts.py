import copy
import json
import unittest

from product_workflow.chat_tools import ChatCase, ChatTools, ChatToolError, TTL_SECONDS
from product_workflow.contracts import STIFFNESS_NAMES
from product_workflow.model_context import query_facts, session_facts, tool_facts
from tests.test_product_chat_tools import SyntheticQuery


PRIVATE = r"C:\private\retain-locally\evidence.log"
SECRET = "synthetic-fixture-api-value"
REF = "dc" * 24
JOB = "job-" + "B" * 24


class ModelFactsTests(unittest.IsolatedAsyncioTestCase):
    def assert_clean(self, obj):
        serialized = json.dumps(obj, ensure_ascii=False, allow_nan=False)
        for marker in ("retain-locally", SECRET, REF):
            self.assertNotIn(marker, serialized)

    def test_saved_science_and_candidate_scopes(self):
        for metric in ("nmse", "score"):
            ref = {k: 2.0 for k in STIFFNESS_NAMES}
            pred = {k: 1.0 for k in STIFFNESS_NAMES}
            job = dict(job_id=JOB, revision=7, status="CANDIDATES_READY", job_ref=REF, job_sha256=SECRET,
                target=dict(physical_9d_gpa=ref, normalized_9d={"C11": 2/110}, confirmed=True,
                            source={"kind": "formal_150w_database", "sample_id": 92, "query_identity": SECRET},
                            range_status="PASS", p09_validation={"status": "PASS", "details": PRIVATE}),
                ranking=dict(metric=metric, lower_is_better=True, contract=PRIVATE),
                candidates=[dict(candidate_id="inverse_150w_0001", rank=2, eligible=True, origin="inverse_generated_structure",
                    score=.7, nmse=.035, predicted_stiffness=pred, stiffness_delta_gpa={k:-1.0 for k in STIFFNESS_NAMES},
                    relative_density=.33, normalized_diameter=.15, source_order=1, p09_validation={"status":"PASS", "file":PRIVATE}),
                    dict(candidate_id="candidate-B", rank=1, eligible=False, predicted_stiffness={"C11":float("nan")},
                         exclusion_reasons=["CAD_READY_REQUIRED", PRIVATE])],
                prediction_units="GPa", candidate_confirmation="UNCONFIRMED",
                comparison_facts={"recommended_candidate_id":"inverse_150w_0001", "eligible_count":1, "private":SECRET},
                selection={"selected_candidate_id":"inverse_150w_0001", "confirmation":{"ref":REF}},
                inverse_summary={"status":"success", "diagnostics":[{"diversity_fallback":True, "command":PRIVATE, "stderr":SECRET}]},
                mesh={"precheck_status":"passed", "original_upload":{"uri":PRIVATE},
                      "design_domain":{"selected_element_count":19, "excluded_solid_count":2, "glob":PRIVATE}},
                allowed_actions=["select_candidate", SECRET], non_claims=["NOT_FEA_VALIDATION", "NOT_FULL_DUAL_SPACE_FUSION", PRIVATE])
            original_keys = set(job["candidates"][0])
            view = tool_facts("get_job_context", job)
            self.assert_clean(view)
            candidate = view["candidates"][0]
            self.assertEqual((view["target"]["physical_9d_gpa"]["C11"], candidate["predicted_stiffness"]["C11"],
                              candidate["stiffness_delta_gpa"]["C11"]), (2., 1., -1.))
            self.assertEqual((view["prediction_units"], view["target"]["source"]["sample_id"]), ("GPa", 92))
            self.assertEqual((candidate["rank"], candidate["nmse"], candidate["relative_density"]), (2, .035, .33))
            self.assertEqual("score" in candidate, metric == "score")
            self.assertEqual(view["ranking"]["metric"], metric)
            self.assertEqual(view["comparison_facts"]["recommended_candidate_id"], "inverse_150w_0001")
            self.assertEqual(view["candidate_confirmation"], "UNCONFIRMED")
            self.assertEqual(view["allowed_actions"], ["select_candidate"])
            self.assertEqual(view["non_claims"], ["NOT_FEA_VALIDATION", "NOT_FULL_DUAL_SPACE_FUSION"])
            self.assertEqual(view["mesh"]["design_domain"]["selected_element_count"], 19)
            self.assertTrue(view["inverse_summary"]["diagnostics"][0]["diversity_fallback"])
            self.assertNotIn("C11", view["candidates"][1]["predicted_stiffness"])
            candidate["predicted_stiffness"]["C11"] = 99
            self.assertEqual(job["candidates"][0]["predicted_stiffness"], pred)
            self.assertEqual(pred["C11"], 1)
            self.assertEqual(set(job["candidates"][0]), original_keys)

    def test_old_absent_or_stale_job_and_independent_csv(self):
        historical = "abcde01234567890" * 2
        saved = {"candidates":[{"candidate_id":historical, "rank":1}], "selection":{"selected_candidate_id":historical}}
        old = tool_facts("get_job_context", saved)
        self.assertEqual(old["candidates"][0]["candidate_id"],historical)
        self.assertEqual(old["selection"]["selected_candidate_id"],historical)
        self.assertNotIn("p09_validation",old["candidates"][0])
        self.assertNotIn("candidate_confirmation",old)
        state = {"job":{"job_id":JOB, "status":"TARGET_PROPOSED", "revision":0, "private":PRIVATE},
                 "active_job_id":JOB, "records":None, "last_query":None,
                 "pending_csv_target":{"state":"PROPOSED", "record_index":2, "record_count":3, "physical_9d_gpa":{"C11":2.3},
                                       "filename":PRIVATE, "sha256":SECRET}, "proposal_ref":REF}
        before = copy.deepcopy(state)
        self.assertEqual(session_facts(state, phase="planning")["job"], {"job_id":JOB, "status":"TARGET_PROPOSED", "revision":0})
        now = session_facts(state, phase="execution")
        self.assert_clean(now)
        self.assertEqual(now["pending_csv_target"]["physical_9d_gpa"]["C11"], 2.3)
        self.assertEqual(state, before)
        state["active_job_id"] = "job-" + "A"*24
        self.assertIsNone(session_facts(state, phase="execution")["job"])
        self.assertEqual(session_facts(state, phase="execution")["pending_csv_target"]["record_index"], 2)

    async def test_query_count_overview_records_and_expiry_without_mutating_guards(self):
        now = [10.]
        db = SyntheticQuery()
        tools = ChatTools(query_service=lambda: db, read_job=None, read_result=None,
                          current_job_id=lambda: None, session_id="local", clock=lambda: now[0])
        args = {"conditions":[{"field":"C11", "op":"between", "value":[11,22], "unit":"GPa"}], "limit":2}
        before = copy.deepcopy(args)
        raw = await tools.execute("query_records", args)
        raw["data"]["rows"][0]["nested"] = json.dumps({"command":PRIVATE})
        raw["provenance"]["source_sha256"] = SECRET
        view = tool_facts("query_records", raw)
        self.assert_clean(view)
        self.assertEqual(args, before)
        self.assertEqual(db.requests[0]["filters"], [{"field":"C11","op":"between","value":[.1,.2]}])
        self.assertEqual(view["data"]["rows"][0]["sample_id"],17)
        self.assertEqual(view["data"]["rows"][0]["C11"],.01)
        self.assertEqual(view["units"], {"stiffness":"C/Eb","relative_density":"ratio","diameter":"normalized_ratio"})
        self.assertEqual(query_facts({**args, "private":SECRET}),before)
        self.assertEqual(query_facts({}), {})
        self.assertEqual(query_facts({"order_by":[]}), {"order_by":[]})
        self.assertEqual(tool_facts("count_records", await tools.execute("count_records", {}))["data"], {"matched_count":3})
        overview = await tools.execute("describe_database", {})
        overview["data"].update(statistics_time="2026-11-01T13:00:00Z",
                                eligibility_policy={"is_pd_full":1,"selected_9d":"finite_positive","policy_text": SECRET})
        self.assert_clean(tool_facts("describe_database", overview))
        self.assertEqual(tool_facts("describe_database", overview)["data"]["eligible_rows"],3)
        self.assertEqual(tool_facts("describe_database", overview)["data"]["eligibility_policy"],
                         {"is_pd_full":1, "selected_9d":"finite_positive"})
        plan = session_facts(tools.model_snapshot(), phase="planning")
        self.assertEqual(plan["records"]["result"]["data"]["rows"], db.rows[:2])
        ref = raw["result_ref"]
        self.assertEqual(tools.model_snapshot()["records"]["result_ref"],ref)
        now[0] += TTL_SECONDS
        self.assertIsNone(tools.model_snapshot()["records"])
        self.assertEqual(tools.snapshot()["records"]["result_ref"],ref)
        with self.assertRaises(ChatToolError):
            await tools.execute("prepare_design_target", {"result_ref":ref, "position":1})

    def test_results_and_confirmation_do_not_copy_links_unknown_keys_or_free_text(self):
        result = {"status":"verified","job_id":JOB,"revision":11,"selected_candidate_id":"candidate-2",
                  "p13_status":"NOT_EVALUATED","viewer_url":PRIVATE, "viewer_display_verified":False,
                  "files":{"stl":{"url":SECRET},PRIVATE:{}}, "summary":{"counts":{"nodes":142,PRIVATE:2},
                  "warnings":[SECRET, {"command":PRIVATE}], "errors":[]}, "non_claims":["NOT_FEA_VALIDATION"]}
        original = copy.deepcopy(result)
        out = tool_facts("get_design_result", result)
        self.assert_clean(out)
        self.assertEqual(out["files"],["stl"])
        self.assertEqual(out["summary"],{"counts":{"nodes":142},"warnings_count":2,"errors_count":0})
        self.assertFalse(out["viewer_display_verified"])
        self.assertEqual(result, original)
        fail = tool_facts("get_design_result", {"status":"error","error":{"code":"RESULT_NOT_READY","message":PRIVATE}})
        self.assertEqual(fail,{"status":"error","error":{"code":"RESULT_NOT_READY"}})
        for name in ("prepare_design_target","prepare_workflow_action"):
            card = dict(status="awaiting_confirmation", confirmation_ref=REF, message=PRIVATE,
                        job_id=JOB, action="select_candidate", candidate_id="candidate-2", proposal_ref=SECRET)
            self.assert_clean(tool_facts(name,card))
            self.assertEqual(card["confirmation_ref"],REF)

    def test_default_trace_is_closed_and_can_correlate_actual_calls(self):
        case = ChatCase()
        run_id, call_id = "f0"*12, "ca"*12
        blob = json.dumps({"secret":SECRET,"cmd":PRIVATE})
        with self.assertLogs("latticegpt.chat") as logs:
            case.trace("model_request", run_id=run_id, call_id=call_id, phase="execution",
                       messages=[{"role":"tool","content":blob}], message=PRIVATE, profile={"base_url":SECRET})
            case.trace("tool_request", run_id=run_id, call_id=call_id, name="get_job_context",
                       arguments={"job_id": JOB, "job_ref":REF}, automatic=False)
            case.trace("tool_result", run_id=run_id, call_id=call_id, name="get_job_context",
                       result={"status":"TARGET_PROPOSED","private":blob,"target":{"source":SECRET}})
            case.trace("execution_receipt", run_id=run_id, receipt={"job_id":JOB, "revision":6, "status":"FAILED",
                       "action":"start_conformal","failed":True,"user_intent":blob,"failure":{"details":PRIVATE}})
            case.trace("tool_error", error_type=SECRET, name=PRIVATE, run_id=PRIVATE, phase=blob)
            case.trace(blob, name=blob, result={PRIVATE:SECRET})
        events = [json.loads(log.getMessage()) for log in logs.records]
        self.assertEqual([log.product_event for log in logs.records], events)
        self.assert_clean(events)
        self.assertEqual(events[0]["message_count"],1)
        self.assertEqual(events[0]["phase"],"execution")
        self.assertEqual([e["call_id"] for e in events[:3]], [call_id]*3)
        self.assertEqual(events[2]["result"],{"status":"TARGET_PROPOSED"})
        self.assertEqual(events[3]["receipt"],{"job_id":JOB,"revision":6,"status":"FAILED","action":"start_conformal","failed":True})
        self.assertIsNone(events[4]["run_id"])
        self.assertEqual(events[4]["error_type"],"OtherError")
        self.assertEqual(events[5]["event"],"other_event")
