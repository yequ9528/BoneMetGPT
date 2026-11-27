import asyncio
import os
import threading
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import tomli
from socketio.exceptions import TimeoutError as AskTimeoutError

from product_workflow import chainlit_ui
from product_workflow.contracts import PRODUCT_MESH_CONTRACT
from product_workflow.contracts import canonical_sha256
from product_workflow.service import ProductWorkflowService
from product_workflow.runtime_manager import RuntimeExecutionManager


def _message_recorder(records):
    def factory(*, content, actions=None, **kwargs):
        message = SimpleNamespace(
            id=uuid.uuid4().hex,
            content=content,
            actions=list(actions or []),
            elements=list(kwargs.get("elements") or []),
            kwargs=kwargs,
        )

        async def send():
            message.kwargs["elements"] = message.elements
            if all(record is not message for record in records):
                records.append(message)
            return message

        async def update():
            message.kwargs["elements"] = message.elements
            return message

        message.send = send
        message.update = update
        return message

    return factory


def _action_factory(**kwargs):
    return SimpleNamespace(id=uuid.uuid4().hex, remove=AsyncMock(), **kwargs)


class _Session:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class _BindingService:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def bind_mesh(self, job_id, **kwargs):
        self.calls.append((job_id, kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def get_job(self, job_id):
        return self.result

    def get_consistent_job(self, job_id):
        return self.result


def _bound_job(status="AWAITING_CONFORMAL_AUTHORIZATION"):
    value = {
        "job_id": "job-mesh-upload",
        "revision": 8,
        "status": status,
    }
    if status == "AWAITING_CONFORMAL_AUTHORIZATION":
        value["mesh_binding"] = {
            "schema_version": "latticegpt_mesh_binding_v1",
            "upload_id": "upload-0001",
            "original_filename": "implant.inp",
            "stored_path": "I:/synthetic/jobs/job-v1/uploads/upload-0001/source.inp",
            "size_bytes": 123,
            "sha256": "A" * 64,
            "node_count": 8,
            "element_count": 1,
            "element_type_original": "C3D8R",
            "element_type_effective": "C3D8",
            "mesh_binding_sha256": "B" * 64,
            "precheck": {
                "status": "passed",
                "geometry_quality": {"reference_length": 1.0},
                "mesh": {
                    "element_type_original": "C3D8R",
                    "element_type_effective": "C3D8",
                },
            },
        }
    elif status == "CANDIDATES_READY":
        value["selection"] = {"selected_candidate_id": "candidate-1"}
        value["mesh_uploads"] = [{
            "upload_id": "upload-0001",
            "status": "REJECTED",
            "precheck": {
                "status": "failed",
                "errors": ["fixture failure"],
            },
        }]
    if status == "CANDIDATES_READY":
        value["job_id"] = "job-" + "A" * 24
        _confirm_fixture(value)
    return value


def _bound_r3_job():
    raw_sha = "A" * 64
    return {
        "job_id": "job-r3-mesh-upload",
        "revision": 9,
        "status": "AWAITING_CONFORMAL_AUTHORIZATION",
        "mesh_uploads": [{
            "upload_id": "upload-0001",
            "original_filename": "implant-v2.inp",
            "stored_path": "I:/synthetic/jobs/job-r3/uploads/upload-0001/source.inp",
            "size_bytes": 456,
            "sha256": raw_sha,
            "status": "ACCEPTED",
            "precheck": {
                "status": "passed",
                "geometry_quality": {"reference_length": 1.0},
                "mesh": {
                    "element_type_original": "C3D8R",
                    "element_type_effective": "C3D8",
                },
            },
        }],
        "mesh_binding": {
            "schema_version": "latticegpt_mesh_binding_v2",
            "source": {
                "raw_mesh_relative_path": "input/source_c3d8.inp",
                "raw_size_bytes": 456,
                "raw_sha256": raw_sha,
                "raw_bytes_preserved": True,
                "original_filename": "implant-v2.inp",
                "units": "mm",
                "frame": "source_native_cartesian_frame",
                "transform": "identity",
                "rotation": "none",
                "reflection": "none",
                "implicit_scale": "none",
            },
            "parser": {},
            "precheck": {},
            "semantics": {
                "element_type_original": "C3D8R",
                "element_type_effective": "C3D8",
                "node_count": 8,
                "element_count": 1,
            },
            "design_domain": {},
            "relationship": {"upload_id": "upload-0001"},
            "digests": {"mesh_binding_sha256": "B" * 64},
        },
    }


def _synthetic_product_config():
    return SimpleNamespace(
        services=SimpleNamespace(
            viewer_frontend_origin="http://127.0.0.1:5173",
            viewer_backend_origin="http://127.0.0.1:8001",
        )
    )


def _temporary_upload_path(name: str) -> Path:
    root = os.environ.get("LATTICEGPT_TEST_RUNTIME_ROOT")
    if not root:
        raise AssertionError(
            "LATTICEGPT_TEST_RUNTIME_ROOT must name the explicit test root"
        )
    return Path(root) / f"{name}-{uuid.uuid4().hex}.inp"


def _upload_job(revision=7):
    job = {
        "job_id": "job-" + "A" * 24,
        "revision": revision,
        "status": "CANDIDATES_READY",
        "selection": {"selected_candidate_id": "candidate-1"},
    }
    return _confirm_fixture(job)


def _confirm_fixture(job):
    """Synthetic confirmation bytes for UI contract tests; no persisted human consent."""
    candidates = job.setdefault("candidate_set", {"candidates": [{"candidate_id": "candidate-1", "eligible": True}]})
    candidates.update(schema_version="latticegpt_candidate_set_v1", recommended_candidate_id="candidate-1")
    selected = candidates["candidates"][0]
    selected.update(rank=1, score=0.1, normalized_diameter=0.2, p09_validation={"status": "PASS"})
    candidates.pop("candidate_set_sha256", None)
    candidates["candidate_set_sha256"] = canonical_sha256(candidates)
    selection = ProductWorkflowService._selection("candidate-1", selected, source="default_rank1", reason="explicit synthetic test action", candidate_set_sha256=candidates["candidate_set_sha256"])
    confirmation = {"schema_version": "latticegpt_candidate_confirmation_v1", "job_id": job["job_id"],
        "candidate_set_schema_version": candidates["schema_version"], "candidate_set_sha256": candidates["candidate_set_sha256"],
        "selected_candidate_id": selected["candidate_id"], "selected_candidate_payload_sha256": canonical_sha256(selected),
        "action_revision": job["revision"] - 1, "confirmed_revision": job["revision"],
        "method": "explicit_user_selection", "confirmed_at": "2026-09-18T00:00:00+00:00"}
    confirmation["confirmation_sha256"] = canonical_sha256(confirmation)
    selection["confirmation"] = confirmation
    selection.pop("selection_sha256")
    selection["selection_sha256"] = canonical_sha256(selection)
    job["selection"] = selection
    return job


class _UiProbe:
    """Real Chainlit messages/actions with only the network/persistence replaced."""
    def __init__(self):
        self.events = []
        self.actions = {}
        self.messages = {}
        self.elements = {}
        self.embedded_by_element = {}
        self.session = _Session({"id": uuid.uuid4().hex, "product_job_id": "job-" + "A" * 24})
        self.fail_event = None
        self.block_event = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def record(self, kind, payload):
        if self.block_event == kind:
            self.entered.set()
            await self.release.wait()
        if self.fail_event == kind:
            raise RuntimeError("fixture transport failure")
        self.events.append((kind, dict(payload)))
        if kind in {"action", "embedded_action"}:
            self.actions[payload["id"]] = dict(payload)
        elif kind == "remove_action":
            self.actions.pop(payload["id"], None)
        elif kind in {"send_step", "update_step"}:
            self.messages[payload["id"]] = dict(payload)
        elif kind == "element":
            self.elements.setdefault(payload["id"], {}).update(payload)
            for action_id in self.embedded_by_element.pop(payload["id"], set()):
                self.actions.pop(action_id, None)
            props = payload.get("props") or {}
            embedded = list(props.get("actions") or [])
            embedded += [item["action"] for item in props.get("candidates", []) if item.get("action")]
            if not props.get("historical") and not props.get("locked"):
                self.embedded_by_element[payload["id"]] = {a["id"] for a in embedded}
                for action in embedded:
                    await self.record("embedded_action", action)
        elif kind == "remove_element":
            self.elements.pop(payload["id"], None)
            for action_id in self.embedded_by_element.pop(payload["id"], set()):
                self.actions.pop(action_id, None)

    def card_props(self, stage, job_id=None):
        job_id = job_id or self.session.get("product_job_id")
        return self.session.get(f"product_job_ui:{job_id}")["cards"][stage]["component"].props

    def stage_actions(self, stage, job_id=None):
        job_id = job_id or self.session.get("product_job_id")
        return self.session.get(f"product_job_ui:{job_id}")["cards"][stage]["actions"]

    def _patch_element_persistence(self):
        from chainlit.element import Element

        async def create(element, persist=True):
            element.url = f"https://fixture.invalid/elements/{element.id}"
            element.persisted = True
            self.elements[element.id] = {**element.to_dict(), "content": element.content}
            return True

        self.stack.enter_context(patch("chainlit.element.get_data_layer", return_value=None))
        self.stack.enter_context(patch.object(Element, "_create", new=create))

    def __enter__(self):
        from chainlit.context import context_var
        from chainlit.chat_context import chat_contexts
        self.context_var = context_var
        self.chat_contexts = chat_contexts
        emitter = SimpleNamespace(
            emit=self.record,
            send_step=lambda value: self.record("send_step", value),
            update_step=lambda value: self.record("update_step", value),
            send_element=lambda value: self.record("element", value),
            task_start=lambda: self.record("task_start", {}),
            task_end=lambda: self.record("task_end", {}),
            clear=lambda kind: self.record(kind, {}),
        )
        self.token = context_var.set(SimpleNamespace(
            session=SimpleNamespace(id=self.session.get("id"), thread_id="fixture-thread"),
            emitter=emitter,
        ))
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(chainlit_ui.cl, "user_session", self.session))
        self.stack.enter_context(patch("chainlit.message.get_data_layer", return_value=None))
        self._patch_element_persistence()
        async def explicit_contract(lease):
            return {"payload": {"entry_token": lease["entry_token"], "confirm": True}}
        self.stack.enter_context(patch.object(chainlit_ui, "_ask_for_mesh_contract", side_effect=explicit_contract))
        return self

    def __exit__(self, *args):
        self.stack.close()
        self.chat_contexts.pop(self.session.get("id"), None)
        self.context_var.reset(self.token)

    def current_entry(self):
        entries = [a for a in self.actions.values() if a["name"] == "product_mesh_upload_entry"]
        if len(entries) != 1:
            raise AssertionError(f"expected one upload entry, found {len(entries)}")
        return SimpleNamespace(**entries[0])


class _RealAskProbe(_UiProbe):
    """Exercise the installed Ask/emitter; only the transport is simulated."""
    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.loading = False

    async def record(self, kind, payload):
        await super().record(kind, payload)
        if kind in {"task_start", "task_end"}:
            self.loading = kind == "task_start"
        if kind == "new_message":
            self.messages[payload["id"]] = dict(payload)

    async def call(self, kind, payload, timeout):
        self.events.append(("ask", payload))
        self.ask_spec = payload.get("spec", {})
        return await self.answer(self, payload)

    def __enter__(self):
        from chainlit.context import ChainlitContext, context_var
        from chainlit.emitter import ChainlitEmitter
        from chainlit.chat_context import chat_contexts
        self.context_var, self.chat_contexts = context_var, chat_contexts
        ws = SimpleNamespace(id=self.session.get("id"), thread_id="fixture-thread",
            emit=self.record, emit_call=self.call, files_spec={}, files={},
            has_first_interaction=True, persist_file=AsyncMock(return_value={"id": "fixture-image"}))
        self.emitter = ChainlitEmitter(ws)
        self.token = context_var.set(ChainlitContext(ws, self.emitter))
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(chainlit_ui.cl, "user_session", self.session))
        self.stack.enter_context(patch("chainlit.message.get_data_layer", return_value=None))
        self.stack.enter_context(patch("chainlit.emitter.get_data_layer", return_value=None))
        self._patch_element_persistence()
        return self

    def response(self, *, confirm=True):
        if self.ask_spec.get("type") == "element":
            element = self.elements[self.ask_spec["element_id"]]
            return ({"submitted": True, "payload": {**element["props"]["askPayload"], "confirm": True}}
                    if confirm else {"submitted": False})
        action = next(a for a in self.actions.values()
                      if a["payload"].get("confirm") is confirm)
        return dict(action)


class ContractAskLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_contract_ask_loading_lifecycle(self):
        for mode in ("success", "cancel", "timeout", "outer_timeout", "exception"):
            with self.subTest(mode=mode):
                async def answer(probe, _):
                    if mode == "timeout": raise AskTimeoutError("fixture socketio timeout")
                    if mode == "outer_timeout": raise asyncio.TimeoutError("fixture outer timeout")
                    if mode == "exception": raise RuntimeError("fixture transport error")
                    return probe.response(confirm=mode == "success")
                job = _upload_job()
                service = SimpleNamespace(get_consistent_job=lambda _: job,
                    start_conformal=MagicMock(side_effect=AssertionError("no auto start")))
                with _RealAskProbe(answer) as probe, patch.object(chainlit_ui, "_workflow", return_value=service), patch.object(chainlit_ui, "_ask_for_product_mesh", new=AsyncMock(return_value=None)) as file_ask:
                    await chainlit_ui.send_product_mesh_entry_card(job)
                    await chainlit_ui.product_mesh_upload_entry(probe.current_entry())
                    self.assertIn("task_start", [kind for kind, _ in probe.events])
                    self.assertFalse(probe.loading, f"{mode} left global loading active")
                    if mode == "timeout":
                        self.assertIn("ask_timeout", [kind for kind, _ in probe.events])
                    self.assertIsNone(chainlit_ui._job_ui_state(job["job_id"])["busy"])
                    service.start_conformal.assert_not_called()
                    self.assertEqual(file_ask.await_count, 1 if mode == "success" else 0)

    async def test_same_session_new_lease_waits_for_old_ask_cleanup(self):
        asked, retiring, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def answer(probe, _):
            asked.set()
            try:
                await asyncio.Event().wait()
            finally:
                retiring.set()
                await release.wait()
        with _RealAskProbe(answer) as probe:
            job_id = probe.session.get("product_job_id")
            old = await chainlit_ui._begin_upload_loading({"job_id": job_id})
            token = chainlit_ui._UPLOAD_LOADING_OWNER.set(old)
            task = asyncio.create_task(chainlit_ui._ask_for_mesh_contract({"entry_token": "old"}))
            chainlit_ui._UPLOAD_LOADING_OWNER.reset(token)
            await asyncio.wait_for(asked.wait(), 1)
            new_task = asyncio.create_task(chainlit_ui._begin_upload_loading({"job_id": job_id}))
            await asyncio.wait_for(retiring.wait(), 1)
            self.assertFalse(new_task.done())
            self.assertIs(old["state"]["owner"], old)
            release.set()
            new = await asyncio.wait_for(new_task, 1)
            with self.assertRaises(asyncio.CancelledError): await task
            await probe.emitter.task_start()
            events = len(probe.events)
            await chainlit_ui._finish_upload_loading(old)
            self.assertEqual(len(probe.events), events)
            self.assertTrue(probe.loading)
            self.assertIs(new["state"]["owner"], new)
            await chainlit_ui._finish_upload_loading(new)
            self.assertFalse(probe.loading)

    async def test_stale_session_cannot_end_new_loading(self):
        async def answer(probe, _): return probe.response(confirm=False)
        with _RealAskProbe(answer) as probe:
            old = await chainlit_ui._begin_upload_loading({"job_id": probe.session.get("product_job_id")})
            await chainlit_ui.invalidate_product_waits()
            new = await chainlit_ui._begin_upload_loading({"job_id": probe.session.get("product_job_id")})
            await probe.emitter.task_start()
            await chainlit_ui._finish_upload_loading(old)
            self.assertTrue(probe.loading)
            token = chainlit_ui._UPLOAD_LOADING_OWNER.set(old)
            try:
                with self.assertRaises(ValueError):
                    await chainlit_ui._ask_for_mesh_contract({"entry_token": "stale"})
            finally:
                chainlit_ui._UPLOAD_LOADING_OWNER.reset(token)
            self.assertFalse(any(kind == "ask" for kind, _ in probe.events))
            await chainlit_ui._finish_upload_loading(new)

    async def test_old_upload_cleanup_preserves_active_runtime(self):
        async def answer(probe, _): return probe.response(confirm=False)
        manager = RuntimeExecutionManager()
        release = asyncio.Event()
        with _RealAskProbe(answer) as probe, patch.object(chainlit_ui, "_RUNTIME_MANAGER", manager):
            job_id = probe.session.get("product_job_id")
            owner = await chainlit_ui._begin_upload_loading({"job_id": job_id})
            task, _ = await manager.start_once(job_id, release.wait)
            try:
                await probe.emitter.task_start()
                count = len(probe.events)
                await chainlit_ui._finish_upload_loading(owner)
                self.assertEqual(len(probe.events), count)
                self.assertTrue(probe.loading)
                self.assertTrue(manager.is_active(job_id))
                with self.assertRaises(RuntimeError):
                    await chainlit_ui._begin_upload_loading({"job_id": job_id})
            finally:
                release.set()
                await task

    async def test_ask_cleanup_failure_blocks_handoff(self):
        async def answer(probe, _): return probe.response(confirm=False)
        with _RealAskProbe(answer) as probe:
            owner = await chainlit_ui._begin_upload_loading({"job_id": probe.session.get("product_job_id")})
            probe.fail_event = "task_end"
            with self.assertRaisesRegex(RuntimeError, "fixture transport"):
                await chainlit_ui._finish_upload_loading(owner)
            probe.fail_event = None
            with self.assertRaises(RuntimeError):
                await chainlit_ui._begin_upload_loading({"job_id": probe.session.get("product_job_id")})
            self.assertIs(owner["state"]["owner"], owner)
            self.assertFalse(any(kind == "ask" for kind, _ in probe.events))

    async def test_slow_ask_cancellation_is_bounded_and_blocks_handoff(self):
        asked, retiring, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def answer(probe, _):
            asked.set()
            try: await asyncio.Event().wait()
            finally:
                retiring.set()
                await release.wait()
        with _RealAskProbe(answer) as probe, patch.object(chainlit_ui, "PRODUCT_UI_SEND_TIMEOUT_SECONDS", 0.02):
            job_id = probe.session.get("product_job_id")
            owner = await chainlit_ui._begin_upload_loading({"job_id": job_id})
            token = chainlit_ui._UPLOAD_LOADING_OWNER.set(owner)
            task = asyncio.create_task(chainlit_ui._ask_for_mesh_contract({"entry_token": "old"}))
            chainlit_ui._UPLOAD_LOADING_OWNER.reset(token)
            await asyncio.wait_for(asked.wait(), 1)
            try:
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(chainlit_ui._begin_upload_loading({"job_id": job_id}), 1)
                self.assertTrue(retiring.is_set())
                with self.assertRaises(RuntimeError):
                    await chainlit_ui._begin_upload_loading({"job_id": job_id})
                self.assertIs(owner["state"]["owner"], owner)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_explicit_start_drains_ask_before_runtime_without_cancelling_runtime(self):
        asked, retired = asyncio.Event(), asyncio.Event()
        async def answer(probe, _):
            asked.set()
            try: await asyncio.Event().wait()
            finally: retired.set()
        with _RealAskProbe(answer) as probe, patch.object(chainlit_ui, "_RUNTIME_MANAGER", RuntimeExecutionManager()), patch.object(chainlit_ui, "_send_runtime_status", new=AsyncMock()):
            job_id = probe.session.get("product_job_id")
            owner = await chainlit_ui._begin_upload_loading({"job_id": job_id})
            token = chainlit_ui._UPLOAD_LOADING_OWNER.set(owner)
            task = asyncio.create_task(chainlit_ui._ask_for_mesh_contract({"entry_token": "old"}))
            chainlit_ui._UPLOAD_LOADING_OWNER.reset(token)
            await asyncio.wait_for(asked.wait(), 1)
            def runtime(*args, **kwargs):
                self.assertTrue(retired.is_set())
                self.assertTrue(owner["ask"].done())
                self.assertIsNone(owner["state"]["owner"])
                self.assertEqual(kwargs["authorization"], {"authorized": True, "authorized_by": "chainlit_user_action"})
                return {"job_id": job_id, "revision": 8, "status": "FAILED"}
            run = MagicMock(side_effect=runtime)
            with patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(start_conformal=run)):
                action = SimpleNamespace(id=uuid.uuid4().hex, payload={"job_id": job_id, "expected_revision": 7})
                chainlit_ui._issue_workflow_confirmation(action, "product_start_conformal")
                await chainlit_ui.start_conformal(action)
            await asyncio.gather(task, return_exceptions=True)
            run.assert_called_once()


class ProductChainlitMeshUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # These tests isolate post-lease feedback. UploadLifecycleTests exercises the real gate.
        gate = patch.object(chainlit_ui, "_consume_mesh_lease", new=AsyncMock(return_value=True))
        gate.start()
        self.addCleanup(gate.stop)
        # This class isolates post-confirmation transport; ChatWorkflowTests covers the real confirmation gate.
        for name, replacement in (("_consume_workflow_confirmation", AsyncMock(return_value=True)),
                                  ("_session_generation", MagicMock(return_value="synthetic-session")),
                                  ("_continue_product_chat", AsyncMock())):
            gate = patch.object(chainlit_ui, name, new=replacement)
            gate.start()
            self.addCleanup(gate.stop)
    async def test_binding_card_reads_r3_mesh_binding_v2_through_normalized_view(self):
        service = _BindingService(result=_bound_r3_job())
        with (_UiProbe(), patch.object(chainlit_ui, "_workflow", return_value=service),
              patch.object(chainlit_ui, "build_product_job_projection", return_value={"view_state":"mesh", "capabilities":{"start_conformal":True}}),
              patch.object(chainlit_ui, "_start_bound_mesh_preview"),
              patch.object(chainlit_ui, "_send_job_stage_card", new=AsyncMock()) as card):
            await chainlit_ui._bind_uploaded_mesh(job_id="job-r3-mesh-upload", name="implant-v2.inp",
                content=b"mesh", expected_revision=8, lease={})
        content = card.await_args.args[2]
        mesh = card.await_args.kwargs["evidence"]
        self.assertIn("implant-v2.inp", content)
        self.assertIn("Input size (bytes): 456", mesh)
        self.assertNotIn("stored_path", mesh)
        self.assertIn("Input element: C3D8R", mesh)
        self.assertEqual(card.await_args.kwargs["model"]["fileName"], "implant-v2.inp")
        self.assertNotIn("SHA256", content)
        self.assertNotIn("input/source_c3d8.inp", content)

    async def test_duplicate_conformal_actions_share_one_runtime_task(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def run(job_id, **_kwargs):
            calls.append(job_id)
            started.set()
            release.wait(timeout=2)
            return {
                "job_id": job_id,
                "revision": 10,
                "status": "VIEWER_READY",
                "attempts": [{"package_id": f"{job_id}-attempt-0001"}],
            }

        service = SimpleNamespace(start_conformal=run)
        sent = []
        action = SimpleNamespace(payload={
            "job_id": "job-mesh-upload",
            "expected_revision": 8,
        })
        with (
            patch.object(chainlit_ui, "_workflow", return_value=service),
            patch.object(chainlit_ui, "_RUNTIME_MANAGER", RuntimeExecutionManager()),
            patch.object(chainlit_ui, "_product_config", side_effect=_synthetic_product_config),
            patch.object(chainlit_ui, "_show_readonly_result", new=AsyncMock()),
            patch.object(chainlit_ui.cl, "Message", side_effect=_message_recorder(sent)),
        ):
            first = asyncio.create_task(chainlit_ui.start_conformal(action))
            await asyncio.to_thread(started.wait, 1)
            second = asyncio.create_task(chainlit_ui.start_conformal(action))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

        self.assertEqual(calls, ["job-mesh-upload"])

    async def test_conformal_runtime_starts_when_progress_message_transport_hangs(self):
        class _RuntimeService:
            def __init__(self):
                self.calls = []

            def start_conformal(self, job_id, **kwargs):
                self.calls.append((job_id, kwargs))
                return {
                    "job_id": job_id,
                    "revision": 10,
                    "status": "VIEWER_READY",
                    "attempts": [
                        {
                            "package_id": f"{job_id}-attempt-0001",
                        }
                    ],
                }

        async def blocked_send():
            await asyncio.Event().wait()

        service = _RuntimeService()
        blocked_message = SimpleNamespace(send=blocked_send)
        with (
            patch.object(chainlit_ui, "_workflow", return_value=service),
            patch.object(
                chainlit_ui.cl,
                "Message",
                MagicMock(return_value=blocked_message),
            ),
            patch.object(
                chainlit_ui,
                "PRODUCT_UI_SEND_TIMEOUT_SECONDS",
                0.01,
                create=True,
            ),
            patch.object(chainlit_ui, "_product_config", side_effect=_synthetic_product_config),
            patch.object(chainlit_ui, "_show_readonly_result", new=AsyncMock()),
        ):
            completed = True
            try:
                await asyncio.wait_for(
                    chainlit_ui.start_conformal(
                        SimpleNamespace(
                            payload={
                                "job_id": "job-mesh-upload",
                                "expected_revision": 8,
                            }
                        )
                    ),
                    timeout=0.2,
                )
            except asyncio.TimeoutError:
                completed = False

        self.assertTrue(
            completed,
            "a stalled UI progress message must not stall the conformal callback",
        )
        self.assertEqual(len(service.calls), 1)
        job_id, kwargs = service.calls[0]
        self.assertEqual(job_id, "job-mesh-upload")
        self.assertEqual(kwargs["expected_revision"], 8)
        self.assertEqual(
            kwargs["authorization"],
            {
                "authorized": True,
                "authorized_by": "chainlit_user_action",
            },
        )

    async def test_conformal_runtime_starts_once_when_progress_message_send_fails(self):
        service = SimpleNamespace(
            start_conformal=MagicMock(return_value={
                "job_id": "job-mesh-upload",
                "revision": 10,
                "status": "VIEWER_READY",
                "attempts": [
                    {
                        "package_id": "job-mesh-upload-attempt-0001",
                    }
                ],
            })
        )
        failed_message = SimpleNamespace(
            send=AsyncMock(side_effect=RuntimeError("websocket unavailable"))
        )
        with (
            patch.object(chainlit_ui, "_workflow", return_value=service),
            patch.object(
                chainlit_ui.cl,
                "Message",
                MagicMock(return_value=failed_message),
            ),
            patch.object(chainlit_ui, "_product_config", side_effect=_synthetic_product_config),
            patch.object(chainlit_ui, "_show_readonly_result", new=AsyncMock()),
        ):
            await chainlit_ui.start_conformal(
                SimpleNamespace(
                    payload={
                        "job_id": "job-mesh-upload",
                        "expected_revision": 8,
                    }
                )
            )

        service.start_conformal.assert_called_once_with(
            "job-mesh-upload",
            expected_revision=8,
            authorization={
                "authorized": True,
                "authorized_by": "chainlit_user_action",
            },
        )

    async def test_conformal_completion_uses_readonly_viewer_links_without_generation(self):
        result = {"job_id":"job-mesh-upload", "revision":10, "status":"VIEWER_READY",
                  "attempts":[{"package_id":"job-mesh-upload-attempt-0001"}]}
        service = SimpleNamespace(start_conformal=MagicMock(return_value=result))
        config = _synthetic_product_config()
        config.paths = SimpleNamespace(conformal_root=Path("synthetic-conformal"))
        stored_job = {**result, "selection":{"selected_candidate_id":"candidate-1"},
                      "candidate_set":{"candidates":[{"candidate_id":"candidate-1", "rank":1}]}}
        package = SimpleNamespace(package_id="job-mesh-upload-attempt-0001",
            mesh_view={"original_filename":"implant.inp", "raw_sha256":"A"*64})
        with (_UiProbe() as probe,
              patch.object(chainlit_ui, "_workflow", return_value=service),
              patch.object(chainlit_ui, "_RUNTIME_MANAGER", RuntimeExecutionManager()),
              patch.object(chainlit_ui, "_product_config", return_value=config),
              patch.object(chainlit_ui, "_readonly_viewer_store", return_value=SimpleNamespace(get_consistent_for_viewer=lambda _:stored_job)),
              patch("product_workflow.artifacts.resolve_product_package", return_value=package) as verify,
              patch("product_workflow.artifacts.resolve_product_package_file", return_value=Path("synthetic.stl")) as resolve_file,
              patch.object(chainlit_ui, "_load_result_view", wraps=chainlit_ui._load_result_view) as read_result,
              patch.object(chainlit_ui, "_render_result_picture", new=AsyncMock(return_value=b"picture")) as render):
            probe.session.set("product_job_id", stored_job["job_id"])
            await chainlit_ui.start_conformal(SimpleNamespace(payload={"job_id":"job-mesh-upload", "expected_revision":8}))
            pending = tuple(probe.session.get("product_preview_tasks") or ())
            self.assertTrue(pending)
            await asyncio.wait_for(asyncio.gather(*pending), timeout=2)
            props = probe.card_props("conformal")
            read_result.assert_called_once_with("job-mesh-upload")
            verify.assert_called_once()
            resolve_file.assert_called_once_with(package, "stl")
            render.assert_awaited_once()
        service.start_conformal.assert_called_once_with("job-mesh-upload", expected_revision=8,
            authorization={"authorized":True, "authorized_by":"chainlit_user_action"})
        self.assertEqual(props["viewerUrl"], "http://127.0.0.1:5173/product.html?job_id=job-mesh-upload")
        self.assertEqual(props["files"], [])
        self.assertNotIn("/api/viewer/product/jobs/", str(props))
        self.assertEqual(props["previewState"], "ready")
        self.assertNotIn("localhost:8000/api/conformal", str(props))

    async def test_global_upload_contract_accepts_windows_binary_and_plain_text_inp(self):
        config_path = Path(__file__).resolve().parents[1] / ".chainlit" / "config.toml"
        with config_path.open("rb") as handle:
            config = tomli.load(handle)

        accept = config["features"]["spontaneous_file_upload"]["accept"]
        self.assertEqual(accept["application/octet-stream"], [".inp"])
        self.assertEqual(accept["text/plain"], [".inp"])
        self.assertEqual({ext for extensions in accept.values() for ext in extensions}, {".csv", ".inp"})

    async def test_unsupported_attachment_receives_format_guidance(self):
        with _UiProbe() as probe, patch.object(chainlit_ui, "_workflow") as workflow:
            message = SimpleNamespace(content="", elements=[SimpleNamespace(name="workbook.XLSX", path="unused")])
            await chainlit_ui.handle_product_message(message)
            replies = " ".join(m.get("output", "") for m in probe.messages.values())
            self.assertIn("CSV", replies)
            self.assertIn("INP", replies)
            workflow.assert_not_called()

    async def test_binding_uses_single_mesh_contract_and_offers_only_start_action(self):
        service = _BindingService(result=_bound_job())
        with (_UiProbe() as probe, patch.object(chainlit_ui, "_workflow", return_value=service),
              patch.object(chainlit_ui, "build_product_job_projection", return_value={"view_state":"mesh", "capabilities":{"start_conformal":True}}),
              patch.object(chainlit_ui, "_start_bound_mesh_preview")):
            probe.session.set("product_job_id", "job-mesh-upload")
            await chainlit_ui._bind_uploaded_mesh(job_id="job-mesh-upload", name="implant.inp",
                content=b"*Heading\n", expected_revision=7, lease={})
            actions = probe.stage_actions("mesh", "job-mesh-upload")
            self.assertEqual([action.name for action in actions], ["product_start_conformal", "product_refresh_job"])
            self.assertEqual(actions[0].payload["expected_revision"], 8)
            self.assertEqual(probe.card_props("mesh", "job-mesh-upload")["quality"], "passed")
        self.assertEqual(len(service.calls), 1)
        job_id, kwargs = service.calls[0]
        self.assertEqual(job_id, "job-mesh-upload")
        self.assertEqual(kwargs["confirmed_contract"], {**PRODUCT_MESH_CONTRACT, "design_domain":"selected_hex_domain"})
        self.assertEqual(kwargs["expected_revision"], 7)
        self.assertNotIn("product_confirm_mesh", [action.name for action in actions])

    async def test_precheck_rejection_offers_reupload_but_not_start(self):
        service = _BindingService(result=_bound_job(status="CANDIDATES_READY"))
        with _UiProbe() as probe, patch.object(chainlit_ui, "_workflow", return_value=service):
            await chainlit_ui._bind_uploaded_mesh(job_id="job-mesh-upload", name="implant.inp",
                content=b"*Heading\n", expected_revision=7, lease={})
            actions = probe.stage_actions("mesh_entry")
            self.assertEqual([action.name for action in actions], ["product_mesh_upload_entry"])
            self.assertEqual(actions[0].payload["expected_revision"], 8)
            self.assertTrue(any("static checks did not pass" in message["output"] for message in probe.messages.values()))
        self.assertEqual(len(service.calls), 1)

    async def test_stale_revision_is_displayed_without_retry(self):
        self.assertTrue(
            hasattr(chainlit_ui, "_bind_uploaded_mesh"),
            "shared mesh-binding helper is missing",
        )
        sent = []
        service = _BindingService(error=ValueError("revision mismatch"))
        with (
            patch.object(chainlit_ui, "_workflow", return_value=service),
            patch.object(
                chainlit_ui.cl,
                "Message",
                side_effect=_message_recorder(sent),
            ),
        ):
            await chainlit_ui._bind_uploaded_mesh(
                job_id="job-mesh-upload",
                name="implant.inp",
                content=b"*Heading\n",
                expected_revision=7, lease={},
            )

        self.assertEqual(len(service.calls), 1)
        self.assertIn("[Error]", sent[-1].content)
        self.assertIn("Stage: INP binding and static checks", sent[-1].content)
        self.assertIn("Saved state: Check the latest Job state", sent[-1].content)
        self.assertIn("Next step: No automatic retry", sent[-1].content)
        self.assertNotIn("revision mismatch", sent[-1].content)
        self.assertNotIn("revision mismatch", sent[-1].kwargs["elements"][0].content)
        self.assertIn("UNCLASSIFIED", sent[-1].kwargs["elements"][0].content)

    async def test_binding_error_hides_internal_paths_and_traceback(self):
        sent = []
        service = _BindingService(
            error=RuntimeError(
                "Traceback: stored_path=I:\\private\\job base_url=http://secret"
            )
        )
        with (
            patch.object(chainlit_ui, "_workflow", return_value=service),
            patch.object(
                chainlit_ui.cl,
                "Message",
                side_effect=_message_recorder(sent),
            ),
        ):
            await chainlit_ui._bind_uploaded_mesh(
                job_id="job-mesh-upload",
                name="implant.inp",
                content=b"*Heading\n",
                expected_revision=7, lease={},
            )

        message = sent[-1].content
        self.assertIn("[Error]", message)
        self.assertIn("Stage: INP binding and static checks", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("stored_path", message)
        self.assertNotIn("base_url", message)
        self.assertNotIn("I:\\", message)
        evidence = "\n".join(element.content for element in sent[-1].kwargs["elements"])
        self.assertIn("UNCLASSIFIED", evidence)
        for private_detail in ("Traceback", "stored_path", "base_url", "I:\\"):
            self.assertNotIn(private_detail, evidence)

    async def test_generic_inp_attachment_without_lease_never_reads_or_binds(self):
        with _UiProbe(), patch.object(Path, "read_bytes", side_effect=AssertionError("no file read")), patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind:
            self.assertTrue(await chainlit_ui._handle_attachment(SimpleNamespace(name="implant.inp", path="unread.inp")))
        bind.assert_not_awaited()


class UploadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_contract_cancel_timeout_and_disconnect_never_open_file_picker(self):
        for mode in ("cancel", "timeout", "disconnect"):
            job = _upload_job()
            with _UiProbe() as probe, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "_ask_for_product_mesh", new=AsyncMock()) as file_ask:
                async def contract(lease):
                    self.assertFalse(chainlit_ui._job_ui_state(job["job_id"])["lock"].locked())
                    if mode == "timeout":
                        raise asyncio.TimeoutError()
                    if mode == "disconnect":
                        await chainlit_ui.invalidate_product_waits()
                    return {"payload": {"entry_token": lease["entry_token"], "confirm": mode != "cancel"}}
                with patch.object(chainlit_ui, "_ask_for_mesh_contract", side_effect=contract):
                    await chainlit_ui.send_product_mesh_entry_card(job)
                    entry = probe.current_entry()
                    await chainlit_ui.product_mesh_upload_entry(entry)
                    await chainlit_ui.product_mesh_upload_entry(entry)
                self.assertIsNone(chainlit_ui._job_ui_state(job["job_id"])["busy"])
                file_ask.assert_not_awaited()

    async def test_valid_entry_preserves_upload_contract_and_original_revision(self):
        job = _upload_job()
        source = _temporary_upload_path("upload-contract")
        source.write_bytes(b"*Heading\n")
        self.addCleanup(source.unlink, missing_ok=True)
        ask = AsyncMock(return_value=[SimpleNamespace(name="implant.inp", path=str(source))])
        bind = AsyncMock()
        with _UiProbe() as ui, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui.cl, "AskFileMessage", return_value=SimpleNamespace(send=ask)) as ask_file, patch.object(chainlit_ui, "_end_ask_file_loading_state", new=AsyncMock()) as end, patch.object(chainlit_ui, "_bind_uploaded_mesh", bind):
            await chainlit_ui.send_product_mesh_entry_card(job)
            await chainlit_ui.product_mesh_upload_entry(ui.current_entry())
        ask_file.assert_called_once_with(content=ANY, accept={"application/octet-stream": [".inp"], "text/plain": [".inp"]}, max_size_mb=50, max_files=1, timeout=600, raise_on_timeout=True)
        self.assertEqual(ask_file.call_args.kwargs["content"].split("\n\n")[0], (
            "Upload one original Abaqus INP file, up to 50 MB. "
            "Confirm that coordinates are in mm and use the original coordinate system, with no rotation or mirroring. "
            "Review the C3D8/C3D8R domain and excluded regions after upload."
        ))
        end.assert_not_awaited()  # The upload owner now closes loading after binding.
        self.assertEqual(sum(k == "task_end" for k, _ in ui.events), 1)
        bind.assert_awaited_once_with(job_id=job["job_id"], name="implant.inp", content=b"*Heading\n", expected_revision=7, lease=ANY)

    async def test_missing_stale_malformed_or_foreign_entry_never_asks_or_binds(self):
        job = _upload_job()
        with _UiProbe() as ui, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "_ask_for_product_mesh", new=AsyncMock()) as ask, patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind:
            await chainlit_ui.send_product_mesh_entry_card(job)
            current = ui.current_entry()
            for payload in ({}, {"job_id": job["job_id"]}, {"job_id": job["job_id"], "expected_revision": 6}, {"job_id": job["job_id"], "expected_revision": "7"}, {"job_id": job["job_id"], "expected_revision": True}, {"job_id": "job-short", "expected_revision": 7}):
                await chainlit_ui.product_mesh_upload_entry(SimpleNamespace(id=current.id, payload=payload))
            await chainlit_ui.product_mesh_upload_entry(SimpleNamespace(id="foreign", payload=current.payload))
            await chainlit_ui.upload_mesh(SimpleNamespace(id="legacy", payload=current.payload))
            job["status"] = "AWAITING_CONFORMAL_AUTHORIZATION"
            await chainlit_ui.product_mesh_upload_entry(current)
            job["status"] = "CANDIDATES_READY"
            job["selection"] = None
            await chainlit_ui.product_mesh_upload_entry(current)
            self.assertTrue(any("This upload entry expired because the Job state changed. Use the latest upload entry." in m["output"] for m in ui.messages.values()))
        ask.assert_not_awaited()
        bind.assert_not_awaited()

    async def test_double_click_is_rejected_through_bind_and_card_update(self):
        job = _upload_job()
        source = _temporary_upload_path("upload-busy")
        source.write_bytes(b"mesh")
        self.addCleanup(source.unlink, missing_ok=True)
        asked = asyncio.Event()
        allow_ask = asyncio.Event()
        binding = asyncio.Event()
        allow_bind = asyncio.Event()
        async def ask():
            asked.set()
            await allow_ask.wait()
            return [SimpleNamespace(name="implant.inp", path=str(source))]
        async def bind(**kwargs):
            binding.set()
            await allow_bind.wait()
            await chainlit_ui.send_product_mesh_entry_card(job)
        with _UiProbe() as ui, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "_ask_for_product_mesh", new=AsyncMock(side_effect=ask)) as asker, patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock(side_effect=bind)) as binder:
            await chainlit_ui.send_product_mesh_entry_card(job)
            entry = ui.current_entry()
            first = asyncio.create_task(chainlit_ui.product_mesh_upload_entry(entry))
            await asked.wait()
            await asyncio.wait_for(chainlit_ui.product_mesh_upload_entry(entry), timeout=0.3)
            allow_ask.set()
            await binding.wait()
            await asyncio.wait_for(chainlit_ui.product_mesh_upload_entry(entry), timeout=0.3)
            ui.block_event = "update_step"
            allow_bind.set()
            await ui.entered.wait()
            await asyncio.wait_for(chainlit_ui.product_mesh_upload_entry(entry), timeout=0.3)
            ui.release.set()
            await first
            self.assertEqual(asker.await_count, 1)
            self.assertEqual(binder.await_count, 1)
            self.assertTrue(any("An upload is already in progress. Finish the current operation first." in m["output"] for m in ui.messages.values()))

    async def test_revision_or_entry_generation_change_while_selecting_rejects_before_file_read(self):
        for change_revision in (True, False):
            with self.subTest(change_revision=change_revision), _UiProbe() as ui:
                job = _upload_job()
                async def ask():
                    if change_revision:
                        job["revision"] = 8
                    await chainlit_ui.send_product_mesh_entry_card(job)
                    return [SimpleNamespace(name="stale.inp", path="must-not-be-read.inp")]
                with patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "_ask_for_product_mesh", side_effect=ask), patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind, patch.object(Path, "read_bytes", side_effect=AssertionError("stale files must not be read")) as read:
                    await chainlit_ui.send_product_mesh_entry_card(job)
                    await chainlit_ui.product_mesh_upload_entry(ui.current_entry())
                bind.assert_not_awaited()
                read.assert_not_called()

    async def test_cancel_timeout_and_task_cancel_release_busy_and_keep_one_entry(self):
        for outcome in (None, asyncio.TimeoutError(), asyncio.CancelledError()):
            with self.subTest(outcome=type(outcome).__name__), _UiProbe() as ui:
                job = _upload_job()
                ask = AsyncMock(return_value=outcome) if outcome is None else AsyncMock(side_effect=outcome)
                with patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui.cl, "AskFileMessage", return_value=SimpleNamespace(send=ask)), patch.object(chainlit_ui, "_end_ask_file_loading_state", new=AsyncMock()) as end, patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind:
                    await chainlit_ui.send_product_mesh_entry_card(job)
                    before = ui.current_entry()
                    if isinstance(outcome, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            await chainlit_ui.product_mesh_upload_entry(before)
                        await chainlit_ui.send_product_mesh_entry_card(job)
                    else:
                        await chainlit_ui.product_mesh_upload_entry(before)
                    self.assertIsNone(chainlit_ui._job_ui_state(job["job_id"])["busy"])
                    await chainlit_ui.send_product_mesh_entry_card(job)
                    self.assertNotEqual(before.id, ui.current_entry().id)
                    ask.side_effect = None
                    ask.return_value = None
                    await chainlit_ui.product_mesh_upload_entry(ui.current_entry())
                    self.assertEqual(ask.await_count, 2)
                    self.assertEqual(end.await_count, 0)
                    self.assertEqual(sum(k == "task_end" for k, _ in ui.events), 2)
                bind.assert_not_awaited()

    async def test_precheck_rejection_reuses_mesh_entry_with_fresh_revision(self):
        job = _upload_job()
        result = {**job, "revision": 8, "mesh_uploads": [{"precheck": {"errors": ["fixture rejection"]}}]}
        current = [job]
        def bind_mesh(*args, **kwargs):
            current[0] = result
            return result
        service = SimpleNamespace(get_consistent_job=lambda _: current[0], bind_mesh=bind_mesh)
        source = _temporary_upload_path("reject")
        source.write_bytes(b"fixture")
        self.addCleanup(source.unlink, missing_ok=True)
        with _UiProbe() as ui, patch.object(chainlit_ui, "_workflow", return_value=service), patch.object(chainlit_ui, "_ask_for_product_mesh", new=AsyncMock(return_value=[SimpleNamespace(name="bad.inp", path=str(source))])):
            await chainlit_ui.send_product_mesh_entry_card(job)
            original = ui.current_entry()
            await chainlit_ui.product_mesh_upload_entry(original)
            new = ui.current_entry()
            self.assertNotEqual(original.id, new.id)
            self.assertEqual(new.payload["expected_revision"], 8)
            self.assertEqual(new.forId, original.forId)
            self.assertTrue(any("static checks did not pass" in value["output"] for value in ui.messages.values()))
            self.assertTrue(any("Input precheck" in value.get("content", "") for value in ui.elements.values()))
            self.assertFalse(any("fixture rejection" in value.get("content", "") for value in ui.elements.values()))

    async def test_old_flow_finally_does_not_release_new_sessions_busy_claim(self):
        job = _upload_job()
        old_asked, new_asked = asyncio.Event(), asyncio.Event()
        finish_old, finish_new = asyncio.Event(), asyncio.Event()
        calls = []
        async def ask():
            calls.append(True)
            if len(calls) == 1:
                old_asked.set()
                await finish_old.wait()
            else:
                new_asked.set()
                await finish_new.wait()
            return None
        with _UiProbe() as ui, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "_ask_for_product_mesh", side_effect=ask):
            await chainlit_ui.send_product_mesh_entry_card(job)
            old_flow = asyncio.create_task(chainlit_ui.product_mesh_upload_entry(ui.current_entry()))
            await old_asked.wait()
            with patch.object(chainlit_ui.cl, "user_session", _Session({"id": "new-session", "product_job_id": job["job_id"]})) as session:
                await chainlit_ui.send_product_mesh_entry_card(job)
                entry = session.get(f"product_job_ui:{job['job_id']}")["cards"]["mesh_entry"]["actions"][0]
                new_flow = asyncio.create_task(chainlit_ui.product_mesh_upload_entry(entry))
                await new_asked.wait()
                finish_old.set()
                await old_flow
                await asyncio.wait_for(chainlit_ui.product_mesh_upload_entry(entry), timeout=0.3)
                self.assertEqual(len(calls), 2)
                finish_new.set()
                await new_flow


