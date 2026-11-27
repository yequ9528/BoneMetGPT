from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from product_workflow import chainlit_ui


class _Session:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


def _candidate_job() -> dict:
    return {
        "schema_version": "latticegpt_design_job_v1",
        "job_id": "job-AAAAAAAAAAAAAAAAAAAAAAAA",
        "revision": 4,
        "status": "CANDIDATES_READY",
        "workflow_disposition": "ACTIVE",
        "created_at": "2026-08-02T00:00:00+00:00",
        "updated_at": "2026-08-02T00:04:00+00:00",
        "target": {"schema_version": "latticegpt_target_proposal_v1"},
        "model_runtime_profile": {"provider": "api", "model": "frozen-workflow-model"},
        "formal_bundle_binding": {"status": "PASS_5_OF_5"},
        "runtime_authorizations": [],
        "candidate_set": {
            "recommended_candidate_id": "candidate-1",
            "candidates": [{"candidate_id": "candidate-1", "eligible": True}],
        },
        "selection": {
            "recommended_candidate_id": "candidate-1",
            "selected_candidate_id": "candidate-1",
        },
        "mesh_binding": None,
        "mesh_uploads": [],
        "attempts": [],
        "failure": None,
    }


class ProductChainlitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_rejects_old_action_then_explicit_reopen_issues_fresh_entry(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe, _confirm_fixture
        job = _candidate_job()
        _confirm_fixture(job)
        job["candidate_set"]["candidates"][0].update(rank=1, score=0.1, normalized_diameter=0.2, p09_validation={"status": "PASS"})
        service = SimpleNamespace(get_consistent_job=lambda _: job)
        with _UiProbe() as old_probe:
            await chainlit_ui.send_product_mesh_entry_card(job)
            old_entry = old_probe.current_entry()
        with _UiProbe() as probe, patch.object(chainlit_ui, "_workflow", return_value=service), patch.object(chainlit_ui, "_job_store", return_value=SimpleNamespace(get_consistent=lambda _: job)), patch.object(chainlit_ui, "_product_config", return_value=SimpleNamespace(services=SimpleNamespace(viewer_frontend_origin="http://127.0.0.1:5173"))), patch.object(chainlit_ui, "_ask_for_product_mesh", new=AsyncMock(return_value=None)) as ask, patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind, patch.object(chainlit_ui._RUNTIME_MANAGER, "start_once", new=AsyncMock()) as runtime:
            await chainlit_ui.product_mesh_upload_entry(old_entry)
            ask.assert_not_awaited()
            self.assertTrue(await chainlit_ui.resume_product_job(job["job_id"]))
            entry = probe.current_entry()
            self.assertNotEqual(old_entry.id, entry.id)
            ask.assert_not_awaited()
            await chainlit_ui.product_mesh_upload_entry(entry)
            ask.assert_awaited_once()
        bind.assert_not_awaited()
        runtime.assert_not_awaited()

    async def test_old_picker_return_in_new_session_never_reads_or_binds(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe, _upload_job
        job = _upload_job()
        async def ask():
            chainlit_ui.cl.user_session.values.clear()
            chainlit_ui.cl.user_session.set("id", "replacement-session")
            return [SimpleNamespace(name="stale.inp", path="must-not-read.inp")]
        with _UiProbe() as probe, patch.object(chainlit_ui, "_workflow", return_value=SimpleNamespace(get_consistent_job=lambda _: job)), patch.object(chainlit_ui, "_ask_for_product_mesh", side_effect=ask), patch.object(chainlit_ui, "_bind_uploaded_mesh", new=AsyncMock()) as bind, patch.object(Path, "read_bytes", side_effect=AssertionError("stale files must not be read")) as read:
            await chainlit_ui.send_product_mesh_entry_card(job)
            await chainlit_ui.product_mesh_upload_entry(probe.current_entry())
        bind.assert_not_awaited()
        read.assert_not_called()

    async def test_start_card_calls_resume_reopen_without_compute_claim(self):
        sent = []

        async def capture(*args, **kwargs):
            sent.append((args, kwargs))

        with patch.object(chainlit_ui, "_send_job_stage_card", side_effect=capture):
            await chainlit_ui.send_product_start_card()

        content = sent[0][0][2]
        actions = sent[0][1]["actions"]
        self.assertIn("Reopening a Job", content)
        self.assertIn("does not resume computation", content)
        self.assertEqual(actions[1].label, "Reopen Job")
        self.assertNotIn("Resume computation", content)

    async def test_recognized_resume_command_is_consumed_even_when_restore_fails(self):
        resume = AsyncMock(return_value=False)
        with (
            patch.object(chainlit_ui, "resume_product_job", resume),
            patch.object(chainlit_ui.cl, "user_session", _Session()),
        ):
            handled = await chainlit_ui.handle_product_message(
                SimpleNamespace(
                    content="/resume job-AAAAAAAAAAAAAAAAAAAAAAAA",
                    elements=[],
                )
            )

        self.assertTrue(handled)
        resume.assert_awaited_once_with("job-AAAAAAAAAAAAAAAAAAAAAAAA")

    async def test_first_session_reconciles_orphaned_runtime_only_once(self):
        reconcile = MagicMock(return_value=[
            "job-AAAAAAAAAAAAAAAAAAAAAAAA"
        ])
        with (
            patch.object(chainlit_ui, "_RUNTIME_RECONCILED", False, create=True),
            patch.object(
                chainlit_ui,
                "reconcile_interrupted_product_jobs",
                reconcile,
                create=True,
            ),
            patch.object(chainlit_ui, "_job_store", return_value=SimpleNamespace()),
        ):
            first = await chainlit_ui.reconcile_product_runtime_state()
            second = await chainlit_ui.reconcile_product_runtime_state()

        self.assertEqual(first, ["job-AAAAAAAAAAAAAAAAAAAAAAAA"])
        self.assertEqual(second, [])
        reconcile.assert_called_once()

    async def test_resume_binds_only_the_exact_validated_job(self):
        job = _candidate_job()
        store = SimpleNamespace(get_consistent=lambda job_id: job)
        session = _Session({"model_name": "current-chat-model"})
        renderer = AsyncMock()
        runtime_start = MagicMock()

        with (
            patch.object(chainlit_ui, "_job_store", return_value=store),
            patch.object(chainlit_ui, "render_product_job", renderer),
            patch.object(chainlit_ui._RUNTIME_MANAGER, "start_once", runtime_start),
            patch.object(chainlit_ui.cl, "user_session", session),
        ):
            restored = await chainlit_ui.resume_product_job(job["job_id"])

        self.assertTrue(restored)
        self.assertEqual(session.get("product_job_id"), job["job_id"])
        self.assertEqual(session.get("product_job_revision"), 4)
        self.assertEqual(session.get("model_name"), "current-chat-model")
        renderer.assert_awaited_once_with(job, runtime_active=False)
        runtime_start.assert_not_called()

    async def test_missing_job_does_not_change_current_session_binding(self):
        def missing(_job_id):
            raise FileNotFoundError("missing")

        store = SimpleNamespace(get_consistent=missing)
        session = _Session({"product_job_id": "job-BBBBBBBBBBBBBBBBBBBBBBBB"})
        sent = []

        def message_factory(*, content, actions=None, **_kwargs):
            message = SimpleNamespace(content=content, actions=list(actions or []))

            async def send():
                sent.append(message)
                return message

            message.send = send
            return message

        with (
            patch.object(chainlit_ui, "_job_store", return_value=store),
            patch.object(chainlit_ui.cl, "user_session", session),
            patch.object(chainlit_ui.cl, "Message", side_effect=message_factory),
        ):
            restored = await chainlit_ui.resume_product_job(
                "job-AAAAAAAAAAAAAAAAAAAAAAAA"
            )

        self.assertFalse(restored)
        self.assertEqual(
            session.get("product_job_id"),
            "job-BBBBBBBBBBBBBBBBBBBBBBBB",
        )
        self.assertIn("Stage: Reopen Job", sent[-1].content)


class CreationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_created_job_is_mapped_before_failed_card_and_duplicate_never_creates_again(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        from product_workflow.contracts import target_from_physical_9d, STIFFNESS_NAMES
        target = target_from_physical_9d({n: 1 for n in STIFFNESS_NAMES}, source_kind="fixture", source_sha256="A"*64, source_name="fixture")
        job = {"job_id": "job-" + "B"*24, "revision": 0}
        service = SimpleNamespace(create_job=MagicMock(return_value=job))
        with _UiProbe() as probe, patch.object(chainlit_ui, "_workflow", return_value=service), patch.object(chainlit_ui, "_current_model_runtime_profile", return_value={}), patch.object(chainlit_ui, "_show_target_card", new=AsyncMock(side_effect=RuntimeError("send failed"))):
            await chainlit_ui._offer_target(target)
            action = SimpleNamespace(**next(a for a in probe.actions.values() if a["name"] == "product_create_target"))
            await chainlit_ui.create_target(action)
            record = chainlit_ui._creation_state()["actions"][action.payload["token"]]
            self.assertEqual(record["state"], "CREATED_UI_PENDING")
            self.assertEqual(probe.session.get("product_job_id"), job["job_id"])
            self.assertEqual(record["job_id"], job["job_id"])
            await chainlit_ui.create_target(action)
            service.create_job.assert_called_once()

    async def test_uncertain_create_blocks_repeat_without_inventing_job_id(self):
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        target = {"physical_9d_gpa": {"C11": 1}}
        service = SimpleNamespace(create_job=MagicMock(side_effect=OSError("write/response uncertain")))
        with _UiProbe() as probe, patch.object(chainlit_ui, "_workflow", return_value=service), patch.object(chainlit_ui, "_current_model_runtime_profile", return_value={}):
            await chainlit_ui._offer_target(target)
            action = SimpleNamespace(**next(a for a in probe.actions.values() if a["name"] == "product_create_target"))
            await chainlit_ui.create_target(action)
            await chainlit_ui.create_target(action)
            record = chainlit_ui._creation_state()["actions"][action.payload["token"]]
            self.assertEqual(record["state"], "CREATE_WRITE_UNCERTAIN")
            self.assertNotIn("job_id", record)
            service.create_job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
