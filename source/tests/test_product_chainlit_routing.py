import unittest
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from product_workflow.preflight import PreflightBindingV1


def full_product_preflight():
    return PreflightBindingV1(
        "full_product", None, None, None, "A" * 64, "B" * 64, "C" * 64
    )


def _message_recorder(records):
    def factory(*, content, actions=None, **kwargs):
        message = SimpleNamespace(
            content=content,
            actions=list(actions or []),
            kwargs=kwargs,
        )

        async def send():
            records.append(message)
            return message

        message.send = send
        return message

    return factory


def _action_factory(**kwargs):
    return SimpleNamespace(**kwargs)


class _Session:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class ProductChainlitRoutingTests(unittest.TestCase):
    def test_r3_replay_action_uses_frozen_plan_boundary_without_runtime_start(self):
        source = (Path(__file__).resolve().parents[1] / "product_workflow/chainlit_ui.py").read_text(encoding="utf-8")
        self.assertIn('@cl.action_callback("product_replay_frozen_query")', source)
        self.assertIn("replay_frozen_query_plan", source)
        replay_function = next(node for node in ast.parse(source).body
                               if isinstance(node, ast.AsyncFunctionDef) and node.name == 'replay_frozen_query')
        replay_block = ast.get_source_segment(source, replay_function)
        self.assertNotIn("start_conformal", replay_block)
        self.assertNotIn("confirm_target_and_run_inverse", replay_block)
    def test_product_ui_uses_central_config_for_store_and_product_urls(self):
        from product_workflow import chainlit_ui
        from product_workflow.config import load_latticegpt_config

        root = Path(__file__).resolve().parent / "config-only-no-write"
        config = load_latticegpt_config(
            explicit={"release_root": root, "services": {"frontend_port": 19471}},
            environ={},
        )
        chainlit_ui.configure_product_ui(config, preflight=full_product_preflight())
        try:
            self.assertEqual(chainlit_ui._job_store().runtime_root, config.paths.runtime_root)
            job_id = "job-AAAAAAAAAAAAAAAAAAAAAAAA"
            self.assertEqual(
                chainlit_ui._viewer_job_url(job_id),
                f"http://{config.services.frontend_host}:19471/product.html?job_id={job_id}",
            )
        finally:
            chainlit_ui.reset_product_ui_for_tests()

    def test_generic_design_intent_enters_product_route(self):
        from product_workflow.chainlit_ui import is_product_design_intent

        self.assertTrue(is_product_design_intent("帮我设计一个适合骨植入的晶格结构"))
        self.assertTrue(is_product_design_intent("根据目标刚度推荐三个单胞候选"))
        self.assertTrue(is_product_design_intent("design a conformal bone implant lattice"))
        self.assertFalse(is_product_design_intent("你好，请介绍一下这个项目"))
        self.assertFalse(is_product_design_intent("查询新版180w C11最高的5个"))

    def test_product_only_entry_does_not_dispatch_legacy_routes(self):
        source = (
            Path(__file__).resolve().parents[1] / "latticegpt_v1_app.py"
        ).read_text(encoding="utf-8")

        self.assertIn("handle_product_message", source)
        for forbidden in (
            "parse_truss_inverse_150w_command",
            "parse_conformal_service_command",
            "run_conformal_service",
        ):
            self.assertNotIn(forbidden, source)

    def test_product_query_planner_uses_shared_structured_boundary_and_binds_evidence(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "product_workflow"
            / "chainlit_ui.py"
        ).read_text(encoding="utf-8")

        self.assertIn("invoke_json_object", source)
        self.assertIn('"product_query_planner_evidence"', source)
        self.assertIn("plan_database_read", source)
        self.assertNotIn("return json.loads(content)", source)


