"""Cross the production Chat entry, model adapter, validated SQLite reader and UI transport."""
import csv
import copy
import hashlib
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import latticegpt_v1_app as entry
from product_workflow import chainlit_ui as ui
from product_workflow import formal_150w_query as database
from product_workflow.chat_tools import ChatTools
from tests.test_product_chainlit_mesh_upload import _UiProbe
from tests.test_product_workflow_targets import local_test_directory


class FollowupDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_number_drives_verified_read_and_only_this_turns_table(self):
        ask = "Use that record's C11 as the inclusive lower bound in database units; show two, highest first."
        marker = "SYNTHETIC_PRIVATE_SOURCE_FILE"
        calls, reads, frozen = [], [], []
        bind_read_plan = ChatTools.bind_read_plan

        def bind(tools, *args, **kwargs):
            turn = bind_read_plan(tools, *args, **kwargs)
            frozen.append(copy.deepcopy(turn.requirement["request"]))
            return turn
        with local_test_directory() as root:
            raw = root / (marker + ".csv")
            flags = {"is_singular": 0, "is_pd_full": 1, "has_periodic_mismatch": 0, "is_degenerate": 0}
            with raw.open("x", newline="", encoding="utf-8") as file:
                table = csv.DictWriter(file, fieldnames=[*database.PROJECTION_COLUMNS, *flags])
                table.writeheader()
                for index, stiffness in ((11, .0625), (22, .125), (33, .25)):
                    table.writerow({"sample_id": index, "relative_density": .2, "diameter": .1,
                                    **dict.fromkeys(database.STIFFNESS_NAMES, stiffness), **flags})
            projection, manifest = root / "design.sqlite", root / "identity.json"
            original = hashlib.sha256(raw.read_bytes()).hexdigest().upper()
            database.build_projection(raw, projection, manifest, expected_source_sha256=original)
            service = database.Formal150wQueryService(projection, manifest, expected_source_sha256=original)
            before = {p: hashlib.sha256(p.read_bytes()).digest() for p in (raw, projection, manifest)}
            execute = service.execute_request

            def read(request):
                value = execute(request)
                reads.append({"request": request, "status": value["status"],
                              "ids": [r["sample_id"] for r in value["data"]["rows"]]})
                return value

            def invoke(messages):
                calls.append([{"role": m.type, "content": m.content} for m in messages])
                history = json.loads(messages[-1].content)
                context = json.loads(history[-1]["content"])
                if context.get("phase") != "planning":
                    answer = {"kind": "answer", "text": "Please see this turn's recorded result."}
                elif next(row["content"] for row in reversed(history) if row["role"] == "user") != ask:
                    answer = {"kind": "plan", "task": "records", "arguments": {
                        "conditions": [{"field": "sample_id", "op": "eq", "value": 22, "unit": "id"}], "limit": 1}}
                else:
                    saved = context["context"]["records"]["result"]
                    row = saved["data"]["rows"][0]
                    if "C11" in row and saved.get("units", {}).get("stiffness") == "C/Eb":
                        answer = {"kind": "plan", "task": "records", "arguments": {
                            "conditions": [{"field": "C11", "op": "gte", "value": row["C11"], "unit": "C/Eb"}],
                            "order_by": [{"field": "C11", "direction": "desc"}], "limit": 2}}
                    else:
                        answer = {"kind": "clarify", "text": "The numeric threshold or its unit is missing."}
                return SimpleNamespace(content=json.dumps(answer))

            with _UiProbe() as probe, patch.object(ui, "_query_service", return_value=service), \
                    patch.object(ChatTools, "bind_read_plan", new=bind), \
                    patch.object(service, "execute_request", side_effect=read), \
                    patch.object(service, "_execute_read", wraps=service._execute_read) as sql, \
                    patch.object(ui, "get_chat_llm", return_value=SimpleNamespace(invoke=invoke)):
                probe.session.set("product_job_id", None)
                await entry.on_v1_message(SimpleNamespace(content="Find reference sample 22.", elements=[]))
                offset = len(probe.events)
                first_messages = set(probe.messages)
                await entry.on_v1_message(SimpleNamespace(content=ask, elements=[]))
                output = [payload["output"] for kind, payload in probe.events[offset:] if kind == "send_step"
                          and payload["id"] not in first_messages and isinstance(payload.get("output"), str)]
            first_question = "Find reference sample 22."
            plans = []
            for call in calls:
                messages = json.loads(call[-1]["content"])
                user_text = [m["content"] for m in messages if m["role"] == "user"][-1]
                envelope = next(json.loads(m["content"]) for m in messages if m["role"] == "tool")
                if envelope["phase"] == "planning":
                    plans.append((user_text, envelope["context"]))
                else:
                    self.fail("Execution-only queries must finish without a model explanation.")
            self.assertEqual(len(calls), 2)
            self.assertEqual([q for q, _ in plans], [first_question, ask])
            self.assertEqual(plans[1][1]["records"]["result"]["data"]["rows"][0]["C11"], .125)
            self.assertEqual(frozen, [r["request"] for r in reads])
            statements = [list(call.args) for call in sql.call_args_list]
            self.assertEqual(len(statements), 2)
            self.assertTrue(statements[1][0].endswith(" WHERE C11 >= ? ORDER BY C11 DESC, sample_id ASC LIMIT ?"))
            self.assertEqual(statements[1][1], [.125, 2])
            record = {"synthetic": True, "question": ask, "provider_messages": calls,
                      "frozen_requests": frozen, "parameterized_reads": statements,
                      "verified_reads": reads, "new_message_bodies": output}
            if os.environ.get("BMG_ACCEPTANCE_ARTIFACTS"):
                path = Path(os.environ["BMG_ACCEPTANCE_ARTIFACTS"]) / "followup-query-delivery.json"
                with path.open("x", encoding="utf-8") as f:
                    json.dump(record, f, ensure_ascii=False, indent=2)
            self.assertEqual([r["ids"] for r in reads], [[22], [33, 22]])
            request = reads[1]["request"]
            self.assertEqual(request["filters"], [{"field": "C11", "op": "gte", "value": .125}])
            self.assertEqual(request["limit"], 2)
            self.assertEqual(request["order_by"][0], {"field": "C11", "direction": "desc"})
            rendered = "\n".join(output)
            self.assertIn("0.125", rendered)
            self.assertIn("C/Eb", rendered)
            self.assertEqual(rendered.count("| Record |"), 1)
            self.assertIn("| 1 | 33 |", rendered)
            self.assertIn("| 2 | 22 |", rendered)
            self.assertIn("prior sample 22: C11=0.125 C/Eb", rendered)
            self.assertNotIn("prior sample 33", rendered)
            self.assertNotIn(marker, json.dumps(record))
            self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).digest() for p in before})

