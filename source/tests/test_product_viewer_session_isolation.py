"""Production session lifecycle with isolated storage and synthetic model calls."""
import asyncio
import copy
import json
import threading
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from chainlit.chat_context import chat_contexts
from chainlit.context import ChainlitContext, context_var
from chainlit.emitter import ChainlitEmitter
from chainlit.user_session import user_sessions

import latticegpt_v1_app as entry
from product_workflow import chainlit_ui as ui
from product_workflow.chat_tools import CHAT_DISPATCH
from product_workflow.config import load_latticegpt_config
from product_workflow.contracts import STIFFNESS_NAMES, canonical_sha256, target_from_physical_9d
from product_workflow.runtime_manager import RuntimeExecutionManager
from product_workflow.service import ProductWorkflowService
from tests.test_product_chainlit_mesh_upload import _RealAskProbe
from tests.test_product_chainlit_routing import full_product_preflight
from tests.test_product_workflow_mesh_runtime import ProductWorkflowMeshRuntimeTests, target_values


class SessionProbe(_RealAskProbe):
    """Use the installed emitter without patching context-bound UserSession."""
    def __init__(self):
        async def unexpected_ask(*_):
            raise AssertionError("This test must not open an upload Ask")
        super().__init__(unexpected_ask)
        self.sid = uuid.uuid4().hex
        self.ws = SimpleNamespace(id=self.sid, thread_id=uuid.uuid4().hex,
            emit=self.record, emit_call=self.call, files_spec={}, files={},
            has_first_interaction=True, persist_file=AsyncMock(return_value={"id":uuid.uuid4().hex}),
            user_env={}, chat_settings={}, user=None, chat_profile=None, client_type="webapp")
        self.emitter = ChainlitEmitter(self.ws)
        self.context = ChainlitContext(self.ws,self.emitter)

    async def record(self,kind,payload):
        if not isinstance(payload,dict):
            self.events.append((kind,copy.deepcopy(payload)))
            return
        await super().record(kind,payload)
        if kind=="update_message": self.messages[payload["id"]]=dict(payload)

    @contextmanager
    def active(self):
        token=context_var.set(self.context)
        try: yield self
        finally: context_var.reset(token)

    def output(self):
        return "\n".join(str(m.get("output","")) for m in self.messages.values())


class ViewerSessionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=ProductWorkflowMeshRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config=load_latticegpt_config(explicit={"release_root":self.fixture.root,
            "runtime_root":self.fixture.root/"isolated"},environ={})
        self.template,_,_,_=self.fixture._r3_service_and_ready_job(runtime_root=self.config.paths.runtime_root)
        self.preflight=full_product_preflight()
        self.compute=dict.fromkeys(("inverse","adapter","conformal"),0)
        self.instances,self.probes=[],[]
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(ui,_CONFIG=None,_PREFLIGHT=None,_WORKFLOW=None,
            _QUERY=None,_JOB_STORE=None,_RUNTIME_RECONCILED=False,_RUNTIME_MANAGER=RuntimeExecutionManager()))
        self.stack.enter_context(patch.object(entry,"load_latticegpt_config",return_value=self.config))
        self.stack.enter_context(patch.object(entry,"require_preflight",return_value=self.preflight))
        self.stack.enter_context(patch.object(ui,"create_product_workflow_service",side_effect=self.new_service))
        for name in ("chainlit.message.get_data_layer","chainlit.element.get_data_layer","chainlit.emitter.get_data_layer"):
            self.stack.enter_context(patch(name,return_value=None))

    async def asyncTearDown(self):
        for probe in self.probes:
            if probe.sid in user_sessions:
                with probe.active():
                    await self.drain_previews()
                    await entry.on_v1_chat_end()
            user_sessions.pop(probe.sid,None)
            chat_contexts.pop(probe.sid,None)

    def new_service(self,config,*,preflight):
        self.assertIs(config,self.config)
        self.assertIs(preflight,self.preflight)
        def inverse(*args,**kwargs):
            self.compute["inverse"]+=1
            return self.template.inverse_runner(*args,**kwargs)
        def forbidden(kind):
            def runner(*_,**__):
                self.compute[kind]+=1
                raise AssertionError("Unexpected "+kind+" dispatch")
            return runner
        t=self.template
        service=ProductWorkflowService(config.paths.runtime_root,inverse_runner=inverse,
            scaler_y_min=t.scaler_y_min,scaler_y_max=t.scaler_y_max,
            formal_bundle_binding=t.formal_bundle_binding,formal_bundle_verifier=t.formal_bundle_verifier,
            adapter_runner=forbidden("adapter"),conformal_runner=forbidden("conformal"),
            package_verifier=t.package_verifier,adapter_output_root=config.paths.adapter_root,
            conformal_output_root=config.paths.conformal_root,reproduction_profile=t.reproduction_profile,
            comparison_contract=t.comparison_contract,source_manifest_sha256=t.source_manifest_sha256)
        self.instances.append(service)
        return service

    def probe(self):
        probe=SessionProbe()
        self.probes.append(probe)
        return probe

    def create_job(self,*,candidates=False):
        service=ui._workflow()
        proposal=target_from_physical_9d(target_values(),source_kind="csv_upload",
            source_sha256="A"*64,source_name="isolation-target.csv")
        job=service.create_job(proposal,model_runtime_profile=self.fixture.profile)
        if candidates:
            job=service.confirm_target_and_run_inverse(job["job_id"],expected_revision=job["revision"],authorization={"authorized":True})
            self.assertEqual(job["status"],"CANDIDATES_READY")
        ui.cl.user_session.set("product_job_id",job["job_id"])
        ui.cl.user_session.set("product_job_revision",job["revision"])
        return service,job

    async def drain_previews(self):
        tasks=tuple(ui.cl.user_session.get("product_preview_tasks") or ())
        if tasks: await asyncio.gather(*tasks)

    def journal(self,store,job_id):
        folder=store.job_directory(job_id)
        return tuple((folder/name).read_bytes() for name in ("job.json","events.jsonl"))

    async def test_startup_preserves_candidate_permission_and_duplicate_is_readonly(self):
        b,a=self.probe(),self.probe()
        with b.active():
            await entry.on_v1_chat_start()
            original,job=self.create_job(candidates=True)
            source=canonical_sha256(job["candidate_set"])
            cache=ui._preview_cache(job["job_id"])
            for c in job["candidate_set"]["candidates"]: cache.put(f"cell-v1:{source}:{c['candidate_id']}",b"synthetic-preview")
            await ui._show_candidates(job)
            await self.drain_previews()
            card=ui._job_ui_state(job["job_id"])["cards"]["candidates"]
            wire=copy.deepcopy(next(c["action"] for c in card["component"].props["candidates"] if c["eligible"]))
            action=ui.cl.Action(**wire)
            permissions=copy.deepcopy(ui._job_ui_state(job["job_id"])["workflow_confirmations"])
            generation=ui._session_generation()
            before_compute=dict(self.compute)
            before=self.journal(original.store,job["job_id"])
        with a.active():
            await entry.on_v1_chat_start()
            self.assertIsNone(ui.cl.user_session.get("product_job_id"))
            self.assertIsNone(ui._WORKFLOW)
        with b.active():
            self.assertEqual(ui._session_generation(),generation)
            self.assertEqual(ui._job_ui_state(job["job_id"])["workflow_confirmations"],permissions)
            self.assertEqual(self.journal(original.store,job["job_id"]),before)
            await ui.select_candidate(action)
            await self.drain_previews()
            current_service=ui._workflow()
            self.assertIsNot(current_service,original)
            current=current_service.store.get_consistent(job["job_id"])
            self.assertEqual(current["revision"],job["revision"]+1)
            self.assertEqual(sum(e["event"]=="candidate_selected" and e["revision"]>job["revision"]
                for e in current_service.store.read_events(job["job_id"])),1)
            committed=self.journal(current_service.store,job["job_id"])
            self.assertNotEqual(committed,before)
            await ui.select_candidate(action)
            await self.drain_previews()
            self.assertEqual(self.journal(current_service.store,job["job_id"]),committed)
            self.assertEqual(self.compute,before_compute)
            self.assertTrue(ui._confirmed_hash(current))

    async def delayed_turn(self,phase,*,start_other_session):
        b,a=self.probe(),self.probe()
        entered,release=threading.Event(),threading.Event()
        calls=[]
        marker=f"Controlled B response {b.sid}."
        def invoke(messages):
            envelope=json.loads(json.loads(messages[-1].content)[-1]["content"])
            calls.append(dict(phase=envelope["phase"],sid=ui.cl.context.session.id,
                generation=ui._session_generation(),dispatch=copy.deepcopy(CHAT_DISPATCH.get()),
                context=copy.deepcopy(envelope["context"])))
            if envelope["phase"]==phase:
                entered.set()
                if not release.wait(5): raise TimeoutError("Synthetic model release missing")
            value={"kind":"plan","task":"conversation"} if envelope["phase"]=="planning" else {"kind":"answer","text":marker}
            return SimpleNamespace(content=json.dumps(value))
        with b.active():
            await entry.on_v1_chat_start()
            service,job=self.create_job()
            values=target_values()
            row=",".join(str(values[n]) for n in STIFFNESS_NAMES)
            await ui._handle_csv_upload((",".join(STIFFNESS_NAMES)+"\n"+row+"\n"+row).encode(),filename="two-targets.csv")
            await ui.product_csv_select(ui._csv_import_state()["row_actions"][2])
            tools=ui._product_chat_tools()
            await tools.execute("get_job_context",{},automatic=True)
            expected=copy.deepcopy(tools.snapshot())
            generation=ui._session_generation()
            initial_budget=dict(tools.case.used)
            before=self.journal(service.store,job["job_id"])
            before_compute=dict(self.compute)
        with patch.object(ui,"get_chat_llm",return_value=SimpleNamespace(invoke=invoke)) as get_model:
            with b.active(): running=asyncio.create_task(entry.on_v1_message(SimpleNamespace(content="Explain my selected target.",elements=[])))
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait,3),4))
                if start_other_session:
                    with a.active():
                        await entry.on_v1_chat_start()
                        self.assertIsNone(ui.cl.user_session.get("product_chat_agent"))
            finally:
                release.set()
                await asyncio.wait_for(running,5)
            self.assertEqual(get_model.call_count,2)
        with b.active():
            self.assertIs(ui.cl.user_session.get("product_chat_tools"),tools)
            self.assertEqual(ui.cl.user_session.get("product_job_id"),job["job_id"])
            self.assertEqual(ui._session_generation(),generation)
            self.assertEqual(tools.snapshot()["pending_csv_target"],expected["pending_csv_target"])
            self.assertEqual(tools.csv_context_version,expected["csv_context_version"])
            delta={k:tools.case.used[k]-initial_budget[k] for k in initial_budget}
        self.assertEqual([c["phase"] for c in calls],["planning","execution"])
        for c in calls:
            self.assertEqual(c["sid"],b.sid)
            self.assertEqual(c["generation"],generation)
            self.assertEqual(c["context"]["job"]["job_id"],job["job_id"])
            csv=c["context"]["pending_csv_target"]
            for key in ("state","record_index","valid_count","invalid_count","record_count"):
                self.assertEqual(csv[key],expected["pending_csv_target"][key])
            self.assertNotIn("filename",csv)
            self.assertNotIn("sha256",csv)
            self.assertNotIn("csv_context_version",c["context"])
            self.assertEqual(csv["physical_9d_gpa"],expected["pending_csv_target"]["physical_9d_gpa"])
        self.assertEqual(calls[0]["dispatch"]["run_id"],calls[1]["dispatch"]["run_id"])
        self.assertNotEqual(calls[0]["dispatch"]["call_id"],calls[1]["dispatch"]["call_id"])
        self.assertIn(marker,b.output())
        self.assertNotIn(marker,a.output())
        self.assertEqual(self.journal(service.store,job["job_id"]),before)
        self.assertEqual(self.compute,before_compute)
        return delta

    async def check_phase(self,phase):
        baseline=await self.delayed_turn(phase,start_other_session=False)
        interleaved=await self.delayed_turn(phase,start_other_session=True)
        self.assertEqual(baseline,{"runs":1,"model_calls":2,"tool_calls":0})
        self.assertEqual(interleaved,baseline)
        self.assertEqual(self.compute,{"inverse":0,"adapter":0,"conformal":0})

    async def test_planning_model_wait_preserves_session_after_other_startup(self): await self.check_phase("planning")
    async def test_execution_model_wait_preserves_session_after_other_startup(self): await self.check_phase("execution")


if __name__=="__main__": unittest.main()