class ProductMessageClassificationTests(unittest.TestCase):
    def _classify(self, content, session_values=None, elements=None):
        from product_workflow import chainlit_ui

        self.assertTrue(
            hasattr(chainlit_ui, "classify_product_message"),
            "classify_product_message is missing",
        )
        return chainlit_ui.classify_product_message(
            SimpleNamespace(
                content=content,
                elements=list(elements or []),
            ),
            _Session(session_values),
        )

    def test_explicit_product_command_enters_product_route(self):
        self.assertEqual(self._classify("/product"), "start")
        self.assertEqual(self._classify("开始产品设计"), "start")

    def test_exact_resume_command_has_its_own_route(self):
        self.assertEqual(
            self._classify("/resume job-AAAAAAAAAAAAAAAAAAAAAAAA"),
            "resume",
        )
        self.assertEqual(self._classify("/resume latest"), "unknown")

    def test_active_product_job_routes_inp_and_csv_attachments(self):
        session = {
            "product_mode": True,
            "product_job_id": "job-AAAAAAAAAAAAAAAAAAAAAAAA",
        }
        for filename in ("implant.inp", "target.csv"):
            with self.subTest(filename=filename):
                route = self._classify(
                    "",
                    session,
                    [SimpleNamespace(name=filename, path=f"C:/{filename}")],
                )
                self.assertEqual(route, "attachment")

    def test_product_target_state_consumes_natural_language_query(self):
        self.assertEqual(
            self._classify(
                "寻找C11较高且相对密度较低的结构",
                {"product_mode": True},
            ),
            "database",
        )

    def test_active_product_job_keeps_ordinary_text_outside_state_machine(self):
        session = {
            "product_mode": True,
            "product_job_id": "job-AAAAAAAAAAAAAAAAAAAAAAAA",
        }
        self.assertEqual(
            self._classify("单胞是什么？", session),
            "help",
        )
        self.assertEqual(
            self._classify("重新查询产品目标：C11较高", session),
            "database",
        )


class ProductCandidateUploadRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from product_workflow import chainlit_ui
        from product_workflow.config import load_latticegpt_config

        config = load_latticegpt_config(
            explicit={
                "release_root": Path(__file__).resolve().parent
                / "candidate-card-config-only-no-write"
            },
            environ={},
        )
        chainlit_ui.configure_product_ui(config, preflight=full_product_preflight())
        self.addCleanup(chainlit_ui.reset_product_ui_for_tests)

    async def test_repeated_candidate_stage_updates_existing_job_cards(self):
        from product_workflow import chainlit_ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe, _upload_job
        with _UiProbe() as probe:
            await chainlit_ui._show_candidates(_upload_job())
            first_messages = set(probe.messages)
            await chainlit_ui._show_candidates(_upload_job())
            self.assertEqual(set(probe.messages), first_messages)
            entries = [a for a in probe.actions.values() if a['name'] == 'product_mesh_upload_entry']
            self.assertEqual(len(entries), 1)
            await chainlit_ui.invalidate_product_waits()

    async def test_candidate_card_exposes_upload_only_after_explicit_confirmation(self):
        from product_workflow import chainlit_ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe, _upload_job
        from product_workflow.contracts import canonical_sha256
        job = _upload_job()
        with _UiProbe() as probe:
            await chainlit_ui._show_candidates(job)
            self.assertEqual(probe.current_entry().payload["confirmation_sha256"], job["selection"]["confirmation"]["confirmation_sha256"])
        job["selection"].pop("confirmation")
        job["selection"].pop("selection_sha256")
        job["selection"]["selection_sha256"] = canonical_sha256(job["selection"])
        with _UiProbe() as probe:
            await chainlit_ui._show_candidates(job)
            self.assertFalse(any(a["name"] == "product_mesh_upload_entry" for a in probe.actions.values()))
            self.assertFalse(probe.card_props('candidates')['selectedId'])
            await chainlit_ui.invalidate_product_waits()

    async def test_candidate_card_without_selection_has_no_upload_action(self):
        from product_workflow import chainlit_ui

        sent = []
        job = {
            "job_id": "job-no-eligible-candidate",
            "revision": 4,
            "candidate_set": {
                "candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "rank": None,
                        "score": None,
                        "normalized_diameter": 0.2,
                        "eligible": False,
                        "p09_validation": {"status": "FAIL"},
                    }
                ]
            },
            "selection": None,
        }

        from tests.test_product_chainlit_mesh_upload import _UiProbe
        with _UiProbe() as probe:
            probe.session.set('product_job_id', job['job_id'])
            await chainlit_ui._show_candidates(job)
            self.assertFalse(any(a['name'] == 'product_mesh_upload_entry' for a in probe.actions.values()))
            self.assertIn('HOLD', probe.card_props('candidates')['message'])
            self.assertTrue(all(cell['action'] is None for cell in probe.card_props('candidates')['candidates']))
            await chainlit_ui.invalidate_product_waits()


class ReadonlyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_unknown_negated_design_and_missing_job_never_create_or_plan(self):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        for current in (None, "job-" + "A" * 24):
            with _UiProbe() as probe, patch.object(ui, "_run_product_chat", new=AsyncMock()) as chat, patch.object(ui, "_create_and_show_target", new=AsyncMock()) as create:
                probe.session.set("product_job_id", current)
                for text in ("你的功能是什么？", "不要开始设计，只解释流程。", "解释流程，别生成晶格", "看看那个", "帮我处理一下", "B31是什么？"):
                    self.assertTrue(await ui.handle_product_message(SimpleNamespace(content=text, elements=[])))
                if current is None:
                    await ui.handle_product_message(SimpleNamespace(content="这个结果怎么样？", elements=[]))
                    self.assertEqual(chat.await_count, 7)
                create.assert_not_awaited()

    async def test_database_failure_and_compound_question_never_fall_through_to_creation(self):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        for outcome in (TimeoutError("model unavailable"), {"status": "design", "action": "create"}):
            with _UiProbe(), patch.object(ui, "_chat_model", side_effect=outcome if isinstance(outcome, Exception) else None, return_value=outcome), patch.object(ui, "_chat_read_job", side_effect=ValueError("synthetic unavailable Job")), patch.object(ui, "_create_and_show_target", new=AsyncMock()) as create:
                await ui.handle_product_message(SimpleNamespace(content="查询 C11 较高的记录 C/Eb", elements=[]))
                create.assert_not_awaited()

    async def test_database_browsing_actions_bind_result_version_and_require_creation_confirmation(self):
        from product_workflow import chainlit_ui as ui
        from tests.test_product_chainlit_mesh_upload import _UiProbe
        from product_workflow.formal_150w_query import DATABASE_REQUEST_SCHEMA
        from tests.test_product_chat_tools import SyntheticQuery
        query = SyntheticQuery()
        result = query.execute_request({
            "schema_version": DATABASE_REQUEST_SCHEMA, "kind": "records",
            "filters": [], "order_by": [], "limit": 1,
        })
        with _UiProbe() as probe, patch.object(ui, "_query_service", return_value=query), patch.object(ui, "_create_and_show_target", new=AsyncMock()) as create:
            probe.session.set("product_job_id", None)
            await ui._show_database_result(result, question="查询几条，合适再设计")
            action = SimpleNamespace(**next(a for a in probe.actions.values() if a["name"] == "product_choose_reference"))
            await ui.choose_reference(SimpleNamespace(payload={**action.payload, "sample_id": 2}))
            self.assertFalse(any(a["name"] == "product_chat_confirm" for a in probe.actions.values()))
            from tests.test_product_ui_v5 import csv_bytes
            await ui._handle_csv_upload(csv_bytes(1), filename="prior.csv")
            csv_record, = ui._creation_state()["actions"].values()
            await ui.choose_reference(action)
            self.assertEqual(csv_record["state"], "RETIRED")
            self.assertIsNone(probe.session.get("product_pending_csv_target"))
            self.assertEqual(sum(a["name"] == "product_chat_confirm" for a in probe.actions.values()), 1)
            await ui.choose_reference(action)
            self.assertEqual(sum(a["name"] == "product_chat_confirm" for a in probe.actions.values()), 1)
            create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
