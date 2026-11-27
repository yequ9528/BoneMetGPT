"""Selected-domain contract tests: synthetic fixtures, no production providers."""
import unittest
import io
import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch, AsyncMock
from socketio.exceptions import TimeoutError as AskTimeoutError

from product_workflow.contracts import PRODUCT_MESH_CONTRACT
from product_workflow.mesh_domain import extract_hex_domain, parse_original_mesh, render_domain_preview
from product_workflow.mesh_identity import parse_mesh_identity_v1
from product_workflow.service import normalize_job_mesh_binding
from tests import test_product_workflow_mesh_runtime as fixtures

VALID_INP = fixtures.VALID_INP

MIXED = VALID_INP.replace(b"1,1,2,3,4,5,6,7,8", b"101,1,2,3,4,5,6,7,8") + b"*Element, type=S4R\n201,1,2,3,4\n*Element, type=C3D6\n301,1,2,3,5,6,7\n"
DUAL_HEX = VALID_INP + b"*Element, type=C3D8R\n102,1,2,3,4,5,6,7,8\n"


class ExtractionTests(unittest.TestCase):
    def test_exact_selected_rows_and_excluded_domain(self):
        derived, domain = extract_hex_domain(MIXED)
        before, after = parse_original_mesh(MIXED), parse_original_mesh(derived)
        self.assertEqual(after["nodes"], before["nodes"])
        self.assertEqual(after["elements"], {101: before["elements"][101]})
        self.assertEqual(after["element_rows"][101], before["element_rows"][101])
        self.assertEqual(domain["selected_element_ids"], [101])
        self.assertEqual(domain["excluded_types"]["S4R"]["element_ids"], [201])
        self.assertEqual(domain["excluded_solid_count"], 1)
        self.assertNotEqual(domain["original_sha256"], domain["derived_sha256"])

    def test_no_hex_bad_reference_duplicate_and_transform_reject(self):
        for raw in (MIXED.replace(b"C3D8", b"C3D20"), MIXED.replace(b"301,1,2,3,5,6,7", b"301,1,2,3,5,6,999"),
                    MIXED+b"*Node\n1,0,0,0\n", MIXED+b"*SYSTEM\n0,0,0\n",
                    MIXED.replace(b"*Node", b"*Node, SYSTEM = C"), MIXED+b"*ELGEN\n1,3,1,1\n"):
            with self.subTest(raw=raw[-40:]), self.assertRaises(ValueError):
                extract_hex_domain(raw)

    def test_dual_hex_keeps_types_and_legacy_parser_rejects(self):
        derived, domain = extract_hex_domain(DUAL_HEX)
        self.assertEqual(domain["selected_types"], {"C3D8": 1, "C3D8R": 1})
        with self.assertRaises(ValueError): parse_mesh_identity_v1(derived)
        self.assertEqual(parse_mesh_identity_v1(derived, allow_mixed_hex=True)["semantics"]["element_type_original"], "C3D8+C3D8R")

    def test_preview_is_png(self):
        self.assertTrue(render_domain_preview(MIXED).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_individual_views_keep_native_coordinates_and_distinct_domains(self):
        from product_workflow import mesh_domain
        from PIL import Image
        self.assertTrue(hasattr(mesh_domain, "render_mesh_views"))
        source = parse_original_mesh(MIXED)
        result = mesh_domain.render_mesh_views(MIXED)
        self.assertEqual(set(result), {"original", "selected", "excluded"})
        self.assertEqual(parse_original_mesh(MIXED), source)
        self.assertNotEqual(result["selected"], result["excluded"])
        for kind, png in result.items():
            with Image.open(io.BytesIO(png)) as picture:
                self.assertEqual(picture.size, (800, 600))
                rgb = picture.convert("RGB").tobytes()
                pixels = list(zip(rgb[::3], rgb[1::3], rgb[2::3]))
                if kind == "selected":
                    self.assertGreater(sum(b > r + 50 and b > g + 10 for r, g, b in pixels), 100)
                if kind == "excluded":
                    self.assertGreater(sum(r > g + 60 and r > b + 60 for r, g, b in pixels), 100)


class DomainWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProductWorkflowMeshRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service, self.ready, self.adapter_calls, self.conformal_calls = self.fixture._r3_service_and_ready_job()

    def propose(self, raw=MIXED):
        return self.service.bind_mesh(self.ready["job_id"], expected_revision=self.ready["revision"],
            original_filename="raw-bone.inp", content=raw,
            confirmed_contract={**PRODUCT_MESH_CONTRACT, "design_domain": "selected_hex_domain"})

    def confirm(self, job, confirmed=True, **changes):
        upload=job["mesh_uploads"][-1]
        args=dict(expected_revision=job["revision"], upload_id=upload["upload_id"],
            proposal_sha256=upload["domain_proposal"]["proposal_sha256"], session_generation="fixture-session", confirmed=confirmed)
        args.update(changes)
        return self.service.confirm_mesh_domain(job["job_id"], **args)

    def assert_no_compute(self, job):
        self.assertEqual(job["attempts"], [])
        self.assertEqual(self.adapter_calls, [])
        self.assertEqual(self.conformal_calls, [])

    def test_proposal_defers_quality_and_cancel_creates_no_derivative(self):
        with patch("product_workflow.service.precheck_inp_mesh", side_effect=AssertionError("early quality")):
            proposed=self.propose()
            self.assertIsNone(proposed["mesh_binding"])
            cancelled=self.confirm(proposed, False)
        self.assertEqual(cancelled["mesh_uploads"][-1]["status"], "DOMAIN_CANCELLED")
        path=Path(cancelled["mesh_uploads"][-1]["stored_path"])
        self.assertEqual(path.read_bytes(), MIXED)
        self.assertFalse((path.parent/"selected_hex.inp").exists())
        self.assert_no_compute(cancelled)

    def test_confirmation_binds_derivative_and_requires_separate_runtime_authority(self):
        proposed=self.propose()
        bound=self.confirm(proposed)
        self.assertEqual(bound["status"], "AWAITING_CONFORMAL_AUTHORIZATION")
        mesh=normalize_job_mesh_binding(bound, verify_stored_file=True)
        self.assertEqual(Path(mesh["stored_path"]).name, "selected_hex.inp")
        self.assertEqual(mesh["design_domain"]["selected_element_ids"], [101])
        self.assertEqual(Path(mesh["stored_path"]).parent.joinpath("source.inp").read_bytes(), MIXED)
        with self.assertRaises(ValueError):
            self.service.start_conformal(bound["job_id"], expected_revision=bound["revision"], authorization={"authorized":False})
        self.assert_no_compute(bound)
        with self.assertRaises(ValueError): self.confirm(proposed)

    def test_condition_below_twenty_binds_with_valid_runtime_reference_length(self):
        raw = MIXED
        for row in (b"5,0,0,1", b"6,1,0,1", b"7,1,1,1", b"8,0,1,1"):
            raw = raw.replace(row, row[:-1] + b"14.4368")
        bound = self.confirm(self.propose(raw))
        quality = bound["mesh_uploads"][-1]["domain_precheck"]["geometry_quality"]
        self.assertEqual(quality["status"], "PASS")
        self.assertGreater(quality["maximum_jacobian_condition_2"], 10.0)
        self.assertLess(quality["maximum_jacobian_condition_2"], 20.0)
        mesh = normalize_job_mesh_binding(bound, verify_stored_file=True)
        self.assertGreater(mesh["reference_length_mm"], 0.0)
        self.assertEqual(bound["status"], "AWAITING_CONFORMAL_AUTHORIZATION")
        self.assert_no_compute(bound)

    def test_stale_and_non_boolean_confirmation_do_not_write_derivative(self):
        job=self.propose()
        for changes in ({"expected_revision":job["revision"]-1}, {"proposal_sha256":"0"*64}, {"confirmed":1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.confirm(job, **changes)
        self.assertFalse(Path(job["mesh_uploads"][-1]["stored_path"]).with_name("selected_hex.inp").exists())
        self.assert_no_compute(self.service.get_consistent_job(job["job_id"]))

    def test_inverted_quality_does_not_bind(self):
        raw=MIXED.replace(b"101,1,2,3,4,5,6,7,8", b"101,2,1,4,3,6,5,8,7")
        job=self.confirm(self.propose(raw))
        self.assertEqual(job["mesh_uploads"][-1]["status"], "DOMAIN_REJECTED")
        self.assertIsNone(job["mesh_binding"])
        self.assert_no_compute(job)

    def test_degenerate_quality_does_not_bind(self):
        raw=MIXED.replace(b"5,0,0,1", b"5,0,0,0").replace(b"6,1,0,1", b"6,1,0,0").replace(b"7,1,1,1", b"7,1,1,0").replace(b"8,0,1,1", b"8,0,1,0")
        job=self.confirm(self.propose(raw))
        self.assertEqual(job["mesh_uploads"][-1]["status"], "DOMAIN_REJECTED")
        self.assertIsNone(job["mesh_binding"])
        self.assertTrue(job["mesh_uploads"][-1]["domain_precheck"]["non_finite_metrics"])
        self.assertEqual(self.service.get_consistent_job(job["job_id"])["status"], "CANDIDATES_READY")
        self.assert_no_compute(job)

    def test_upload_without_hex_is_retained_with_actionable_error(self):
        job=self.propose(VALID_INP.replace(b"C3D8", b"S8R"))
        self.assertEqual(job["mesh_uploads"][-1]["status"], "REJECTED")
        self.assertIn("没有可计算", str(job["mesh_uploads"][-1]["precheck"]["errors"]))
        self.assert_no_compute(self.service.get_consistent_job(job["job_id"]))

    def test_quality_exception_preserves_original_error_and_no_retry(self):
        proposed=self.propose()
        with patch("product_workflow.service.precheck_inp_mesh", side_effect=RuntimeError("fixture diagnostic")) as checker:
            rejected=self.confirm(proposed)
        checker.assert_called_once()
        self.assertEqual(rejected["mesh_uploads"][-1]["domain_precheck"]["errors"], ["RuntimeError: fixture diagnostic"])
        self.assertIsNone(rejected["mesh_binding"])
        with self.assertRaises(ValueError): self.confirm(rejected)

    def test_original_change_before_confirmation_is_rejected(self):
        job=self.propose()
        with Path(job["mesh_uploads"][-1]["stored_path"]).open("ab") as out: out.write(b"** changed\n")
        with self.assertRaisesRegex(ValueError, "changed"): self.confirm(job)

    def test_binding_and_derivative_tamper_are_rejected(self):
        job=self.confirm(self.propose())
        altered=deepcopy(job)
        altered["mesh_binding"]["design_domain"]["excluded_solid_count"]=0
        with self.assertRaises(ValueError): normalize_job_mesh_binding(altered, verify_stored_file=True)
        path=Path(normalize_job_mesh_binding(job)["stored_path"])
        with path.open("ab") as out: out.write(b"** changed\n")
        with self.assertRaises(ValueError): normalize_job_mesh_binding(job, verify_stored_file=True)

    def test_dual_hex_opt_in_and_runtime_identity(self):
        from conformal_service import _validate_product_mesh_binding, _select_runtime_hex_elements
        from product_workflow.service import _runtime_source_mesh_contract
        import torch
        job=self.confirm(self.propose(DUAL_HEX))
        mesh=normalize_job_mesh_binding(job, verify_stored_file=True)
        self.assertEqual(mesh["element_type_original"], "C3D8+C3D8R")
        self.assertEqual(_validate_product_mesh_binding(job["mesh_binding"], source_mesh_path=Path(mesh["stored_path"])), job["mesh_binding"])
        sets={"C3D8":torch.tensor([[1,2,3,4,5,6,7,8]]), "C3D8R":torch.tensor([[8,7,6,5,4,3,2,1]])}
        with self.assertRaises(ValueError): _select_runtime_hex_elements(sets)
        actual=_select_runtime_hex_elements(sets, source_mesh_contract=_runtime_source_mesh_contract(mesh))
        self.assertEqual(actual[2].tolist(), [[1,2,3,4,5,6,7,8],[8,7,6,5,4,3,2,1]])
        self.assert_no_compute(job)

    def test_viewer_carries_selected_scope_and_original_identity(self):
        from product_viewer_payloads import _build_mesh, _build_non_claims
        job=self.confirm(self.propose())
        called=[]
        payload=_build_mesh(job, lambda p: called.append(p) or {"status":"ok"}, self.service.store)
        self.assertTrue(called[0].endswith("selected_hex.inp"))
        self.assertEqual(payload["original_upload"]["original_filename"], "raw-bone.inp")
        self.assertEqual(payload["design_domain"]["excluded_solid_count"], 1)
        self.assertIn("NOT_FULL_BONE_COVERAGE", _build_non_claims(job))

    def test_explicit_runtime_uses_derivative_and_one_fixture_attempt(self):
        bound=self.confirm(self.propose())
        final=self.service.start_conformal(bound["job_id"], expected_revision=bound["revision"], authorization={"authorized":True})
        self.assertEqual(final["status"], "VIEWER_READY")
        self.assertEqual(len(self.adapter_calls), 1)
        self.assertEqual(len(self.conformal_calls), 1)
        self.assertTrue(self.conformal_calls[0]["source_mesh_path"].endswith("selected_hex.inp"))
        self.assertEqual(self.conformal_calls[0]["mesh_binding"]["design_domain"]["excluded_solid_count"], 1)
        with self.assertRaises(ValueError):
            self.service.start_conformal(final["job_id"], expected_revision=final["revision"], authorization={"authorized":True})


class DomainUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case=DomainWorkflowTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    async def exercise(self, mode):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _RealAskProbe
        job=self.case.propose()
        seen=[]
        clock = SimpleNamespace(monotonic=lambda: now[0])
        now = [100.0]
        async def answer(probe, wire):
            self.assertEqual(wire["spec"]["type"], "element")
            seen.append(deepcopy(probe.elements[wire["spec"]["element_id"]]["props"]))
            self.assertEqual(state["busy"]["stage"], "AWAIT_DOMAIN")
            if mode=="timeout": raise asyncio.TimeoutError()
            if mode=="cancel": return {"submitted":False}
            response=probe.response()
            if mode in {"slow_cleanup", "late_answer"}:
                now[0] = lease["expires_at"] + (0.1 if mode == "late_answer" else -0.2)
            if mode=="foreign": response["payload"]["entry_token"]="another"
            if mode=="nonboolean": response["payload"]["confirm"]=1
            if mode=="stale_session": lease["session_generation"]="old"
            if mode=="stale_revision":
                self.case.service.select_candidate(job["job_id"], job["selection"]["selected_candidate_id"],
                    expected_revision=job["revision"], expected_candidate_set_sha256=job["candidate_set"]["candidate_set_sha256"], reason="fixture new confirmation")
            return response
        with _RealAskProbe(answer) as probe, ExitStack() as stack:
            probe.session.set("product_job_id",job["job_id"])
            if mode in {"slow_cleanup", "late_answer"}:
                from product_workflow import ui_cards
                stack.enter_context(patch.object(ui, "time", clock))
                stack.enter_context(patch.object(ui_cards, "time", clock))
                remove = ui.cl.CustomElement.remove
                async def cleanup(element):
                    now[0] += 1
                    await remove(element)
                stack.enter_context(patch.object(ui.cl.CustomElement, "remove", cleanup))
            stack.enter_context(patch.object(ui,"_workflow",return_value=self.case.service))
            stack.enter_context(patch("product_workflow.mesh_domain.render_mesh_views", return_value={name:b"fixture-image" for name in ("original","selected","excluded")}))
            stack.enter_context(patch.object(ui,"_refresh_mesh_entry",new=AsyncMock()))
            state=ui._job_ui_state(job["job_id"])
            lease={"job_id":job["job_id"],"stage":"SUBMITTING","session_generation":ui._session_generation(),
                "entry_token":"fixture-token", "confirmation_sha256":job["selection"]["confirmation"]["confirmation_sha256"]}
            state["busy"]=lease
            result=await ui._confirm_uploaded_domain(job,content=MIXED,lease=lease)
            if mode in {"confirm", "slow_cleanup"}:
                self.assertIsNotNone(result)
                self.assertEqual(result["status"],"AWAITING_CONFORMAL_AUTHORIZATION")
                self.assertIsNone(await ui._confirm_uploaded_domain(job,content=MIXED,lease=lease))
            else: self.assertIsNone(result)
            self.assertEqual(len(seen),1)
            self.assertEqual(seen[0]["kind"],"mesh")
            self.assertEqual(seen[0]["mode"],"await_domain")
            self.assertEqual(seen[0]["selectedCount"],1)
            self.assertEqual(seen[0]["excludedSolids"],1)
            self.assertEqual(set(seen[0]["views"]),{"original","selected","excluded"})
            self.assertEqual(set(seen[0]["askPayload"]),{"entry_token","upload_id","proposal_sha256"})
        current=self.case.service.get_consistent_job(job["job_id"])
        self.case.assert_no_compute(current)
        if mode not in {"confirm", "slow_cleanup"}:
            self.assertIsNone(current["mesh_binding"])
            self.assertFalse(Path(job["mesh_uploads"][-1]["stored_path"]).with_name("selected_hex.inp").exists())

    async def test_timely_answer_survives_slow_cleanup(self): await self.exercise("slow_cleanup")
    async def test_late_answer_is_rejected(self): await self.exercise("late_answer")
    async def test_explicit_preview_and_confirmation(self): await self.exercise("confirm")
    async def test_cancel(self): await self.exercise("cancel")
    async def test_timeout(self): await self.exercise("timeout")
    async def test_foreign_payload(self): await self.exercise("foreign")
    async def test_non_boolean_payload(self): await self.exercise("nonboolean")
    async def test_stale_session(self): await self.exercise("stale_session")
    async def test_stale_revision(self): await self.exercise("stale_revision")


class DomainAskLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_callback_takes_over_waiting_file_ask(self):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _RealAskProbe, _temporary_upload_path
        case = DomainWorkflowTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        job = case.ready
        source = _temporary_upload_path("attachment-takeover")
        source.write_bytes(MIXED)
        file_waiting, file_finished = asyncio.Event(), asyncio.Event()
        async def answer(probe, request):
            if request["spec"]["type"] == "file":
                file_waiting.set()
                try: await asyncio.Event().wait()
                finally: file_finished.set()
            return probe.response()
        with _RealAskProbe(answer) as probe, patch.object(ui, "_workflow", return_value=case.service), patch("product_workflow.mesh_domain.render_domain_preview", return_value=b"fixture-image"), patch.object(case.service, "bind_mesh", wraps=case.service.bind_mesh) as bind:
            probe.session.set("product_job_id", job["job_id"])
            await ui.send_product_mesh_entry_card(job)
            first = asyncio.create_task(ui.product_mesh_upload_entry(probe.current_entry()))
            try:
                await asyncio.wait_for(file_waiting.wait(), 1)
                old_lease = ui._job_ui_state(job["job_id"])["busy"]
                # This test task is an independent on_message context, not the upload action.
                self.assertIsNone(ui._UPLOAD_LOADING_OWNER.get())
                await ui._handle_attachment(SimpleNamespace(name="fixture.inp", path=str(source)))
                self.assertTrue(file_finished.is_set(), "old file Ask must retire before domain Ask")
                self.assertIsNot(ui._job_ui_state(job["job_id"])["busy"], old_lease)
                self.assertFalse(probe.loading)
                bind.assert_called_once()
            finally:
                if not first.done(): first.cancel()
                await asyncio.gather(first, return_exceptions=True)
        current = case.service.get_consistent_job(job["job_id"])
        self.assertEqual(current["status"], "AWAITING_CONFORMAL_AUTHORIZATION")
        case.assert_no_compute(current)

    async def exercise_loading(self, mode):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _RealAskProbe, _temporary_upload_path
        case = DomainWorkflowTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        job = case.ready
        source = _temporary_upload_path("domain-loading")
        source.write_bytes(MIXED)
        calls = []
        async def answer(probe, request):
            calls.append(request)
            if len(calls) == 1:
                return probe.response()
            if mode == "timeout": raise AskTimeoutError("fixture domain socketio timeout")
            if mode == "outer_timeout": raise asyncio.TimeoutError("fixture domain outer timeout")
            if mode == "exception": raise RuntimeError("fixture domain transport")
            if mode == "expired":
                ui._job_ui_state(job["job_id"])["busy"]["expires_at"] = 0
            if mode == "stale_session":
                probe.session.set("product_session_generation", "new-generation")
            return probe.response(confirm=mode != "cancel")
        with _RealAskProbe(answer) as probe, patch.object(ui, "_workflow", return_value=case.service), patch.object(ui, "_ask_for_product_mesh", new=AsyncMock(return_value=[SimpleNamespace(name="fixture.inp", path=str(source))])), patch("product_workflow.mesh_domain.render_domain_preview", return_value=b"fixture-image"):
            probe.session.set("product_job_id", job["job_id"])
            await ui.send_product_mesh_entry_card(job)
            await ui.product_mesh_upload_entry(probe.current_entry())
            self.assertEqual(len(calls), 2, str([m.get("output") for m in probe.messages.values()]))
            self.assertFalse(probe.loading, f"domain {mode} left loading active")
            if mode == "timeout":
                self.assertIn("ask_timeout", [kind for kind, _ in probe.events])
            self.assertIsNone(ui._job_ui_state(job["job_id"])["busy"])
        current = case.service.get_consistent_job(job["job_id"])
        case.assert_no_compute(current)
        if mode == "success":
            self.assertEqual(current["status"], "AWAITING_CONFORMAL_AUTHORIZATION")
        else:
            self.assertIsNone(current["mesh_binding"])

    async def test_domain_ask_loading_lifecycle(self):
        for mode in ("success", "cancel", "timeout", "outer_timeout", "exception"):
            with self.subTest(mode=mode): await self.exercise_loading(mode)

    async def test_domain_confirmation_never_auto_starts_runtime(self):
        await self.exercise_loading("success")

    async def test_expired_domain_answer_cannot_bind(self):
        await self.exercise_loading("expired")

    async def test_domain_stale_session_does_not_bind(self):
        await self.exercise_loading("stale_session")
