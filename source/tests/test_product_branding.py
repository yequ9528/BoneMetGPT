"""Offline display-name contracts; paths and compatibility identifiers stay unchanged."""
import ast
from pathlib import Path
import re
import unittest
from unittest.mock import AsyncMock, patch

from product_workflow import chat_agent, chainlit_ui, config
from product_workflow.chat_tools import ChatTools
from tests.test_product_chainlit_mesh_upload import _UiProbe

SOURCE = Path(__file__).resolve().parents[1]
NAME = "BoneMetGPT"
OLD_DISPLAY_NAME = "BoneMetaGPT"


class ProductBrandingTests(unittest.TestCase):
    def test_chainlit_assistant_name(self):
        text = (SOURCE / ".chainlit/config.toml").read_text(encoding="utf-8")
        section = re.search(r"(?ms)^\[UI\]\s*\n(.*?)(?=^\[|\Z)", text).group(1)
        self.assertRegex(section, r'(?m)^name\s*=\s*"BoneMetGPT"\s*$')

    def test_viewer_tab_title(self):
        text = (SOURCE / "viewer_frontend/product.html").read_text(encoding="utf-8")
        self.assertIn("<title>BoneMetGPT V1 Product Viewer</title>", text)
        self.assertNotIn(OLD_DISPLAY_NAME, text)

    def test_welcome_markdown(self):
        text = (SOURCE / "chainlit.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# BoneMetGPT ·"))
        self.assertNotIn(OLD_DISPLAY_NAME, text)

    def test_dom_header_display_name(self):
        text = (SOURCE / "public/conversation-previews.js").read_text(encoding="utf-8")
        self.assertIn("brand.textContent = 'BoneMetGPT';", text)
        self.assertNotIn(OLD_DISPLAY_NAME, text)
        self.assertIn("brand.className = 'bone-brand'", text)

    def test_shared_viewer_header(self):
        text = (SOURCE / "viewer_frontend/src/layout/WorkflowHeader.jsx").read_text(encoding="utf-8")
        self.assertIn("<h1>BoneMetGPT V1.0</h1>", text)
        self.assertNotIn(OLD_DISPLAY_NAME, text)

    def test_planning_and_execution_prompts_share_official_identity(self):
        rule = getattr(chat_agent, "PROJECT_IDENTITY_PROMPT", "")
        self.assertTrue(rule, "An explicit official-name and evidence-preservation rule is required")
        self.assertIn(NAME, rule)
        self.assertIn("LatticeGPT", rule)
        self.assertIn("原样保留", rule)
        for prompt in (chat_agent.PLANNING_PROMPT, chat_agent.SYSTEM_PROMPT):
            self.assertIn(rule, prompt)
            self.assertNotIn(OLD_DISPLAY_NAME, prompt)
        self.assertIn("English", chat_agent.SYSTEM_PROMPT)

    def test_internal_configuration_identifiers_preserved(self):
        self.assertEqual(config.LatticeGPTConfig.__name__, "LatticeGPTConfig")
        text = (SOURCE / "product_workflow/config.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        self.assertIn("LatticeGPTConfig", {node.name for node in tree.body if isinstance(node, ast.ClassDef)})
        self.assertIn('CONFIG_SCHEMA_VERSION_V2 = "latticegpt_paths_config_v2"', text)

    def test_configuration_error_display_names(self):
        path = Path("synthetic-config.json")
        with patch.object(Path, "read_text", side_effect=FileNotFoundError("synthetic absence")):
            with self.assertRaisesRegex(ValueError, "unable to read BoneMetGPT config"):
                config._read_json_config(path)
        with patch.object(Path, "read_text", return_value="[]"):
            with self.assertRaisesRegex(ValueError, "BoneMetGPT config must be a JSON object"):
                config._read_json_config(path)


class ProductBrandingAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_start_card_uses_official_name(self):
        with _UiProbe(), patch.object(chainlit_ui, "_send_job_stage_card", new_callable=AsyncMock) as send:
            await chainlit_ui.send_product_start_card()
        content = send.await_args.args[2]
        self.assertIn("# BoneMetGPT ·", content)
        self.assertNotIn(OLD_DISPLAY_NAME, content)

    async def test_actual_agent_dispatches_identity_and_keeps_original_references(self):
        observed = []
        recorded_path = r"C:\synthetic_fixture\record.json"
        answer = f"BoneMetGPT preserves `{recorded_path}` and `LatticeGPTConfig` as recorded."

        def forbidden(*_args, **_kwargs):
            self.fail("An identity-only conversation must not query data or execute workflow actions")

        def model(messages):
            observed.append(messages)
            if len(observed) == 1:
                return {"kind": "plan", "task": "conversation"}
            return {"kind": "answer", "text": answer}

        tools = ChatTools(query_service=forbidden, read_job=forbidden, read_result=forbidden,
                          current_job_id=lambda: None, session_id="branding-offline")
        result = await chat_agent.ChatAgent(model, tools).run("Introduce the project and preserve the cited path.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["text"], answer)
        self.assertEqual(len(observed), 2)
        for messages in observed:
            self.assertIn(NAME, messages[0]["content"])
            self.assertNotIn(OLD_DISPLAY_NAME, messages[0]["content"])
        self.assertEqual(tools.case.used["tool_calls"], 0)
        self.assertEqual(tools.pending, {})
        self.assertIsNone(tools.current_job_id())
