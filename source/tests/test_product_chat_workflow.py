"""Synthetic database and compute; production tools, UI confirmations, Job and package chain."""
import asyncio
import csv
import hashlib
import json
import os
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from product_workflow import chainlit_ui as ui
from product_workflow.artifacts import LEGACY_FILE_KEYS, PRODUCT_FILE_MAP
from product_workflow.contracts import FORMAL_BUNDLE_ARTIFACT_NAMES, STIFFNESS_NAMES, formal_bundle_binding_from_verified_artifacts
from product_workflow.formal_150w_query import Formal150wQueryService, build_projection
from product_workflow.reproducibility import build_comparison_contract_v1
from product_workflow.runtime import adapter_controlled_write, conformal_controlled_run, verify_product_package
from product_workflow.runtime_manager import RuntimeExecutionManager
from product_workflow.service import ProductWorkflowService
from tests.test_product_chainlit_mesh_upload import _UiProbe, _RealAskProbe
from tests.test_product_workflow_mesh_runtime import VALID_INP, target_values
from tests.test_r3_reproduction_profile import valid_profile
from tests.test_product_chat_tools import SyntheticQuery
from product_workflow.chat_tools import ChatCase


class ChatWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_case_budget_survives_chat_invalidation_and_session_generation_change(self):
        def model(messages):
            return ({"kind": "plan", "task": "conversation"} if json.loads(messages[-1]['content']).get('phase') == 'planning'
                    else {"kind": "answer", "text": "解释"})
        with _UiProbe() as probe, patch.object(ui, "_chat_model", side_effect=model):
            probe.session.set("product_job_id", None)
            probe.session.set('product_chat_case', ChatCase(max_runs=10))
            for _ in range(10):
                self.assertEqual((await ui._run_product_chat("解释"))["status"], "answered")
            ui.invalidate_product_chat()
            probe.session.set("product_session_generation", uuid.uuid4().hex)
            self.assertEqual((await ui._run_product_chat("继续"))["status"], "budget_exhausted")

    async def test_issued_runtime_confirmation_expires_and_rejects_session_or_payload_changes(self):
        with _UiProbe() as probe:
            job_id = probe.session.get("product_job_id")
            for mutation in ("expiry", "session", "payload"):
                action = SimpleNamespace(id=uuid.uuid4().hex, payload={"job_id": job_id, "expected_revision": 2})
                ui._issue_workflow_confirmation(action, "product_confirm_target")
                record = ui._job_ui_state(job_id)["workflow_confirmations"][action.id]
                if mutation == "expiry":
                    record["issued_at"] -= 901
                elif mutation == "session":
                    probe.session.set("product_session_generation", uuid.uuid4().hex)
                else:
                    action.payload["expected_revision"] = 3
                self.assertFalse(await ui._consume_workflow_confirmation(action, "product_confirm_target"))

    async def test_legacy_runtime_buttons_require_current_session_issued_confirmation(self):
        self.make_service()
        from unittest.mock import Mock
        fake = SimpleNamespace(confirm_target_and_run_inverse=Mock(return_value={"status": "FAILED", "job_id": "job-old", "failure": {}}),
                               start_conformal=Mock(return_value={"status": "FAILED", "job_id": "job-old", "revision": 2, "failure": {}}))
        with _UiProbe() as probe, patch.object(ui, "_workflow", return_value=fake), \
                patch.object(ui, "_RUNTIME_MANAGER", RuntimeExecutionManager()):
            for current in ("job-different", "job-old"):
                probe.session.set("product_job_id", current)
                action = SimpleNamespace(id="unissued-action", payload={"job_id": "job-old", "expected_revision": 1})
                await ui.confirm_target(action)
                await ui.start_conformal(action)
            fake.confirm_target_and_run_inverse.assert_not_called()
            fake.start_conformal.assert_not_called()

    async def test_failed_production_callbacks_publish_recorded_failure_without_model(self):
        from llm_provider import resolve_model_runtime_profile
        from product_workflow.contracts import target_from_physical_9d
        for phase in ("inverse", "conformal"):
            self.make_service()
            profile = resolve_model_runtime_profile(provider="api", model_name="gpt-4o-mini", selection_source="session_selection", session_bound=True)
            job = self.service.create_job(target_from_physical_9d(target_values(), source_kind="csv_upload", source_sha256="A" * 64,
                source_name="synthetic.csv"), model_runtime_profile=profile)
            if phase == "conformal":
                job = self.service.confirm_target_and_run_inverse(job["job_id"], expected_revision=job["revision"], authorization={"authorized": True})
                job = self.service.select_candidate(job["job_id"], job["selection"]["selected_candidate_id"], expected_revision=job["revision"],
                    expected_candidate_set_sha256=job["candidate_set"]["candidate_set_sha256"], reason="synthetic user")
                job = self.service.bind_mesh(job["job_id"], original_filename="synthetic.inp", content=VALID_INP, expected_revision=job["revision"],
                    confirmed_contract={"units": "mm", "frame": "source_native_cartesian_frame", "design_domain": "whole_file_implant_domain"})
                self.service.adapter_runner = lambda **kwargs: (_ for _ in ()).throw(ValueError("synthetic adapter failure"))
            else:
                self.service.inverse_runner = lambda *args, **kwargs: {"status": "failed", "errors": ["synthetic inverse failure"]}
            seen = []
            def model(messages):
                seen.append(messages)
                self.fail('A recorded execution receipt must not invoke the model.')
            with _UiProbe() as probe, patch.object(ui, "_workflow", return_value=self.service), \
                    patch.object(ui, "_job_store", return_value=self.service.store), patch.object(ui, "_chat_model", side_effect=model), \
                    patch.object(ui, "_RUNTIME_MANAGER", RuntimeExecutionManager()), \
                    self.assertLogs('latticegpt.chat', level='INFO') as captured:
                probe.session.set("product_job_id", job["job_id"])
                probe.session.set("product_chat_enabled", True)
                tools = ui._product_chat_tools()
                context = await tools.execute("get_job_context", {})
                action = "confirm_target_and_generate" if phase == "inverse" else "start_conformal"
                request = await tools.execute("prepare_workflow_action", {"action": action, "job_ref": context["job_ref"]})
                await ui.confirm_chat_action(SimpleNamespace(payload={"token": request["confirmation_ref"]}))
                self.assertEqual(seen, [], phase)
                done = self.service.get_consistent_job(job["job_id"])
                self.assertEqual(done["status"], "FAILED")
                self.assertFalse(any(r["state"] == "issued" for r in tools.pending.values()))
                events = [json.loads(record.getMessage()) for record in captured.records]
                receipts = [event['receipt'] for event in events if event['event'] == 'execution_receipt']
                self.assertEqual(len(receipts), 1)
                self.assertTrue(receipts[0]['failed'])
                self.assertEqual(receipts[0]['action'], action)
                self.assertEqual((receipts[0]['job_id'], receipts[0]['revision']),
                                 (done['job_id'], done['revision']))
                fact = probe.session.get('product_chat_agent').history[-1]['content']
                self.assertIn('did not complete successfully', fact)
                self.assertIn('No action was retried', fact)
                self.assertTrue(any(fact in message.get('output', '') for message in probe.messages.values()))
                self.assertFalse(any(event['event'] == 'model_request' for event in events))

    async def test_stale_job_revision_rejects_prepared_action(self):
        from llm_provider import resolve_model_runtime_profile
        from product_workflow.chat_tools import ChatToolError, ChatTools
        from product_workflow.contracts import target_from_physical_9d
        self.make_service()
        profile = resolve_model_runtime_profile(provider="api", model_name="gpt-4o-mini", selection_source="session_selection", session_bound=True)
        created = self.service.create_job(target_from_physical_9d(target_values(), source_kind="csv_upload", source_sha256="A" * 64,
            source_name="synthetic.csv"), model_runtime_profile=profile)
        tools = ChatTools(query_service=lambda: self.query, read_job=self.service.get_consistent_job, read_result=lambda _: None,
            current_job_id=lambda: created["job_id"], session_id="synthetic")
        context = await tools.execute("get_job_context", {})
        action = await tools.execute("prepare_workflow_action", {"action": "confirm_target_and_generate", "job_ref": context["job_ref"]})
        ready = self.service.confirm_target_and_run_inverse(created["job_id"], expected_revision=created["revision"], authorization={"authorized": True})
        with self.assertRaises(ChatToolError):
            await tools.consume_action(action["confirmation_ref"])
        with self.assertRaises(ChatToolError):
            await tools.execute("prepare_workflow_action", {"action": "select_candidate", "job_ref": context["job_ref"], "candidate_id": "synthetic-0"})
        current = await tools.execute("get_job_context", {})
        self.assertEqual(current["revision"], ready["revision"])
        self.assertEqual(current["candidates"][0]["predicted_stiffness"], target_values())
        self.assertEqual(self.inverse_calls, 1)

    async def test_execution_receipt_waits_for_ordinary_chat_and_is_not_lost(self):
        from llm_provider import resolve_model_runtime_profile
        from product_workflow.contracts import target_from_physical_9d
        self.make_service()
        model_profile = resolve_model_runtime_profile(provider="api", model_name="gpt-4o-mini", selection_source="session_selection", session_bound=True)
        job = self.service.create_job(target_from_physical_9d(target_values(), source_kind="csv_upload", source_sha256="A" * 64,
            source_name="synthetic.csv"), model_runtime_profile=model_profile)
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def model(messages):
            context = next(json.loads(m["content"]) for m in messages if m["role"] == "tool" and '"execution_receipt"' in m["content"])
            seen.append(context)
            self.assertIsNone(context['execution_receipt'])
            if context['phase'] == 'planning':
                return {'kind': 'plan', 'task': 'conversation'}
            entered.set()
            await release.wait()
            return {"kind": "answer", "text": "A general workflow explanation."}
        with _UiProbe() as probe, patch.object(ui, "_chat_model", new=model), \
                patch.object(ui, "_chat_read_job", side_effect=self.service.get_consistent_job), \
                self.assertLogs('latticegpt.chat', level='INFO') as captured:
            probe.session.set("product_job_id", None)
            ordinary = asyncio.create_task(ui._run_product_chat("先解释，再继续比较C11"))
            await entered.wait()
            probe.session.set("product_job_id", job["job_id"])
            receipt = asyncio.create_task(ui._continue_product_chat("create_job", job["job_id"]))
            await asyncio.sleep(.03)
            try:
                self.assertFalse(receipt.done())
            finally:
                release.set()
                await ordinary
                await receipt
            self.assertEqual([item['phase'] for item in seen], ['planning', 'execution'])
            events = [json.loads(record.getMessage()) for record in captured.records]
            receipts = [event['receipt'] for event in events if event['event'] == 'execution_receipt']
            self.assertEqual(len(receipts), 1)
            self.assertNotIn('user_intent', receipts[0])
            self.assertNotIn('比较C11', '\n'.join(record.getMessage() for record in captured.records))
            self.assertEqual(receipts[0]['job_id'], job['job_id'])
            fact = probe.session.get('product_chat_agent').history[-1]['content']
            self.assertIn('Target prepared', fact)
            self.assertTrue(any(fact in message.get('output', '') for message in probe.messages.values()))

    async def test_only_one_creation_can_be_in_flight_across_new_proposals(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def slow_create(*args, **kwargs):
            calls.append(args)
            entered.set()
            await release.wait()
        with _UiProbe() as probe, patch.object(ui, "_query_service", return_value=SyntheticQuery()), \
                patch.object(ui, "_create_and_show_target", side_effect=slow_create):
            probe.session.set("product_job_id", None)
            tools = ui._product_chat_tools()
            result = await tools.execute("query_records", {"conditions": []})
            first = await tools.execute("prepare_design_target", {"result_ref": result["result_ref"], "position": 1})
            task = asyncio.create_task(ui.confirm_chat_action(SimpleNamespace(payload={"token": first["confirmation_ref"]})))
            await entered.wait()
            try:
                second = await tools.execute("prepare_design_target", {"result_ref": result["result_ref"], "position": 2})
                other = asyncio.create_task(ui.confirm_chat_action(SimpleNamespace(payload={"token": second["confirmation_ref"]})))
                await asyncio.sleep(.05)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                await task
                if 'other' in locals():
                    await other

    async def test_receipt_is_dropped_when_session_changes_during_job_read(self):
        entered, release = threading.Event(), threading.Event()
        def read(job_id):
            entered.set()
            release.wait(2)
            return {"job_id": job_id, "revision": 3, "status": "CANDIDATES_READY"}
        with _UiProbe() as probe, patch.object(ui, "_chat_read_job", side_effect=read), \
                patch.object(ui, "_run_product_chat", new=AsyncMock()) as run:
            probe.session.set("product_chat_enabled", True)
            task = asyncio.create_task(ui._continue_product_chat("select_candidate", probe.session.get("product_job_id")))
            await asyncio.to_thread(entered.wait, 2)
            probe.session.set("product_session_generation", "new-session")
            release.set()
            await task
            run.assert_not_awaited()

    def make_service(self):
        self.root = Path(os.environ["LATTICEGPT_TEST_RUNTIME_ROOT"]) / ("chat-chain-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.inverse_calls = self.compute_calls = 0
        self.profile = valid_profile()
        bundle = formal_bundle_binding_from_verified_artifacts({n: {"path": f"C:/synthetic/{n}", "size_bytes": i,
            "sha256": f"{i:X}" * 64, "status": "PASS"} for i, n in enumerate(FORMAL_BUNDLE_ARTIFACT_NAMES, 1)})

        def inverse(target, **kwargs):
            self.inverse_calls += 1
            return {"status": "success", "model_variant": "150w", "runtime_mode": "formal_product", "cad_ready_count": 3,
                    "warnings": [], "errors": [], "steps": [], "cleanup": {"status": "complete", "raw_files_available": False,
                    "work_dir_removed": True}, "work_dir_retained": False,
                    "candidates": [{"candidate_id": f"synthetic-{i}", "cad_ready": True, "predicted_stiffness": target_values(),
                        "diameter": .1, "relative_density": .2, "nodes": [[0, 0, 0], [1, 1, 1]], "connections": [[0, 1]]} for i in range(3)]}
        producer = {"generation": "r4_lite_single_reference_workstation_v22", **{k: self.profile["identities"][k]
                    for k in ("source_manifest_sha256", "config_sha256", "active_release_binding_sha256")}}
        self.service = ProductWorkflowService(self.root, inverse_runner=inverse,
            scaler_y_min={n: 0 for n in STIFFNESS_NAMES}, scaler_y_max={n: 1 for n in STIFFNESS_NAMES},
            formal_bundle_binding=bundle, formal_bundle_verifier=lambda: bundle,
            adapter_runner=adapter_controlled_write, conformal_runner=conformal_controlled_run, package_verifier=verify_product_package,
            reproduction_profile=self.profile, comparison_contract=build_comparison_contract_v1(
                tolerance_schema_path=self.profile["tolerance"]["schema_path"], tolerance_schema_sha256=self.profile["tolerance"]["schema_sha256"]),
            source_manifest_sha256=self.profile["identities"]["source_manifest_sha256"], readonly_package_producer=producer)
        source = self.root / "synthetic.csv"
        rows = [dict(sample_id=i, relative_density=.2, diameter=.1, is_singular=0, is_pd_full=1,
                     has_periodic_mismatch=0, is_degenerate=0, **{n: v / 110 for n, v in target_values().items()}) for i in (17, 42, 93)]
        with source.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        sha = hashlib.sha256(source.read_bytes()).hexdigest().upper()
        build_projection(source, self.root / "synthetic.sqlite", self.root / "synthetic_projection.json", expected_source_sha256=sha)
        self.query = Formal150wQueryService(self.root / "synthetic.sqlite", self.root / "synthetic_projection.json", expected_source_sha256=sha)

    def compute(self, request):
        self.compute_calls += 1
        package = Path(request["output_root"]) / request["package_id"]
        adapter = json.loads(Path(request["adapter_metadata_path"]).read_text(encoding="utf-8"))
        metadata = {"result_id": request["package_id"], "created_by": "cad_module.build_conformal_result_package",
                    "stage": "6.11d-conformal-service-runtime-validation",
                    "traceability": {**adapter["traceability"], "case_manifest_sha256": adapter["case_manifest_sha256"]},
                    "product_mesh_binding": request["product_mesh_binding"]}
        for key in LEGACY_FILE_KEYS:
            path = package / PRODUCT_FILE_MAP[key]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(Path(request["source_mesh_path"]).read_bytes() if key == "source_mesh" else
                             json.dumps(metadata).encode() if key == "metadata" else b"synthetic isolated artifact\n")
        return {"status": "success", "package_id": request["package_id"], "package_dir": str(package),
                "metadata_path": str(package / "metadata.json")}

    def script(self, messages):
        initial = next(json.loads(m["content"]) for m in messages if m["role"] == "tool" and '"execution_receipt"' in m["content"])
        snapshot, receipt = initial["context"], initial["execution_receipt"]
        last = json.loads(messages[-1]["content"]) if messages[-1]["role"] == "tool" else {}
        self.assertIsNone(receipt, 'Receipts explain recorded facts without a model or preparing the next action.')
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if initial.get('phase') == 'planning':
            plans = {
                '说明数据库': {'kind': 'plan', 'task': 'overview', 'arguments': {}},
                '查三条': {'kind': 'plan', 'task': 'records', 'arguments': {'conditions': [
                    {'field': 'relative_density', 'op': 'lt', 'value': 30, 'unit': '%'},
                    {'field': 'C11', 'op': 'gt', 'value': 11, 'unit': 'GPa'}], 'limit': 3}},
                '其中多少': {'kind': 'plan', 'task': 'count', 'arguments': {'inherit': True}},
                '用第二条': {'kind': 'plan', 'task': 'workflow'},
                '准备生成三个候选': {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'prepare_generation'}},
                '选择第1名': {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'choose_candidate', 'rank': 1}},
                '准备上传网格': {'kind': 'plan', 'task': 'workflow'},
                '准备共形确认': {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'prepare_conformal'}},
                '比较候选': {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'compare_candidates'}},
                '解释NMSE': {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'compare_candidates', 'focus': 'metrics'}},
                '比较密度': {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'compare_candidates', 'focus': 'density'}},
                '解释负差值': {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'compare_candidates', 'focus': 'component_delta'}},
                '读取最终结果': {'kind': 'plan', 'task': 'workflow', 'arguments': {'intent': 'read_result'}},
            }
            return plans[user]
        if user in {'比较候选', '解释NMSE', '比较密度', '解释负差值', '读取最终结果'}:
            self.fail('Controlled Job facts must not be rewritten by an execution model.')
        if user == '准备上传网格':
            return {'kind': 'tool', 'name': 'prepare_workflow_action', 'arguments': {
                'action': 'request_mesh_upload', 'job_ref': snapshot['job']['job_ref']}}
        if "kind" in last or "matched_count" in str(last):
            return {"kind": "answer", "text": "已读取实际合成查询结果。"}
        if user == "说明数据库":
            return {"kind": "tool", "name": "describe_database", "arguments": {}}
        if user == "查三条":
            return {"kind": "tool", "name": "query_records", "arguments": {"conditions": [
                {"field": "relative_density", "op": "lt", "value": 30, "unit": "%"},
                {"field": "C11", "op": "gt", "value": 11, "unit": "GPa"}], "limit": 3}}
        if user == "其中多少":
            return {"kind": "tool", "name": "count_records", "arguments": {"inherit": True}}
        return {"kind": "tool", "name": "prepare_design_target", "arguments": {"result_ref": snapshot["records"]["result_ref"], "position": 2}}

    def latest_confirmation(self, probe, kind):
        tools = ui._product_chat_tools()
        for action in reversed(list(probe.actions.values())):
            if action["name"] == "product_chat_confirm":
                token = action["payload"]["token"]
                if token in tools.pending and tools.pending[token]["action"] == kind and tools.pending[token]["state"] == "issued":
                    return SimpleNamespace(**action)
        self.fail(f"missing confirmation for {kind}; outputs=" + str([m.get("output") for m in probe.messages.values()][-8:]))

    async def test_workflow_plan_binds_cached_sample_and_current_rank_to_real_confirmation_cards(self):
        from llm_provider import resolve_model_runtime_profile

        self.make_service()
        comparison_calls = []

        def model(messages):
            context = json.loads(messages[-1]["content"])
            if context.get("execution_receipt"):
                return {"kind": "answer", "text": "Generic workflow status from the model."}
            text = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            if text == "查三条":
                return self.script(messages)
            if text.startswith("比较当前三个候选") and context["phase"] == "execution":
                self.fail('A read-only comparison must not invoke an execution model or prepare a selection.')
            self.assertEqual(context["phase"], "planning")
            if text == "用刚才第 2 条作为目标":
                return {"kind": "plan", "task": "workflow", "arguments": {"intent": "reference_target", "position": 2}}
            self.assertEqual(context["context"]["active_job_id"], ready["job_id"])
            if text.startswith("比较当前三个候选"):
                comparison_calls.append(text)
                return {"kind": "plan", "task": "workflow", "arguments": {"intent": "compare_candidates"}}
            args = {"intent": "choose_candidate", "rank": 2}
            if text == "从另一个任务选候选":
                args["job_id"] = "job-" + "F" * 24
            return {"kind": "plan", "task": "workflow", "arguments": args}

        with _UiProbe() as probe, patch.object(ui, "_query_service", return_value=self.query), \
                patch.object(ui, "_workflow", return_value=self.service), \
                patch.object(ui, "_job_store", return_value=self.service.store), patch.object(ui, "_chat_model", side_effect=model):
            probe.session.set("product_job_id", None)
            self.assertEqual((await ui._run_product_chat("查三条"))["status"], "answered")
            self.assertEqual((await ui._run_product_chat("用刚才第 2 条作为目标"))["status"], "waiting_for_user")
            create = self.latest_confirmation(probe, "create_job")
            self.assertEqual(create.label, "Create Job with this target")
            target = ui._product_chat_tools().pending[create.payload["token"]]["target"]
            self.assertEqual(target["source"]["sample_id"], 42)
            self.assertFalse(self.service.store.jobs_root.exists())

            profile = resolve_model_runtime_profile(provider="api", model_name="gpt-4o-mini", selection_source="session_selection", session_bound=True)
            job = self.service.create_job(target, model_runtime_profile=profile)
            ready = self.service.confirm_target_and_run_inverse(job["job_id"], expected_revision=job["revision"], authorization={"authorized": True})
            rank2 = next(c for c in ready["candidate_set"]["candidates"] if c["rank"] == 2)
            probe.session.set("product_job_id", ready["job_id"])
            ui.invalidate_product_chat()
            previous = set(probe.messages)
            comparison = await ui._run_product_chat("比较当前三个候选")
            self.assertEqual((comparison["status"], comparison["answer_basis"]), ("answered", "workflow"))
            visible = "\n".join(m["output"] for key, m in probe.messages.items() if key not in previous)
            self.assertIn("Candidate comparison", visible)
            self.assertIn("Predicted stiffness", visible)
            self.assertIn("prediction − target", visible)
            for item in ready["candidate_set"]["candidates"]:
                self.assertIn(f"| {item['rank']} | `{item['candidate_id']}` | {item['score']:.6g} |", visible)
            target_c11 = ready["target"]["physical_9d_gpa"]["C11"]
            predicted_c11 = rank2["predicted_stiffness"]["C11"]
            self.assertIn(f"{predicted_c11:.6g} ({predicted_c11 - target_c11:+.6g})", visible)
            self.assertNotIn("| Component | Target stiffness (GPa) |", visible)
            details = next(e for e in probe.elements.values() if e["name"] == "View target stiffness (GPa)")
            self.assertEqual(details["display"], "side")
            self.assertIn(f"| C11 | {target_c11:.6g} |", details["content"])
            readonly = await ui._run_product_chat("比较当前三个候选，只读")
            self.assertEqual(readonly['status'], 'answered')
            self.assertEqual(readonly['answer_origin'], 'program_job_facts')
            self.assertEqual(readonly['job_evidence']['job_id'], ready['job_id'])
            self.assertEqual(comparison_calls, ['比较当前三个候选', '比较当前三个候选，只读'])
            self.assertEqual(probe.session.get('product_chat_agent').history[-1]['content'], readonly['text'])
            self.assertEqual(ui._product_chat_tools().pending, {})
            previous = set(probe.messages)
            self.assertEqual((await ui._run_product_chat("请给我第 2 名候选的确认卡"))["status"], "waiting_for_user")
            select = self.latest_confirmation(probe, "select_candidate")
            self.assertEqual(select.label, f"Confirm Rank 2 · {rank2['candidate_id']}")
            self.assertEqual(len(set(probe.messages) - previous), 1)
            self.assertEqual((await ui._run_product_chat("从另一个任务选候选"))["status"], "error")
            self.assertEqual(self.latest_confirmation(probe, "select_candidate").payload, select.payload)
            self.assertEqual(self.service.get_consistent_job(ready["job_id"])["revision"], ready["revision"])
            previous = set(probe.messages)
            await ui.confirm_chat_action(select)
            saved = self.service.get_consistent_job(ready["job_id"])
            self.assertEqual(saved["selection"]["confirmation"]["selected_candidate_id"], rank2["candidate_id"])
            visible = "\n".join(m["output"] for key, m in probe.messages.items() if key not in previous)
            self.assertEqual(visible.count(f"Selected Rank 2 · `{rank2['candidate_id']}`."), 1)
            self.assertIn("Upload hexahedral mesh (.inp)", visible)
            self.assertNotIn("Candidates generated", visible)
            self.assertNotIn("| Component | Target stiffness (GPa) |", visible)
            await asyncio.gather(*(probe.session.get("product_preview_tasks") or []))

    async def test_complete_database_to_sealed_result_through_real_ui_confirmations(self):
        self.make_service()
        uploaded = self.root / "user-selected.inp"; uploaded.write_bytes(VALID_INP)
        config = SimpleNamespace(paths=SimpleNamespace(conformal_root=self.service.conformal_output_root),
            services=SimpleNamespace(viewer_frontend_origin="http://synthetic.invalid", viewer_backend_origin="http://synthetic.invalid"))
        async def answer(probe, payload):
            self.assertIn(payload["spec"]["type"], {"action", "element"})
            return probe.response(confirm=True)
        with _RealAskProbe(answer) as probe, patch.object(ui, "_workflow", return_value=self.service), \
                patch.object(ui, "_job_store", return_value=self.service.store), \
                patch.object(ui, "_readonly_viewer_store", return_value=self.service.store), \
                patch.object(ui, "_query_service", return_value=self.query), patch.object(ui, "_product_config", return_value=config), \
                patch.object(ui, "_chat_model", side_effect=self.script), patch.object(ui, "_RUNTIME_MANAGER", RuntimeExecutionManager()), \
                patch.object(ui, "_ask_for_product_mesh", return_value=[SimpleNamespace(name="synthetic.inp", path=str(uploaded))]), \
                patch.object(ui, "_render_result_picture", new=AsyncMock(return_value=b"isolated-picture")), \
                patch("product_workflow.runtime.run_conformal_service", side_effect=self.compute), \
                self.assertLogs("latticegpt.chat", level="INFO") as captured:
            probe.session.set("product_job_id", None)
            for text in ("说明数据库", "查三条", "其中多少", "用第二条"):
                await ui.handle_product_message(SimpleNamespace(content=text, elements=[]))
            self.assertEqual(self.inverse_calls, 0)
            self.assertFalse(self.service.store.jobs_root.exists())
            create = self.latest_confirmation(probe, "create_job")
            await ui.confirm_chat_action(create)
            job_id = probe.session.get("product_job_id")
            self.assertIsNotNone(job_id)
            self.assertEqual(self.service.get_consistent_job(job_id)["target"]["source"]["sample_id"], 42)
            self.assertEqual(self.inverse_calls, 0)
            self.assertFalse(any(r['state'] == 'issued' for r in ui._product_chat_tools().pending.values()))
            self.assertEqual((await ui._run_product_chat('准备生成三个候选'))['status'], 'waiting_for_user')
            previous = set(probe.messages)
            await ui.confirm_chat_action(self.latest_confirmation(probe, "confirm_target_and_generate"))
            self.assertEqual(self.inverse_calls, 1)
            visible = "\n".join(m["output"] for key, m in probe.messages.items() if key not in previous)
            self.assertNotIn("Use the available actions on the current Job card.", visible)
            self.assertFalse(any(r['state'] == 'issued' for r in ui._product_chat_tools().pending.values()))
            case = ui._product_chat_tools().case
            for question in ('比较候选', '解释NMSE', '比较密度', '解释负差值'):
                explanation = await ui._run_product_chat(question)
                self.assertEqual(explanation['status'], 'answered')
                self.assertEqual(explanation['answer_origin'], 'program_job_facts')
                self.assertEqual(explanation['job_evidence']['job_id'], job_id)
            self.assertEqual((await ui._run_product_chat('选择第1名'))['status'], 'waiting_for_user')
            await ui.confirm_chat_action(self.latest_confirmation(probe, "select_candidate"))
            self.assertFalse(any(r['state'] == 'issued' for r in ui._product_chat_tools().pending.values()))
            self.assertEqual((await ui._run_product_chat('准备上传网格'))['status'], 'waiting_for_user')
            await ui.confirm_chat_action(self.latest_confirmation(probe, "request_mesh_upload"))
            await ui.product_mesh_upload_entry(probe.current_entry())
            self.assertEqual(self.compute_calls, 0)
            mesh_context = ui._product_chat_tools().snapshot()["job"]["mesh"]
            self.assertEqual(mesh_context["original_upload"]["original_filename"], "synthetic.inp")
            self.assertRegex(mesh_context["binding_sha256"], r"^[0-9A-F]{64}$")
            self.assertFalse(any(r['state'] == 'issued' for r in ui._product_chat_tools().pending.values()))
            self.assertEqual((await ui._run_product_chat('准备共形确认'))['status'], 'waiting_for_user')
            start = self.latest_confirmation(probe, "start_conformal")
            await ui.confirm_chat_action(start)
            done = self.service.get_consistent_job(job_id)
            self.assertEqual(done["status"], "VIEWER_READY", done.get("failure"))
            await asyncio.gather(*(probe.session.get("product_preview_tasks") or []))
            self.assertEqual(self.compute_calls, 1)
            final_answer = await ui._run_product_chat('读取最终结果')
            self.assertEqual(final_answer['status'], 'answered')
            self.assertEqual(final_answer['answer_origin'], 'program_job_facts')
            self.assertEqual(final_answer['design_result']['status'], 'verified')
            self.assertEqual(final_answer['design_result']['job_id'], done['job_id'])
            self.assertEqual(final_answer['design_result']['revision'], done['revision'])
            self.assertEqual(probe.session.get('product_chat_agent').history[-1]['content'], final_answer['text'])
            self.assertIs(ui._product_chat_tools().case, case)
            self.assertGreater(case.used['runs'], 10)
            self.assertLessEqual(case.used['runs'], 20)
            self.assertLessEqual(case.used['model_calls'], 40)
            self.assertFalse(case.exhausted)
            await ui.confirm_chat_action(start)
            await ui.confirm_chat_action(create)
            self.assertEqual(self.compute_calls, 1)
            self.assertEqual(self.inverse_calls, 1)
            self.assertEqual(len(list(self.service.store.jobs_root.iterdir())), 1)
            from product_workflow.artifacts import resolve_product_package
            package = resolve_product_package(self.service.store, job_id, self.service.conformal_output_root)
            path = package.package_dir / PRODUCT_FILE_MAP["b31"]
            path.write_bytes(b"tampered")
            failed = await ui._product_chat_tools().execute("get_design_result", {})
            self.assertEqual(failed['error']['code'], 'RESULT_IDENTITY_MISMATCH')
            events = [json.loads(record.getMessage()) for record in captured.records]
            self.assertEqual([event['plan']['task'] for event in events if event['event'] == 'query_plan'],
                             ['overview', 'records', 'count', 'workflow'] + ['workflow'] * 9)
            self.assertFalse(any(event['event'] == 'model_request' and event.get('phase') == 'receipt' for event in events))
            self.assertEqual(len({event["case_id"] for event in events}), 1)
            self.assertTrue({"confirmation_issued", "confirmation_consumed", "ui_confirmation", "execution_receipt",
                             "mesh_contract_confirmation", "mesh_domain_confirmation", "tool_result"}
                            <= {event["event"] for event in events})
            result_event = next(event for event in events if event["event"] == "tool_result"
                                and event["name"] == "get_design_result")
            self.assertEqual(result_event['result']['status'], 'verified')
            self.assertEqual(result_event['result']['file_role_count'], len(LEGACY_FILE_KEYS))
            self.assertNotIn('files', result_event['result'])
            self.assertNotIn('package_id', result_event['result'])
            self.assertNotIn(start.payload["token"], "\n".join(record.getMessage() for record in captured.records))