if __name__ == "__main__":
    unittest.main()

class UploadWaitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_contract_handoff_allows_599_seconds_at_each_stage(self):
        job = _upload_job()
        clock = [1000.0]
        deadlines, waits = [], []
        real_wait_for = asyncio.wait_for

        async def wait_for(awaitable, timeout):
            waits.append(timeout)
            return await real_wait_for(awaitable, timeout=timeout)

        async def answer(probe, payload):
            lease = chainlit_ui._job_ui_state(job["job_id"])["busy"]
            deadlines.append(lease["expires_at"] - clock[0])
            clock[0] += 599
            return probe.response() if payload["spec"]["type"] == "action" else None

        with _RealAskProbe(answer) as probe, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui.time, "monotonic", side_effect=lambda: clock[0]), patch.object(chainlit_ui.asyncio, "wait_for", side_effect=wait_for), patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind:
            await chainlit_ui.send_product_mesh_entry_card(job)
            await chainlit_ui.product_mesh_upload_entry(probe.current_entry())
            asks = [payload for kind, payload in probe.events if kind == "ask"]
            self.assertEqual([(a["spec"]["type"], a["spec"]["timeout"]) for a in asks], [("action", 600), ("file", 600)])
            self.assertEqual(deadlines, [600, 600])
            self.assertEqual(waits.count(600), 2)
            self.assertTrue(all("10 minutes" in a["msg"]["output"] for a in asks))
            self.assertFalse(probe.loading)
            self.assertIsNone(chainlit_ui._job_ui_state(job["job_id"])["busy"])
            bind.assert_not_awaited()

    async def test_contract_and_file_timeouts_remove_old_buttons_and_offer_guarded_refresh(self):
        for stage, error in (("action", AskTimeoutError), ("action", asyncio.TimeoutError), ("file", AskTimeoutError), ("file", asyncio.TimeoutError)):
            with self.subTest(stage=stage, error=error.__name__):
                job = _upload_job()

                async def answer(probe, payload):
                    if payload["spec"]["type"] == stage:
                        raise error("fixture wait expired")
                    return probe.response()

                with _RealAskProbe(answer) as probe, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind:
                    await chainlit_ui.send_product_mesh_entry_card(job)
                    entry = probe.current_entry()
                    await chainlit_ui.product_mesh_upload_entry(entry)
                    recovery = [a for a in probe.actions.values() if a["name"] == "product_refresh_job"]
                    self.assertEqual(len(recovery), 1)
                    self.assertEqual(recovery[0]["payload"], {"job_id": job["job_id"], "expected_revision": 7, "session_generation": entry.payload["session_generation"]})
                    self.assertFalse(any(a["name"] in {"product_mesh_contract_confirm", "product_mesh_contract_cancel", "product_mesh_upload_entry"} for a in probe.actions.values()))
                    text = "\n".join(m["output"] for m in probe.messages.values())
                    self.assertIn("timed out", text)
                    self.assertIn("10 minutes", text)
                    self.assertNotIn(chainlit_ui._UPLOAD_STALE, text)
                    self.assertFalse(probe.loading)
                    self.assertIsNone(chainlit_ui._job_ui_state(job["job_id"])["busy"])
                    bind.assert_not_awaited()

    async def test_session_or_revision_change_during_contract_never_opens_file_picker(self):
        for change in ("session", "revision"):
            with self.subTest(change=change):
                job = _upload_job()

                async def answer(probe, payload):
                    if payload["spec"]["type"] == "action":
                        if change == "session":
                            probe.session.set("product_session_generation", "replacement-session")
                        else:
                            job["revision"] = 8
                        return probe.response()
                    return None

                with _RealAskProbe(answer) as probe, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind:
                    await chainlit_ui.send_product_mesh_entry_card(job)
                    await chainlit_ui.product_mesh_upload_entry(probe.current_entry())
                    self.assertEqual([p["spec"]["type"] for kind, p in probe.events if kind == "ask"], ["action"])
                    self.assertTrue(any(chainlit_ui._UPLOAD_STALE in m["output"] for m in probe.messages.values()))
                    self.assertFalse(any(a["name"] == "product_refresh_job" for a in probe.actions.values()))
                    self.assertTrue(any(f"/resume {job['job_id']}" in m["output"] for m in probe.messages.values()))
                    self.assertFalse(probe.loading)
                    bind.assert_not_awaited()

    async def test_recovery_button_revalidates_job_revision_and_session_before_resume(self):
        job = _upload_job()
        with _UiProbe() as probe, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "resume_product_job", new=AsyncMock(return_value=True)) as resume:
            generation = chainlit_ui._session_generation()
            payload = {"job_id": job["job_id"], "expected_revision": 7, "session_generation": generation}
            for change in ({"expected_revision": 6}, {"session_generation": "old"}, {"job_id": "job-" + "B" * 24}):
                await chainlit_ui.product_refresh_job(SimpleNamespace(payload={**payload, **change}))
            resume.assert_not_awaited()
            await chainlit_ui.product_refresh_job(SimpleNamespace(payload=payload))
            resume.assert_awaited_once_with(job["job_id"])

    async def test_done_ask_cleanup_failure_blocks_new_upload_and_preserves_failure(self):
        for failed_event in ("remove_action", "clear_ask"):
            with self.subTest(failed_event=failed_event):
                job = _upload_job()
                async def answer(probe, _):
                    return probe.response()
                with _RealAskProbe(answer) as probe, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind:
                    await chainlit_ui.send_product_mesh_entry_card(job)
                    probe.fail_event = failed_event
                    with self.assertRaises(RuntimeError):
                        await chainlit_ui.product_mesh_upload_entry(probe.current_entry())
                    loading = chainlit_ui._upload_loading_state()
                    self.assertIsNotNone(loading["failure"])
                    self.assertIsNotNone(loading["owner"])
                    self.assertTrue(loading["owner"]["ask"].done())
                    with self.assertRaisesRegex(RuntimeError, "Upload dialog cleanup failed"):
                        await chainlit_ui._begin_upload_loading({"job_id": job["job_id"]})
                    self.assertEqual([p["spec"]["type"] for kind, p in probe.events if kind == "ask"], ["action"])
                    bind.assert_not_awaited()
