from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import secrets
import json
import copy
import hashlib
from contextvars import ContextVar
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from chainlit.emitter import TimeoutError as AskTimeoutError

import chainlit as cl
from langchain_core.messages import HumanMessage, SystemMessage

from llm_provider import chat_agent_timeout_seconds, get_chat_llm, invoke_chat_action, invoke_json_object, resolve_model_runtime_profile

from .chat_agent import ChatAgent
from .chat_tools import ChatTools, ChatToolError, ChatCase, CHAT_DISPATCH

from .contracts import (
    PRODUCT_INP_MAX_SIZE_MB,
    PRODUCT_MESH_CONTRACT,
    STIFFNESS_NAMES,
    canonical_sha256,
    target_from_database_record,
)
from .config import LatticeGPTConfig, load_latticegpt_config
from .factory import create_formal_query_service, create_product_workflow_service
from .inputs import (
    plan_formal_query,
    proposals_from_formal_query,
    replay_frozen_query_plan,
    target_from_csv_upload,
    parse_target_csv_upload,
    target_from_csv_record,
    classify_readonly_intent,
    plan_database_read,
)
from .formal_150w_query import DATABASE_REQUEST_SCHEMA, ReadonlyQueryError
from .job_state import build_product_job_projection, build_readonly_product_job_projection, candidate_confirmation_status
from .preflight import PreflightBindingV1
from .presentation import (
    VALIDATION_NOTE, display_number, error_summary, exclusion_labels, job_status_text,
    recorded_chat_reply, recorded_error_code, recorded_stage, result_file_roles, stiffness_table,
)
from .runtime_manager import (
    RuntimeExecutionManager,
    reconcile_interrupted_product_jobs,
)
from .service import normalize_job_mesh_binding, CandidateConfirmationCommitError
from .store import JOB_ID_PATTERN, JobStore
from .ui_previews import (
    PreviewBusy, PreviewTimeout, PreviewResourceError, PreviewSourceError, PreviewCache,
    PreviewExecutor, render_candidate_previews, STANDARD_CELL_RENDERER, standard_candidate_label,
)
from .ui_cards import action_dict, update_component, DomainAsk


_WORKFLOW = None
_QUERY = None
_JOB_STORE = None
_CONFIG = None
_PREFLIGHT = None
_RUNTIME_MANAGER = RuntimeExecutionManager()
_RUNTIME_RECONCILED = False
_RUNTIME_RECONCILE_LOCK = threading.Lock()
PRODUCT_UI_SEND_TIMEOUT_SECONDS = 5.0
PRODUCT_UPLOAD_WAIT_SECONDS = 600
PRODUCT_PREVIEW_WAIT_SECONDS = 60
_PREVIEW_EXECUTOR = PreviewExecutor()
_INPUT_EXECUTOR = PreviewExecutor()
_UPLOAD_LOADING_OWNER = ContextVar("product_upload_loading_owner", default=None)


def _upload_loading_state(*, create=True):
    from chainlit.context import ChainlitContextException
    try:
        state = cl.user_session.get("product_upload_loading")
    except ChainlitContextException:
        if create:
            raise
        return None
    if state is None and create:
        state = {"lock": asyncio.Lock(), "owner": None, "failure": None}
        cl.user_session.set("product_upload_loading", state)
    return state


def _upload_runtime_active(owner):
    return any(_RUNTIME_MANAGER.is_active(job_id) for job_id in owner["job_ids"])


async def _retire_upload_loading_locked(state):
    """Drain only the raw Chainlit send task, which never acquires this lock.

    Holding the handoff lock prevents a new Ask/runtime from interleaving with
    Chainlit's internal task_end/task_start. Waiting is bounded and fail-closed.
    """
    if state["failure"] is not None:
        raise RuntimeError("Upload dialog cleanup failed. Further uploads and starts are blocked in this session. Preserve the session.") from state["failure"]
    owner = state["owner"]
    if owner is None:
        return
    try:
        task = owner["ask"]
        if task is not None and not task.done():
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=PRODUCT_UI_SEND_TIMEOUT_SECONDS)
            if not done:
                raise TimeoutError("previous upload Ask did not finish cleanup")
            if not task.cancelled():
                task.result()
        if not _upload_runtime_active(owner):
            await asyncio.wait_for(owner["emitter"].task_end(), timeout=PRODUCT_UI_SEND_TIMEOUT_SECONDS)
    except BaseException as exc:
        state["failure"] = exc
        raise
    state["owner"] = None


async def _begin_upload_loading(lease):
    state = _upload_loading_state()
    async with state["lock"]:
        await _retire_upload_loading_locked(state)
        job_ids = set(cl.user_session.get("product_ui_job_ids") or ()) | {lease["job_id"]}
        owner = {"state": state, "lease": lease, "session_generation": _session_generation(),
                 "emitter": cl.context.emitter, "job_ids": job_ids, "ask": None}
        if _upload_runtime_active(owner):
            raise RuntimeError("Computation is still running. No upload dialog was opened.")
        state["owner"] = owner
        return owner


async def _finish_upload_loading(owner):
    if owner is None:
        return
    state = owner["state"]
    async with state["lock"]:
        if state["owner"] is owner:
            await _retire_upload_loading_locked(state)


async def _tracked_upload_ask(send):
    owner = _UPLOAD_LOADING_OWNER.get()
    if owner is None:
        return await send()
    state = owner["state"]
    async with state["lock"]:
        if (state["owner"] is not owner or state["failure"] is not None
                or owner["session_generation"] != _session_generation()
                or _upload_runtime_active(owner)):
            raise ValueError(_UPLOAD_STALE)
        task = asyncio.create_task(send())
        owner["ask"] = task
    return await task


logger = logging.getLogger(__name__)

_UPLOAD_STALE = "This upload entry expired because the Job state changed. Use the latest upload entry."
_UPLOAD_BUSY = "An upload is already in progress. Finish the current operation first."
_UPLOAD_UPDATE_FAILED = "The upload entry could not be updated. Refresh Job status; do not upload again."


class _DisplayDetails(str):
    """Text constructed by a stage-specific display formatter, never raw evidence."""


def _choice(value, choices, fallback="Not recorded"):
    return value if isinstance(value, str) and value in choices else fallback


def _filename(value):
    if not isinstance(value, str) or any(c in value for c in '{}\r\n'):
        return "Not recorded"
    return PureWindowsPath(value).name or "Not recorded"


def _mesh_details(job, mesh):
    # Only inspect the precheck belonging to the bound upload.
    binding = job.get("mesh_binding") or {}
    upload = next((item for item in job.get("mesh_uploads") or []
                   if item.get("upload_id") == mesh.get("upload_id")), {})
    precheck = upload.get("domain_precheck") or upload.get("precheck") or binding.get("precheck") or {}
    info = [str(_quality_details(precheck))]
    for key, label in (("raw_size_bytes", "Input size (bytes)"), ("reference_length_mm", "Reference length (mm)")):
        if type(mesh.get(key)) in (int, float):
            info.append(f"{label}: {mesh[key]}")
    element = _choice(mesh.get("element_type_original"), ("C3D8", "C3D8R"))
    info.append(f"Input element: {element}")
    return _DisplayDetails("\n".join(info))


def _excluded_counts(domain):
    return {kind: item["count"] for kind, item in (domain.get("excluded_types") or {}).items()
            if re.fullmatch(r"[A-Z][A-Z0-9]{0,15}", kind) and isinstance(item, dict)
            and type(item.get("count")) is int}


def _quality_details(precheck):
    quality = precheck.get("geometry_quality") or {}
    state = _choice(precheck.get("status"), ("passed", "failed", "PASS", "HOLD", "FAIL"))
    lines = [f"Input precheck: {state}"]
    for key, label in (("minimum_scaled_jacobian", "Minimum scaled Jacobian"),
                       ("minimum_jacobian_determinant", "Minimum Jacobian determinant"),
                       ("maximum_jacobian_condition_2", "Maximum Jacobian condition"),
                       ("reference_length", "Reference length (mm)")):
        if type(quality.get(key)) in (float, int):
            lines.append(f"{label}: {quality[key]}")
    return _DisplayDetails("\n".join(lines))


def _format_product_error(
    stage: str,
    error: Exception | object,
    *,
    write_state: str,
    retry_state: str,
) -> str:
    return (
        f"[Error] Stage: {stage}. Saved state: {write_state}. "
        f"Next step: {retry_state}. {recorded_error_code(error)}: {error_summary(error)}"
    )


def _product_error_evidence(error):
    code = recorded_error_code(error)
    stage = recorded_stage(error.get("stage")) if isinstance(error, dict) else "Unknown"
    return _DisplayDetails(f"{code}: {error_summary(error)} Recorded stage: {stage}.")


def _query_clarification_text(question):
    questions = {
        "刚度条件使用 C/Eb（无量纲）还是 GPa？请注明单位。":
            "Should the stiffness condition use dimensionless C/Eb or GPa? State the unit.",
        "请明确需要计数的字段、比较关系、数值和单位。":
            "For the count, specify the field, comparison, value, and unit.",
        "请明确每个筛选条件的比较关系，例如等于、大于或介于。":
            "Specify each comparison, such as equal to, greater than, or between.",
    }
    if question in questions:
        return questions[question]
    return ("Specify the query field, comparison, value, and unit in natural language. "
            "Stiffness conditions use C/Eb (dimensionless) or GPa; relative density and "
            "normalized strut diameter are dimensionless.")


def _workflow_status(content: str) -> str:
    if content.startswith("[Workflow status]"):
        return content.replace("[Workflow status] #", "[Workflow status]\n\n#", 1)
    return f"[Workflow status]\n\n{content}"


def _job_ui_state(job_id: str) -> dict:
    key = f"product_job_ui:{job_id}"
    state = cl.user_session.get(key)
    if state is None:
        state = {"lock": asyncio.Lock(), "cards": {}, "entry": None, "busy": None}
        cl.user_session.set(key, state)
        ids = set(cl.user_session.get("product_ui_job_ids") or ())
        ids.add(job_id)
        cl.user_session.set("product_ui_job_ids", ids)
    return state


def _session_generation() -> str:
    generation = cl.user_session.get("product_session_generation")
    if generation is None:
        generation = secrets.token_hex(16)
        cl.user_session.set("product_session_generation", generation)
    return generation


async def invalidate_product_waits() -> None:
    """Retire only in-memory actions; never mutate persisted Jobs on disconnect."""
    loading = _upload_loading_state(create=False)
    if loading is not None:
        async with loading["lock"]:
            await _retire_upload_loading_locked(loading)
    await _retire_csv_import()
    invalidate_product_chat()
    cl.user_session.set("product_session_generation", secrets.token_hex(16))
    for task in cl.user_session.get("product_preview_tasks") or ():
        task.cancel()
    cl.user_session.set("product_preview_tasks", set())
    preview_cache = cl.user_session.get("product_preview_cache")
    if preview_cache is not None:
        preview_cache["cache"].clear()
    cl.user_session.set("product_preview_cache", None)
    for job_id in cl.user_session.get("product_ui_job_ids") or ():
        state = cl.user_session.get(f"product_job_ui:{job_id}")
        if state is not None:
            for card in state["cards"].values():
                async with card["lock"]:
                    component = card.get("component")
                    if component is not None:
                        props = {**component.props, "historical": True, "locked": True,
                                 "askActive": False}
                        await update_component(component, props)
                        card["model"] = props
            async with state["lock"]:
                state["entry"] = None
                state["busy"] = None
                state["workflow_confirmations"] = {}
                state.pop("selection_pending", None)
                state.pop("selection_running", None)


def _confirmed_hash(job: dict) -> str | None:
    if candidate_confirmation_status(job) != "CONFIRMED":
        return None
    return job["selection"]["confirmation"]["confirmation_sha256"]


def _valid_revision(value) -> bool:
    return type(value) is int and value >= 1


def _issue_workflow_confirmation(action, name=None):
    name = name or getattr(action, "name", None)
    if name not in {"product_confirm_target", "product_select_candidate", "product_start_conformal"}:
        return
    token = getattr(action, "id", None)
    payload = getattr(action, "payload", None)
    if not token or not isinstance(payload, dict):
        return
    state = _job_ui_state(str(payload.get("job_id") or ""))
    state.setdefault("workflow_confirmations", {})[token] = {
        "name": name, "payload": dict(payload), "session_generation": _session_generation(),
        "issued_at": time.monotonic(), "consumed": False}


async def _consume_workflow_confirmation(action, name):
    payload = action.payload or {}
    job_id = payload.get("job_id")
    if not job_id or cl.user_session.get("product_job_id") != job_id:
        return False
    state = _job_ui_state(job_id)
    async with state["lock"]:
        record = state.get("workflow_confirmations", {}).get(getattr(action, "id", None))
        if (record is None or record["name"] != name or record["payload"] != payload
                or record["session_generation"] != _session_generation()
                or time.monotonic() - record["issued_at"] >= 900):
            return False
        if record["consumed"]:
            return name == "product_start_conformal" and _RUNTIME_MANAGER.is_active(job_id)
        record["consumed"] = True
        return True


def _side_text(name, text):
    from chainlit.context import ChainlitContextException, get_context
    try:
        thread_id = get_context().session.thread_id
    except ChainlitContextException:
        thread_id = ""  # offline message-construction tests only
    return cl.Text(name=name, content=text, display="side", thread_id=thread_id)


def _technical_elements(evidence):
    if not isinstance(evidence, _DisplayDetails) or not evidence:
        return []
    return [_side_text("Technical details", str(evidence))]


async def _send_job_stage_card(
    job_id: str,
    stage: str,
    content: str,
    *,
    actions=None,
    card_revision: int | None = None,
    terminal: bool = False,
    evidence=None,
    elements=None,
    model=None,
):
    if not isinstance(evidence, _DisplayDetails):
        evidence = None
    session_key = f"product_stage_card:{job_id}:{stage}"
    try:
        state = _job_ui_state(job_id)
    except Exception:
        # Informational callbacks may outlive their connection. No upload entry
        # can become valid without a live session-owned state record.
        if stage == "mesh_entry":
            raise RuntimeError("upload session unavailable")
        return await cl.Message(content=_workflow_status(content), actions=list(actions or []), elements=[*(elements or []), *_technical_elements(evidence)]).send()
    card = state["cards"].setdefault(stage, {
        "lock": asyncio.Lock(), "revision": None, "terminal": False,
    })
    async with card["lock"]:
        previous_revision = card["revision"]
        if ((card_revision is None and card["terminal"])
                or (card_revision is not None and previous_revision is not None
                    and card_revision < previous_revision)
                or (card["terminal"] and card_revision == previous_revision and not terminal)):
            return cl.user_session.get(session_key)
        next_actions = list(actions or [])
        if stage == "mesh_entry" and model is None:
            model = {"kind":"entry", "title":"Upload your mesh", "description":
                     "Candidate confirmed. Review the mesh requirements and upload one INP file.",
                     "actions":[action_dict(a) for a in next_actions]}
        # A business refresh replaces the publication, even at the same revision.
        # Late image work must not attach to its replacement or reissue actions.
        card["publication"] = object()
        card["preview_source"] = None
        card["preview_elements"] = []
        card["base_content"] = _workflow_status(content)
        display_content = ('Technical details' if evidence is not None else '') if model is not None else (
            _workflow_status(content) + ('\n\nTechnical details' if evidence is not None else ''))
        old_preview_task = card.get("preview_task")
        if old_preview_task is not None and old_preview_task is not asyncio.current_task():
            old_preview_task.cancel()
        if stage == "mesh_entry":
            async with state["lock"]:
                state["entry"] = None
            if next_actions and (not _valid_revision(card_revision) or len(next_actions) != 1
                    or next_actions[0].name != "product_mesh_upload_entry"
                    or set(next_actions[0].payload) != {"job_id", "expected_revision", "confirmation_sha256", "entry_token", "session_generation"}
                    or next_actions[0].payload.get("job_id") != job_id
                    or next_actions[0].payload.get("expected_revision") != card_revision):
                raise ValueError("upload card/action revision mismatch")
        if card_revision is not None:
            card["revision"] = card_revision
        card["terminal"] = terminal
        existing = cl.user_session.get(session_key)
        previous_actions = list(card.get("actions", getattr(existing, "actions", [])))
        card["actions"] = next_actions
        if model is not None:
            model = {"jobId": job_id, "historical": False,
                     "replayLabel": cl.user_session.get("product_replay_label"), **model}
            if job_id != "entry" and cl.user_session.get("product_job_id") != job_id:
                model.update(historical=True, locked=True, askActive=False)
                next_actions = []
                card['actions'] = []
            component = card.get("component")
            if component is None:
                component = cl.CustomElement(name="BoneTaskCard", props=model, display="inline")
            model['componentId'] = component.id
            component.props = model
            component.content = json.dumps(model, ensure_ascii=False)
            card["component"] = component
            card["model"] = model
            elements = [component, *(elements or [])]
        try:
            if existing is not None:
                # Message.update sends new actions but does not remove old ones.
                old_actions = previous_actions
                for old_action in old_actions:
                    records = state.get("workflow_confirmations", {})
                    record = records.get(getattr(old_action, "id", None))
                    if not (record and record["consumed"] and record["name"] == "product_start_conformal" and _RUNTIME_MANAGER.is_active(job_id)):
                        records.pop(getattr(old_action, "id", None), None)
                    await old_action.remove()
                existing.actions = [] if model is not None else next_actions
                existing.content = display_content
                for old_element in getattr(existing, "elements", []):
                    if old_element is not card.get("component"):
                        await old_element.remove()
                existing.elements = [*(elements or []), *_technical_elements(evidence)]
                if model is not None:
                    for next_action in next_actions:
                        next_action.forId = existing.id
                        _issue_workflow_confirmation(next_action)
                await existing.update()
                sent = existing
            else:
                sent = cl.Message(content=display_content,
                                  actions=[] if model is not None else next_actions,
                                  elements=[*(elements or []), *_technical_elements(evidence)])
                # Keep a partially sent card so explicit refresh can remove any
                # actions that reached the emitter before a transport failure.
                cl.user_session.set(session_key, sent)
                if model is not None:
                    for next_action in next_actions:
                        next_action.forId = sent.id
                        _issue_workflow_confirmation(next_action)
                await sent.send()
            if model is None:
                for next_action in next_actions:
                    _issue_workflow_confirmation(next_action)
            if stage == "mesh_entry" and next_actions:
                async with state["lock"]:
                    if _job_ui_state(job_id) is state:
                        state["entry"] = {
                            "action_id": next_actions[0].id,
                            "revision": card_revision,
                            "generation": object(),
                            "payload": dict(next_actions[0].payload),
                        }
            return sent
        except (Exception, asyncio.CancelledError) as exc:
            if model is not None:
                for action in next_actions:
                    state.get("workflow_confirmations", {}).pop(action.id, None)
            if stage != "mesh_entry":
                raise
            logger.warning("product_upload_card_update_failed job_id=%s", job_id)
            try:
                await cl.Message(content=_workflow_status(_UPLOAD_UPDATE_FAILED)).send()
            except Exception as notice_error:
                logger.warning("product_upload_card_notice_failed")
            if isinstance(exc, asyncio.CancelledError):
                raise
            return None


def _preview_cache(job_id):
    holder = cl.user_session.get("product_preview_cache")
    if holder is None or holder["job_id"] != job_id:
        if holder is not None:
            holder["cache"].clear()
        holder = {"job_id": job_id, "cache": PreviewCache()}
        cl.user_session.set("product_preview_cache", holder)
    return holder["cache"]


async def _media_url(key, png):
    """Reuse session media; byte-LRU eviction must not break chat history."""
    media = cl.user_session.get("product_card_media")
    if media is None:
        media = {}
        cl.user_session.set("product_card_media", media)
    key = canonical_sha256({'source': key, 'png_sha256': hashlib.sha256(png).hexdigest()})
    if key not in media:
        picture = cl.Image(name="preview.png", content=png, display="inline")
        await picture._create()
        media[key] = picture.url or (f"/project/file/{picture.chainlit_key}"
                                     f"?session_id={cl.context.session.id}")
    return media[key]


async def _patch_card(target, patch):
    if target is None:
        return False
    card = target["card"]
    async with card["lock"]:
        if not _preview_target_current(target):
            return False
        model = card["model"]
        changes = patch(model) if callable(patch) else patch
        props = {**model, **changes}
        await update_component(card["component"], props)
        card["model"] = props
        return _preview_target_current(target)


async def _candidate_feedback(job_id, status, message, expected_revision=None):
    state = _job_ui_state(job_id)
    state['selection_running'] = status == 'pending'
    for stage in ("candidates", "mesh_entry"):
        card = state["cards"].get(stage)
        if not card or card.get("component") is None:
            continue
        async with card["lock"]:
            if expected_revision is not None and card.get("revision") != expected_revision:
                continue
            props = {**card["model"], "locked": True, "message": message,
                     "confirmationState": status}
            await update_component(card["component"], props)
            card["model"] = props


async def _safe_candidate_feedback(*args):
    """A failed UI transmission never changes a business commit outcome."""
    try:
        await asyncio.wait_for(_candidate_feedback(*args), PRODUCT_UI_SEND_TIMEOUT_SECONDS)
    except Exception:
        logger.warning('candidate_feedback_unavailable job_id=%s', args[0])


def _capture_preview_target(job_id, stage, source_key):
    """Capture one card publication, never a new authorization."""
    from chainlit.context import ChainlitContextException
    try:
        state = cl.user_session.get(f"product_job_ui:{job_id}")
        message = cl.user_session.get(f"product_stage_card:{job_id}:{stage}")
        card = state["cards"].get(stage) if state else None
        if card is None or message is None or cl.user_session.get("product_job_id") != job_id:
            return None
        card["preview_source"] = source_key
        return {"job_id": job_id, "stage": stage, "source": source_key,
                "generation": _session_generation(), "revision": card["revision"],
                "card": card, "publication": card["publication"], "message": message}
    except ChainlitContextException:
        return None


def _preview_target_current(target):
    if target is None:
        return False
    state = cl.user_session.get(f"product_job_ui:{target['job_id']}")
    card = target["card"]
    return (target["generation"] == _session_generation()
            and cl.user_session.get("product_job_id") == target["job_id"]
            and state is not None and state["cards"].get(target["stage"]) is card
            and card.get("publication") is target["publication"]
            and card.get("revision") == target["revision"]
            and card.get("preview_source") == target["source"]
            and cl.user_session.get(f"product_stage_card:{target['job_id']}:{target['stage']}") is target["message"])


async def _update_stage_preview(target, elements, *, note=None, content=None):
    """Replace only preview elements; existing Action objects stay issued/consumed."""
    if target is not None and target["card"].get("component") is not None:
        def failure_patch(model):
            failure = {"previewState": "unavailable", "previewMessage": note or content}
            if model.get("kind") != "candidates":
                return failure
            return {**failure, "candidates": [
                {**cell, **failure} if cell.get("previewState") == "preparing" else cell
                for cell in model["candidates"]
            ]}
        return await _patch_card(target, failure_patch)
    if target is None:
        return False
    card, message = target["card"], target["message"]
    async with card["lock"]:
        if not _preview_target_current(target):
            return False
        previous = list(card.get("preview_elements") or [])
        for element in previous:
            await element.remove()
            if not _preview_target_current(target):
                return False
        base = [element for element in getattr(message, "elements", []) if element not in previous]
        card["preview_elements"] = list(elements)
        message.elements = [*elements, *base]
        message.content = _workflow_status(content) if content is not None else card["base_content"]
        if note:
            message.content = message.content.replace("Preview: preparing unit-cell views. ", "")
            message.content += "\n\n" + note
        # Chainlit 2.9.6 update sends only Actions whose forId is None.
        # Retain the already-published objects, and do not call _issue_* here.
        await message.update()
        for element in elements:
            if getattr(element, "persisted", False) and getattr(element, "type", None) == "image":
                element.content = None  # media storage owns sent bytes, cache owns reuse
        return _preview_target_current(target)


def _start_preview_task(target, operation):
    if target is None:
        return
    async def run():
        try:
            await operation()
        except asyncio.CancelledError:
            raise
        except (PreviewBusy, PreviewTimeout) as exc:
            note = ("Preview is busy. Refresh Job status later." if isinstance(exc, PreviewBusy)
                    else "Preview preparation timed out. Refresh Job status to try again.")
            await _update_stage_preview(target, [], note=note)
        except PreviewResourceError:
            await _update_stage_preview(target, [], note="This result exceeds the image preview size limit. The saved result files are unchanged; open 3D results to view the original preview or download a model.")
        except PreviewSourceError:
            await _update_stage_preview(target, [], note="Preview source could not be verified. Check the recorded candidate data.")
        except Exception:
            logger.error("product_preview_failed job_id=%s stage=%s", target["job_id"], target["stage"])
            await _update_stage_preview(target, [], note="Preview is unavailable. The recorded task state is unchanged.")
    tasks = cl.user_session.get("product_preview_tasks")
    if tasks is None:
        tasks = set()
        cl.user_session.set("product_preview_tasks", tasks)
    task = asyncio.create_task(run())
    tasks.add(task)
    target["card"]["preview_task"] = task
    def finished(completed):
        tasks.discard(completed)
        if not completed.cancelled():
            # A disconnected transport can fail while publishing the error note.
            error = completed.exception()
            if error is not None:
                logger.warning("product_preview_delivery_failed")
    task.add_done_callback(finished)


async def _send_runtime_status(
    content: str, *, job_id: str | None = None,
    card_revision: int | None = None, terminal: bool = False, evidence=None,
) -> bool:
    try:
        await asyncio.wait_for(
            (_send_job_stage_card(job_id, "conformal", content, card_revision=card_revision, terminal=terminal, evidence=evidence)
             if job_id else cl.Message(content=_workflow_status(content), elements=_technical_elements(evidence)).send()),
            timeout=PRODUCT_UI_SEND_TIMEOUT_SECONDS,
        )
        return True
    except asyncio.TimeoutError:
        logger.warning("product_runtime_status_send_timed_out")
    except Exception as exc:
        logger.warning("product_runtime_status_send_failed")
    return False


def configure_product_ui(
    config: LatticeGPTConfig, *, preflight: PreflightBindingV1
) -> None:
    global _CONFIG, _PREFLIGHT, _WORKFLOW, _QUERY, _JOB_STORE
    if not isinstance(config, LatticeGPTConfig):
        raise TypeError("config must be LatticeGPTConfig")
    if not isinstance(preflight, PreflightBindingV1) or preflight.profile != "full_product":
        raise TypeError("full_product PreflightBindingV1 is required")
    _CONFIG = config
    _PREFLIGHT = preflight
    _WORKFLOW = None
    _QUERY = None
    _JOB_STORE = None


def reset_product_ui_for_tests() -> None:
    global _CONFIG, _PREFLIGHT, _WORKFLOW, _QUERY, _JOB_STORE, _RUNTIME_RECONCILED
    _CONFIG = None
    _PREFLIGHT = None
    _WORKFLOW = None
    _QUERY = None
    _JOB_STORE = None
    _RUNTIME_RECONCILED = False


def _product_config() -> LatticeGPTConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_latticegpt_config()
    return _CONFIG


def _workflow():
    global _WORKFLOW
    if _WORKFLOW is None:
        if _PREFLIGHT is None:
            raise RuntimeError("product UI preflight is not configured")
        _WORKFLOW = create_product_workflow_service(
            _product_config(), preflight=_PREFLIGHT
        )
    return _WORKFLOW


def _query_service():
    global _QUERY
    if _QUERY is None:
        if _PREFLIGHT is None:
            raise RuntimeError("product UI preflight is not configured")
        _QUERY = create_formal_query_service(_product_config(), preflight=_PREFLIGHT)
    return _QUERY


def _job_store():
    global _JOB_STORE
    if _JOB_STORE is None:
        if _PREFLIGHT is None:
            raise RuntimeError("product UI preflight is not configured")
        _JOB_STORE = JobStore(_product_config().paths.runtime_root)
    return _JOB_STORE


def _viewer_job_url(job_id: str) -> str:
    return f"{_product_config().services.viewer_frontend_origin}/product.html?job_id={job_id}"



async def reconcile_product_runtime_state() -> list[str]:
    global _RUNTIME_RECONCILED
    with _RUNTIME_RECONCILE_LOCK:
        if _RUNTIME_RECONCILED:
            return []
        _RUNTIME_RECONCILED = True
    try:
        return await asyncio.to_thread(
            reconcile_interrupted_product_jobs,
            _job_store(),
            _RUNTIME_MANAGER,
        )
    except Exception:
        with _RUNTIME_RECONCILE_LOCK:
            _RUNTIME_RECONCILED = False
        raise


def _current_model_runtime_profile(*, prompt_template_version: str = "query-plan-prompt-v1") -> dict:
    provider = cl.user_session.get("provider")
    model_name = cl.user_session.get("model_name")
    session_bound = model_name is not None
    return resolve_model_runtime_profile(
        provider=provider,
        model_name=model_name,
        selection_source="session_selection" if session_bound else "central_default",
        session_bound=session_bound,
        prompt_template_version=prompt_template_version,
    )


def is_product_design_intent(text: str) -> bool:
    value = " ".join(str(text or "").strip().lower().split())
    if not value:
        return False
    if re.search(r"不要|别|不想|无需|不开始|不设计|不用|解释|是什么", value):
        return False
    if re.search(r"(?:查询|查看).*(?:新版\s*)?180w", value):
        return False
    design_intent = re.search(
        r"设计|生成|推荐|逆向|design|generate|recommend|inverse",
        value,
    )
    product_subject = re.search(
        r"晶格|单胞|植入|骨|刚度|共形|lattice|cell|implant|stiffness|conformal",
        value,
    )
    return bool(design_intent and product_subject)


def invalidate_product_chat() -> None:
    tools = cl.user_session.get("product_chat_tools")
    if tools is not None:
        tools.invalidate()
    cl.user_session.set("product_chat_tools", None)
    cl.user_session.set("product_chat_agent", None)


def _chat_read_job(job_id):
    return _job_store().get_consistent(job_id)


def _chat_read_result(job_id):
    from .artifacts import resolve_product_package, resolve_product_package_file, PRODUCT_FILE_MAP, LEGACY_FILE_KEYS
    store = _readonly_viewer_store()
    job = store.get_consistent_for_viewer(job_id)
    package = resolve_product_package(store, job_id, _product_config().paths.conformal_root)
    keys = PRODUCT_FILE_MAP if package.package_metadata is not None else LEGACY_FILE_KEYS
    files = {}
    for key in keys:
        path = resolve_product_package_file(package, key)
        files[key] = {"size_bytes": path.stat().st_size}
    return {"status": "verified", "job_id": job_id, "revision": job["revision"], "package_id": package.package_id,
            "p13_status": (package.compatibility or {}).get("p13_status", "PACKAGE_CONTRACT_VALIDATED"),
            "selected_candidate_id": job["selection"]["selected_candidate_id"],
            "summary": {key: package.metadata[key] for key in ("counts", "statistics", "geometry_summary", "warnings", "errors") if key in package.metadata},
            "files": files, "viewer_url": _viewer_job_url(job_id), "viewer_display_verified": False,
            "non_claims": ["NOT_MECHANICAL_PASS", "NOT_FEA_VALIDATION", "NOT_MANUFACTURING_VALIDATION", "NOT_FULL_DUAL_SPACE_FUSION"]}


def _product_chat_tools():
    tools = cl.user_session.get("product_chat_tools")
    case = cl.user_session.get("product_chat_case")
    if case is None:
        case = ChatCase()
        cl.user_session.set("product_chat_case", case)
    generation = _session_generation()
    if tools is None or tools.session_id != generation:
        if tools is not None:
            tools.invalidate()
        tools = ChatTools(query_service=_query_service, read_job=_chat_read_job, read_result=_chat_read_result,
                          current_job_revision=lambda: cl.user_session.get("product_job_revision"),
                          current_job_id=lambda: cl.user_session.get("product_job_id"), session_id=generation, case=case)
        cl.user_session.set("product_chat_tools", tools)
    tools.set_pending_csv_target(cl.user_session.get('product_pending_csv_target'))
    return tools


def _chat_trace(event, **fields):
    try:
        case = cl.user_session.get("product_chat_case")
    except Exception:
        return  # A production callback can outlive its UI connection.
    if case is not None:
        case.trace(event, session_generation=_session_generation(), **fields)


def _chat_model(messages):
    case = cl.user_session.get('product_chat_case')
    dispatch = dict(CHAT_DISPATCH.get() or {})
    generation = _session_generation()
    def trace(event, **fields):
        if case is not None:
            case.trace(event, session_generation=generation, **dispatch, **fields)
    profile = _current_model_runtime_profile(prompt_template_version="chat-agent-auto-query-v3")
    _chat_trace("model_profile", **(CHAT_DISPATCH.get() or {}), profile={key: profile.get(key) for key in
                ("provider", "model", "prompt_template_version", "profile_sha256")})
    llm = get_chat_llm(provider=profile["provider"], model_name=profile["model"], temperature=0, purpose="chat_agent")
    return invoke_chat_action(llm, [SystemMessage(content=messages[0]["content"]),
                                  HumanMessage(content=json.dumps(messages[1:], ensure_ascii=False, allow_nan=False))], trace=trace)


async def _present_chat_confirmation(result):
    action = result["action"]
    labels = {"create_job": "Create Job with this target", "confirm_target_and_generate": "Confirm target and generate 3 candidates",
              "select_candidate": "Confirm candidate", "request_mesh_upload": "Review mesh requirements and upload", "start_conformal": "Start conformal generation"}
    content = "Waiting for confirmation · " + labels[action]
    if result.get("target"):
        target = result["target"]
        source = _target_source_text(target)
        if source:
            content += "\n\n" + source.rstrip()
        content += "\n\n" + stiffness_table(target["physical_9d_gpa"])
        content += "\nConfirmation creates a Job with this proposed target. Candidate generation requires a separate confirmation."
    if action == "select_candidate":
        content += "\nConfirming records your selection. Mesh upload and generation remain separate steps."
    if result.get("candidate_id"):
        candidate_id = result['candidate_id']
        tools = cl.user_session.get('product_chat_tools')
        pending = tools.pending.get(result.get('confirmation_ref'), {}) if tools else {}
        job = pending.get('job') or {}
        candidate = next((c for c in (job.get('candidate_set') or {}).get('candidates', [])
                          if c.get('candidate_id') == candidate_id), {})
        rank = f"Rank {candidate['rank']} · " if candidate.get('rank') is not None else ''
        labels[action] = f'Confirm {rank}{candidate_id}'
        content += f"\n\n**Selected object: {rank}{candidate_id}**"
    if result.get('job_id'):
        content += f"\n\nTask · {result['job_id'][-8:]}"
    await cl.Message(content=content, actions=[cl.Action(name="product_chat_confirm",
        payload={"token": result["confirmation_ref"]}, label=labels[action])]).send()


async def _present_chat_tool(name, result):
    if name in {"describe_database", "query_records", "count_records"}:
        await _show_database_result(result, question="This natural-language query")
    elif result.get("status") == "awaiting_confirmation":
        if name == 'prepare_design_target':
            tools = _product_chat_tools()
            ref = result['confirmation_ref']
            if tools.pending.get(ref, {}).get('state') != 'issued':
                raise ChatToolError('This target proposal was superseded before display.')
            retired_messages = _revoke_target_proposals(keep_ref=ref)
            # Capture our own synchronous retirement before UI awaits. A later
            # CSV selection must still invalidate this Agent turn.
            retired_csv_context_version = tools.csv_context_version
            await _show_retired_target_proposals(retired_messages)
            if (cl.user_session.get('product_chat_tools') is not tools
                    or tools.pending.get(ref, {}).get('state') != 'issued'):
                raise ChatToolError('This target proposal was superseded before display.')
        await _present_chat_confirmation(result)
        if name == 'prepare_design_target':
            return {'retired_csv_context_version': retired_csv_context_version}
    elif name == "get_design_result":
        # Reading is not publication. The final reply checks the Job evidence
        # before displaying either a verified package or a read failure.
        return


def _trace_receipt(event, receipt, **fields):
    # Confirmation payload, intent and bindings remain in the in-session record.
    facts = {k: receipt.get(k) for k in ("action", "job_id", "revision", "status", "failed")}
    _chat_trace(event, receipt=facts, **fields)


async def _run_product_chat(text, *, receipt=None):
    lock = cl.user_session.get("product_chat_turn_lock")
    if lock is None:
        lock = asyncio.Lock()
        cl.user_session.set("product_chat_turn_lock", lock)
    if receipt is None and lock.locked():
        await cl.Message(content="The previous request is still being processed. This new request has not been processed; any requested change or cancellation has not taken effect. Send it again after the current turn finishes.").send()
        return {"status": "busy"}
    async with lock:
        if receipt is not None:
            if receipt["session_generation"] != _session_generation() or receipt["job_id"] != cl.user_session.get("product_job_id"):
                _trace_receipt("receipt_rejected", receipt, reason="stale_session_or_job")
                return {"status": "stale"}
            job = await asyncio.to_thread(_chat_read_job, receipt["job_id"])
            if receipt["session_generation"] != _session_generation() or receipt["job_id"] != cl.user_session.get("product_job_id"):
                _trace_receipt("receipt_rejected", receipt, reason="changed_during_read")
                return {"status": "stale"}
            receipt = {**receipt, "revision": job["revision"], "status": job["status"],
                       "failed": bool(receipt.get("failed")) or bool(job.get("failure")) or job["status"] in {"FAILED", "HOLD_RUNTIME_INTERRUPTED"},
                       "user_intent": cl.user_session.get("product_chat_intent")}
            key = f"{receipt['action']}:{receipt['job_id']}:{receipt['revision']}"
            receipts = cl.user_session.get("product_chat_receipts") or []
            if key in receipts:
                _trace_receipt("receipt_rejected", receipt, reason="duplicate")
                return {"status": "duplicate"}
            _trace_receipt("receipt_received", receipt)
        else:
            cl.user_session.set("product_chat_intent", str(text)[:8000])
        result = await _run_product_chat_locked(text, receipt=receipt)
        if receipt is not None:
            cl.user_session.set("product_chat_receipts", (receipts + [key])[-32:])
        return result


async def _run_product_chat_locked(text, *, receipt=None):
    tools = _product_chat_tools()
    agent = cl.user_session.get("product_chat_agent")
    if agent is None or agent.tools is not tools:
        agent = ChatAgent(_chat_model, tools, on_tool=_present_chat_tool)
        cl.user_session.set("product_chat_agent", agent)
    # Settings can change while this session continues to reuse the same agent.
    agent.timeout_seconds = chat_agent_timeout_seconds(cl.user_session.get("provider"))
    cl.user_session.set("product_chat_enabled", True)
    history_before = copy.deepcopy(agent.history)
    result = await agent.run(text, receipt=receipt)
    evidence = result.get('job_evidence')
    controlled = result.get('answer_origin') == 'program_job_facts'
    current = not controlled
    if evidence is not None:
        try:
            current = await tools.job_evidence_current(evidence)
        except Exception:
            current = False
    snapshot = tools.snapshot()
    if controlled and current:
        job = snapshot.get('job') or {}
        current = (isinstance(evidence, dict)
                   and job.get('job_id') == evidence.get('job_id')
                   and job.get('revision') == evidence.get('revision')
                   and job.get('job_sha256') == evidence.get('sha256'))
        package = result.get('design_result')
        if package is not None:
            current = (current and isinstance(package, dict) and package.get('status') == 'verified'
                       and package.get('job_id') == evidence.get('job_id')
                       and package.get('revision') == evidence.get('revision'))
    if not current or result.get("display_read_stale"):
        agent.history = history_before
        result = {'status': 'stale', 'display_read_stale': bool(result.get('display_read_stale'))}
        result['text'] = recorded_chat_reply(result, snapshot)
    status = result.get("status")
    if status == "waiting_for_user":
        return result  # The tool already sent the confirmation card.
    if (status == "answered" and receipt and not receipt.get("failed")
            and receipt["action"] == "select_candidate"):
        return result  # The callback already presented the candidate stage or the selection receipt.
    comparison = result.get("workflow_intent") == "compare_candidates"
    if result.get("text") or comparison or result.get("display_read") or status == "needs_clarification":
        try:
            content = recorded_chat_reply(result, snapshot)
            basis = result.get("answer_basis")
            if result.get("display_read") and status != "stale":
                clarification = content if (
                    status == "needs_clarification" and result.get("clarification_origin") == "model"
                    or status == "answered" and result.get("explanation_requested") is True
                ) else None
                content = _database_turn_outcome(result["display_read"], status, clarification)
                if not content:
                    return result  # The result card already contains the complete reply.
            elif not controlled and status == "needs_clarification" and result.get("query_state", {}).get("status") == "not_queried":
                content = "Clarification needed · No database query in this turn\n\n" + content
            elif (not controlled and (basis == "conversation" or (basis == "workflow" and not snapshot.get("job"))) and not comparison
                    and result.get("query_state", {}).get("status") == "not_queried"):
                label = "Workflow explanation" if basis == "workflow" else "Chat explanation"
                content = label + " · No database query in this turn\n\n" + content
            elements = []
            if status == "answered" and comparison:
                physical = ((snapshot.get("job") or {}).get("target") or {}).get("physical_9d_gpa")
                if physical:
                    label = "View target stiffness (GPa)"
                    content += "\n\n" + label
                    elements.append(_side_text(label, stiffness_table(physical)))
            package = result.get('design_result')
            if status == 'answered' and result.get('answer_origin') == 'program_job_facts' and package:
                if package.get('viewer_url'):
                    content += f"\n\n[Open 3D results]({package['viewer_url']})"
            await cl.Message(content=content, elements=elements).send()
        except BaseException:
            agent.history = history_before
            raise
    return result


async def _continue_product_chat(action, job_id, *, generation=None, failed=False):
    try:
        enabled = cl.user_session.get("product_chat_enabled")
        current = cl.user_session.get("product_job_id")
    except Exception:
        return  # A callback may finish after its UI connection has gone away.
    if not enabled or current != job_id:
        return
    generation = generation or _session_generation()
    if generation is not None and generation != _session_generation():
        return
    try:
        job = await asyncio.to_thread(_chat_read_job, job_id)
        if generation != _session_generation() or cl.user_session.get("product_job_id") != job_id:
            return
        await _run_product_chat("请依据执行回执说明当前结果和下一步；不要重复已经执行的动作。面向用户使用英文，不要求用户手写工具名或JSON。", receipt={
            "action": action, "job_id": job_id, "revision": job["revision"], "status": job["status"], "session_generation": generation,
            "failed": failed or bool(job.get("failure")) or job["status"] in {"FAILED", "HOLD_RUNTIME_INTERRUPTED"}})
    except Exception:
        logger.error("product_chat_receipt_failed job_id=%s action=%s", job_id, action)
        await cl.Message(content="The Job record is authoritative. The chat explanation did not complete, and the action will not run again. Reopen the Job to check its state.").send()


@cl.action_callback("product_chat_confirm")
async def confirm_chat_action(action: cl.Action):
    payload = action.payload or {}
    record = None
    try:
        if set(payload) != {"token"}:
            raise ChatToolError("The confirmation parameters are invalid.")
        tools = _product_chat_tools()
        record = await tools.consume_action(payload["token"])
        kind, job = record["action"], record["job"]
        _chat_trace("ui_confirmation", run_id=record["issued_run_id"], action=kind,
                    job_id=record["current_job_id"], issued_run_id=record["issued_run_id"])
        if kind == "create_job":
            state = _creation_state()
            creation = {"state": "CREATING", "action_id": payload["token"]}
            async with state["lock"]:
                if state.get("busy") is not None:
                    raise ChatToolError("A target is being created or its write needs verification. Do not create it again.")
                state["busy"] = creation
                retired_messages = _revoke_target_proposals()
            try:
                await _create_and_show_target(record["target"], creation_action=creation)
                await _show_retired_target_proposals(retired_messages)
            finally:
                await _release_completed_creation(creation)
            await _continue_product_chat(kind, cl.user_session.get("product_job_id"))
        elif kind == "request_mesh_upload":
            await send_product_mesh_entry_card(job)
            await _continue_product_chat(kind, job["job_id"])
        else:
            args = {"job_id": job["job_id"], "expected_revision": job["revision"]}
            if kind == "select_candidate":
                args.update(candidate_id=record["candidate_id"], expected_candidate_set_sha256=job["candidate_set"]["candidate_set_sha256"])
            callback = {"confirm_target_and_generate": confirm_target, "select_candidate": select_candidate, "start_conformal": start_conformal}[kind]
            trusted_action = SimpleNamespace(payload=args, id=payload["token"])
            _issue_workflow_confirmation(trusted_action, {"confirm_target_and_generate": "product_confirm_target",
                "select_candidate": "product_select_candidate", "start_conformal": "product_start_conformal"}[kind])
            await callback(trusted_action)
        _chat_trace("ui_callback_returned", run_id=record["issued_run_id"], action=kind,
                    job_id=cl.user_session.get("product_job_id"), outcome="consult_job_and_receipt")
    except Exception as exc:
        _chat_trace("ui_confirmation_rejected", run_id=record.get("issued_run_id") if record else None,
                    reason="action_rejected")
        if isinstance(exc, ChatToolError):
            logger.info("product_chat_confirmation_rejected reason=guard_rejected")
        else:
            logger.warning("product_chat_confirmation_rejected")
        await cl.Message(content=_format_product_error("Chat action confirmation", exc, write_state="Check the recorded Job events", retry_state="No automatic retry. Reopen the Job"), elements=_technical_elements(_product_error_evidence(exc))).send()


def _planner(prompt: str):
    profile = _current_model_runtime_profile()
    llm = get_chat_llm(
        provider=profile["provider"],
        model_name=profile["model"],
        temperature=0,
    )
    messages = [
        SystemMessage(content="You are a fail-closed JSON query planner. Return JSON only."),
        HumanMessage(content=prompt),
    ]
    result = invoke_json_object(llm, messages)
    result["model_runtime_profile_sha256"] = profile["profile_sha256"]
    return result


def _validation_label(value):
    return _choice(value, ("PASS", "HOLD", "FAIL", "SCALER_RANGE", "P09", "BOTH", "INVALID_CONVERSION",
                           "HOLD_INVALID_CONVERSION", "HOLD_OUT_OF_RANGE", "strict_float32_C_over_Eb_v1",
                           "INVALID_FLOAT32_CONVERSION", "P09_NUMERIC_EVALUATION_FAILED", "NONFINITE_P09_RESULT",
                           "UNCHANGED", "IN_RANGE", "ROUNDS_TO_BOUNDARY", "ROUNDED_TO_BOUNDARY", "ROUNDED_INTO_RANGE",
                           "ROUNDED_OUT_OF_RANGE", "ROUNDED_WITHIN_RANGE", "REMAINS_OUT_OF_RANGE"))


def _target_validation_evidence(target: dict | None) -> str:
    target = target or {}
    check = target.get("range_check") or {}
    p09 = target.get("p09_validation") or {}
    if not check:
        return _DisplayDetails("This historical Job has no recorded component-level range evidence. This view does not recompute or add it.")

    def number(value):
        return repr(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else "Not recorded"

    lines = ["", "", "**Target validation evidence**", "",
             f"- Scaler range: `{_validation_label(check.get('status'))}`; P-09: `{_validation_label(p09.get('status'))}`.",
             f"- Criterion: `{_validation_label(check.get('criterion'))}`; failure classification: `{_validation_label(target['validation_failure_kind']) if target.get('validation_failure_kind') else 'None'}`.",
             "- Comparisons use dimensionless C/Eb. The original target is preserved; float32 values are the numerical representation used for inference.",
             "", "| Component | Original C/Eb | float32 C/Eb | Minimum | Maximum | Original delta from min / max | float32 delta from min / max | Status / rounding |",
             "| --- | ---: | ---: | ---: | ---: | --- | --- | --- |"]
    if p09.get("evaluated") is False:
        lines.insert(5, f"- P-09 not evaluated: `{_validation_label(p09.get('error_code'))}`; status remains HOLD.")
    fields = {item["field"]: item for item in check.get("fields", []) if isinstance(item, dict)
              and item.get("field") in STIFFNESS_NAMES}
    for name in STIFFNESS_NAMES:
        item = fields.get(name, {})
        values = [name, number(item.get("raw_value_C_over_Eb")),
                  number(item.get("effective_value_C_over_Eb")), number(item.get("minimum")),
                  number(item.get("maximum")),
                  f"{number(item.get('raw_delta_from_minimum'))} / {number(item.get('raw_delta_from_maximum'))}",
                  f"{number(item.get('effective_delta_from_minimum'))} / {number(item.get('effective_delta_from_maximum'))}",
                  f"{_validation_label(item.get('status'))} / {_validation_label(item.get('rounding_classification'))}"]
        lines.append("| " + " | ".join(values) + " |")
    return _DisplayDetails("\n".join(lines))


def _target_details(target):
    target = target or {}
    values = target.get("physical_9d_gpa") or {}
    lines = [f"{name} = {values[name]} GPa" for name in STIFFNESS_NAMES if type(values.get(name)) in (float, int)]
    source = _target_source_text(target)
    if source:
        lines.append(source.rstrip())
    lines.append(_target_validation_evidence(target))
    return _DisplayDetails("\n".join(lines))


def _target_card(target: dict, job_id: str) -> str:
    return (
        "[Workflow status] ### 9D target confirmation\n\n"
        f"- Source: {_choice((target.get('source') or {}).get('kind'), ('csv_upload', 'formal_150w_database', 'manual'))}\n"
        + _target_source_text(target)
        +
        "- Material: TC4; Eb = 110 GPa\n"
        + "\n" + stiffness_table(target.get("physical_9d_gpa")) + "\n\n"
        "The target is ready. Review it and select Confirm target and generate 3 candidates. Inverse design starts only after confirmation."
    )


async def _create_and_show_target(target: dict, *, creation_action: dict) -> None:
    generation = _session_generation()
    previous_job = cl.user_session.get('product_job_id')
    async def create_and_remember():
        try:
            job = await asyncio.to_thread(_workflow().create_job, target,
                                         model_runtime_profile=_current_model_runtime_profile())
        except BaseException:
            creation_action["state"] = "CREATE_WRITE_UNCERTAIN"
            _set_csv_creation_status(creation_action, 'CREATE_WRITE_UNCERTAIN')
            logger.error("product_target_create_uncertain")
            raise
        # Persist the session mapping before the first UI await after create.
        creation_action.update(state="CREATED", job_id=job["job_id"], revision=job["revision"])
        if generation == _session_generation() and cl.user_session.get('product_job_id') == previous_job:
            cl.user_session.set("product_job_id", job["job_id"])
            cl.user_session.set("product_job_revision", job["revision"])
            _set_pending_csv_target(None)
        logger.info("product_target_created job_id=%s revision=%s", job["job_id"], job["revision"])
        if creation_action.get('ui_detached'):
            creation_action['state'] = 'CREATED_UI_PENDING'
            await _release_completed_creation(creation_action)
        return job
    task = asyncio.create_task(create_and_remember())
    task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())
    try:
        # Own the accepted write before any cancellable UI transmission.
        await _csv_creation_feedback(creation_action, 'creating')
        job = await asyncio.shield(task)
    except asyncio.CancelledError:
        creation_action['ui_detached'] = True
        await _release_completed_creation(creation_action)
        raise
    if generation != _session_generation() or cl.user_session.get('product_job_id') != job['job_id']:
        creation_action['state'] = 'CREATED_UI_PENDING'
        raise RuntimeError('The Job was created after the current task changed. Reopen the recorded Job.')
    await invalidate_product_waits()
    try:
        await _show_target_card(target, job)
    except (Exception, asyncio.CancelledError):
        creation_action["state"] = "CREATED_UI_PENDING"
        logger.error("product_target_ui_pending job_id=%s", job["job_id"])
        raise


async def _show_target_card(target: dict, job: dict) -> None:
    await _show_job_summary(job)
    await _send_job_stage_card(
        job["job_id"],
        "target",
        _target_card(target, job["job_id"]),
        card_revision=job["revision"],
        evidence=_target_details(target),
        actions=[cl.Action(
            name="product_confirm_target",
            payload={"job_id": job["job_id"], "expected_revision": job["revision"]},
            label="Confirm target and generate 3 candidates",
        )],
    )


def _creation_state():
    state = cl.user_session.get("product_creation_actions")
    if state is None:
        state = {"lock": asyncio.Lock(), "actions": {}}
        cl.user_session.set("product_creation_actions", state)
    return state


def _revoke_target_proposals(*, keep_ref=None, keep_record=None):
    """Revoke execution rights synchronously, before any UI await or new write."""
    tools = cl.user_session.get('product_chat_tools')
    if tools is not None:
        tools.retire_target_proposals(keep_ref=keep_ref)
    messages = []
    current_import = _csv_import_state(create=False)
    for record in _creation_state()['actions'].values():
        if record is keep_record or record['state'] != 'ISSUED':
            continue
        record['state'] = 'RETIRED'
        if current_import and record.get('csv_import_token') == current_import['import_token']:
            _set_pending_csv_target(None)
        if record.get('message') is not None:
            messages.append(record['message'])
    return messages


async def _show_retired_target_proposals(messages):
    for message in messages:
        message.content = '**Historical · Superseded target proposal**\n\n' + message.content
        for action in message.actions:
            try:
                await asyncio.wait_for(action.remove(), PRODUCT_UI_SEND_TIMEOUT_SECONDS)
            except Exception:
                logger.info('retired_target_action_display_unavailable')
        message.actions = []
        try:
            await asyncio.wait_for(message.update(), PRODUCT_UI_SEND_TIMEOUT_SECONDS)
        except Exception:
            logger.info('retired_target_display_unavailable')


async def _retire_target_proposals(*, keep_ref=None):
    await _show_retired_target_proposals(_revoke_target_proposals(keep_ref=keep_ref))


async def _release_completed_creation(record):
    if record.get('state') in {'CREATED', 'CREATED_UI_PENDING'}:
        state = _creation_state()
        async with state['lock']:
            if state.get('busy') is record:
                state['busy'] = None


async def _offer_target(target: dict, *, csv_import_token=None) -> None:
    token = secrets.token_hex(24)
    current = cl.user_session.get("product_job_id")
    record = {"state": "ISSUED", "target": target, "current_job_id": current,
              "issued_at": time.monotonic(), "session_generation": _session_generation(),
              "action_id": canonical_sha256({"token": token})}
    if csv_import_token is not None:
        record['csv_import_token'] = csv_import_token
    _creation_state()["actions"][token] = record
    label = "Create new Job; keep current Job" if current else "Create Job with this target"
    message = cl.Message(content=("### Proposed target · Not created\n\n" + _target_source_text(target) + '\n' + stiffness_table(target["physical_9d_gpa"])
                              + "\n\nConfirmation creates a Job with this proposed target. Candidate generation requires a separate confirmation.\n\nTechnical details"),
                     elements=_technical_elements(_target_details(target)),
                     actions=[cl.Action(name="product_create_target", payload={"token": token}, label=label)])
    record['message'] = message
    try:
        await message.send()
    except BaseException:
        record['state'] = 'RETIRED'
        raise


def _target_source_text(target):
    source = target.get('source') or {}
    if source.get('kind') == 'formal_150w_database' and type(source.get('sample_id')) is int:
        return f"- Reference database sample: {source['sample_id']}\n"
    if not source.get('name'):
        return ''
    record = (f" · Record {source['record_index']} of {source['record_count']}"
              if type(source.get('record_index')) is int and type(source.get('record_count')) is int else '')
    return f"- File: `{_filename(source['name'])}`{record}\n"


def _csv_import_state(*, create=True):
    state = cl.user_session.get('product_csv_import')
    if state is None and create:
        state = {'import_token': secrets.token_hex(16), 'generation': _session_generation(),
                 'page': 1, 'selected_index': None, 'row_actions': {}, 'page_actions': {},
                 'parsed': None, 'component': None, 'message': None}
        cl.user_session.set('product_csv_import', state)
    return state


def _set_pending_csv_target(summary):
    cl.user_session.set('product_pending_csv_target', copy.deepcopy(summary))
    tools = cl.user_session.get('product_chat_tools')
    if tools is not None:
        tools.set_pending_csv_target(summary)


def _set_csv_creation_status(record, status):
    summary = cl.user_session.get('product_pending_csv_target')
    if record.get('csv_import_token') and summary is not None:
        _set_pending_csv_target({**summary, 'state': status})


async def _retire_csv_proposals(import_token):
    for record in _creation_state()['actions'].values():
        if record.get('csv_import_token') != import_token or record['state'] != 'ISSUED':
            continue
        record['state'] = 'RETIRED'
        message = record.get('message')
        if message is not None:
            message.content = '**Historical · Superseded CSV selection**\n\n' + message.content
            for action in message.actions:
                try:
                    await asyncio.wait_for(action.remove(), PRODUCT_UI_SEND_TIMEOUT_SECONDS)
                except Exception:
                    logger.info('csv_old_action_display_unavailable')
            message.actions = []
            try:
                await asyncio.wait_for(message.update(), PRODUCT_UI_SEND_TIMEOUT_SECONDS)
            except Exception:
                logger.info('csv_history_display_unavailable')


async def _retire_csv_import():
    state = _csv_import_state(create=False)
    if state is not None:
        cl.user_session.set('product_csv_import', None)
        await _retire_csv_proposals(state['import_token'])
        component = state.get('component')
        if component is not None:
            try:
                await asyncio.wait_for(update_component(component, {**component.props, 'state': 'history',
                    'historical': True, 'actions': [], 'rows': [{**r, 'action': None} for r in component.props.get('rows', [])]}),
                    PRODUCT_UI_SEND_TIMEOUT_SECONDS)
            except Exception:
                logger.info('csv_history_display_unavailable')
    busy = _creation_state().get('busy')
    if not busy or busy.get('state') not in {'CREATING', 'CREATE_WRITE_UNCERTAIN'}:
        _set_pending_csv_target(None)


def _csv_error_code(error):
    if isinstance(error, PreviewBusy):
        return "CSV_BUSY"
    if isinstance(error, PreviewTimeout):
        return "CSV_TIMEOUT"
    known = {
        "CSV upload must not exceed 5 MB": "CSV_SIZE",
        "target CSV must use UTF-8 encoding": "CSV_ENCODING",
        "target upload must be a CSV file": "CSV_INVALID",
        "geometry CSV is not a stiffness target; upload the supported 9D GPa columns": "CSV_INVALID",
        "target CSV must contain exactly the supported 9D columns without repeated headers": "CSV_INVALID",
        "target CSV is malformed": "CSV_INVALID",
        "target CSV must contain at least one data row": "CSV_EMPTY",
    }
    if len(error.args) == 1 and isinstance(error.args[0], str):
        return known.get(error.args[0], "UNCLASSIFIED")
    return "UNCLASSIFIED"


def _csv_row_error(error):
    if error is None:
        return None
    if error == "Record must contain exactly nine values.":
        return error
    for name in STIFFNESS_NAMES:
        if isinstance(error, str) and error.startswith("physical_9d_gpa." + name + " "):
            return f"{name} must be a finite positive number in GPa."
    return "Record must contain finite positive numbers in all nine columns."


async def _show_csv_picker(state):
    if _csv_import_state(create=False) is not state or state['generation'] != _session_generation():
        return
    parsed = state['parsed']
    page_count = max(1, (parsed['record_count'] + 19) // 20)
    page = state['page']
    row_actions, rows = {}, []
    for row in parsed['rows'][(page-1)*20:page*20]:
        index = row['record_index']
        action = None
        if row['error'] is None:
            action = cl.Action(name='product_csv_select', label=f'Select record {index}',
                               payload={'import_token': state['import_token'], 'record_index': index})
            row_actions[index] = action
        rows.append({'recordIndex': index, 'values': {name: row['values'][name] for name in STIFFNESS_NAMES
                                     if type((row['values'] or {}).get(name)) in (int, float)},
                     'error': _csv_row_error(row['error']),
                     'action': action_dict(action) if action else None})
    page_actions = {number: cl.Action(name='product_csv_page', label=label,
                    payload={'import_token': state['import_token'], 'page': number})
                    for number, label in ((page-1, 'Previous page'), (page+1, 'Next page'))
                    if 1 <= number <= page_count}
    # Register exact identities before the component can receive clicks.
    state.update(row_actions=row_actions, page_actions=page_actions)
    props = {'filename': _filename(parsed['filename']), 'recordCount': parsed['record_count'],
             'validCount': parsed['valid_count'], 'invalidCount': parsed['invalid_count'],
             'page': page, 'pageCount': page_count, 'selectedRecordIndex': state['selected_index'],
             'state': 'active', 'rows': rows, 'actions': [action_dict(a) for a in page_actions.values()],
             'replayLabel': cl.user_session.get('product_replay_label')}
    component = state['component']
    try:
        if component is None:
            component = cl.CustomElement(name='CsvTargetPicker', props=props, display='inline')
            props['componentId'] = component.id
            component.props = props
            state['component'] = component
            state['message'] = cl.Message(content='', elements=[component])
            await asyncio.wait_for(state['message'].send(), PRODUCT_UI_SEND_TIMEOUT_SECONDS)
        else:
            props['componentId'] = component.id
            await asyncio.wait_for(update_component(component, props), PRODUCT_UI_SEND_TIMEOUT_SECONDS)
    except BaseException:
        state.update(row_actions={}, page_actions={})
        raise


def _csv_action_current(action, state, kind):
    payload = action.payload or {}
    if state is None or state['generation'] != _session_generation() or payload.get('import_token') != state['import_token']:
        return False
    key = 'record_index' if kind == 'row_actions' else 'page'
    if set(payload) != {'import_token', key} or type(payload[key]) is not int:
        return False
    expected = state[kind].get(payload[key])
    return expected is not None and expected.id == action.id and expected.payload == payload


async def _begin_csv_import():
    creation = _creation_state()
    async with creation['lock']:
        if creation.get('busy') is not None:
            await cl.Message(content='A target is being created or its result needs verification. Keep that record before importing another CSV.').send()
            return None
        await _retire_csv_import()
        return _csv_import_state()


async def _handle_csv_upload(content, *, filename, state=None):
    if state is None:
        state = await _begin_csv_import()
    if state is None or _csv_import_state(create=False) is not state or state['generation'] != _session_generation():
        return
    creation = _creation_state()
    source = 'csv:' + canonical_sha256({'sha256': hashlib.sha256(content).hexdigest(), 'filename': filename})
    try:
        parsed = await _INPUT_EXECUTOR.run(source, lambda: parse_target_csv_upload(content, filename=filename),
                                           timeout=PRODUCT_PREVIEW_WAIT_SECONDS)
        if _csv_import_state(create=False) is not state or state['generation'] != _session_generation():
            return
        state['parsed'] = parsed
        if parsed['record_count'] == 1 and parsed['valid_count'] == 1:
            async with creation['lock']:
                if _csv_import_state(create=False) is state and creation.get('busy') is None:
                    await _select_csv_record(state, 1)
        else:
            await _show_csv_picker(state)
    except (ValueError, PreviewBusy, PreviewTimeout) as exc:
        if _csv_import_state(create=False) is state:
            code = _csv_error_code(exc)
            await cl.Message(content=_format_product_error("CSV preparation", {"code": code},
                write_state="Check the current target proposal", retry_state="Review the CSV and upload manually")).send()


async def _select_csv_record(state, index, *, explicit_selection=False):
    target = target_from_csv_record(state['parsed'], index)
    if explicit_selection:
        await _retire_target_proposals()
    await _retire_csv_proposals(state['import_token'])
    if _csv_import_state(create=False) is not state or state['generation'] != _session_generation():
        return
    state['selected_index'] = index
    parsed = state['parsed']
    _set_pending_csv_target({key: parsed[key] for key in
        ('filename', 'sha256', 'record_count', 'valid_count', 'invalid_count')} | {
        'record_index': index, 'physical_9d_gpa': target['physical_9d_gpa'], 'state': 'PROPOSED'})
    if state['component'] is not None:
        await _show_csv_picker(state)
    if _csv_import_state(create=False) is not state or state['generation'] != _session_generation():
        return
    await _offer_target(target, csv_import_token=state['import_token'])


@cl.action_callback('product_csv_select')
async def product_csv_select(action):
    async with _creation_state()['lock']:
        state = _csv_import_state(create=False)
        if not _csv_action_current(action, state, 'row_actions') or _creation_state().get('busy') is not None:
            await cl.Message(content='This CSV selection expired or a target creation is pending. Use the current file card.').send()
            return
        await _select_csv_record(state, action.payload['record_index'], explicit_selection=True)


@cl.action_callback('product_csv_page')
async def product_csv_page(action):
    async with _creation_state()['lock']:
        state = _csv_import_state(create=False)
        if not _csv_action_current(action, state, 'page_actions') or _creation_state().get('busy') is not None:
            return
        state['page'] = action.payload['page']
        await _show_csv_picker(state)


@cl.action_callback("product_create_target")
async def create_target(action: cl.Action):
    payload = action.payload or {}
    state = _creation_state()
    record = None
    async with state["lock"]:
        candidate = state["actions"].get(payload.get("token")) if set(payload) == {"token"} else None
        if candidate is not None and state.get("busy") is None and candidate["state"] == "ISSUED" and candidate["session_generation"] == _session_generation() and time.monotonic() - candidate["issued_at"] < 900 and candidate["current_job_id"] == cl.user_session.get("product_job_id"):
            candidate["state"] = "CREATING"
            _set_csv_creation_status(candidate, 'CREATING')
            record = candidate
            state["busy"] = record
            retired_messages = _revoke_target_proposals(keep_record=record)
    if record is None:
        mapped = candidate.get("job_id") if candidate else None
        await cl.Message(content=(f"This action already created a Job. Reopen Job `{mapped}`." if mapped else "This action expired, is being processed, or has a write that needs verification. Do not create the Job again.")).send()
        return
    try:
        await _create_and_show_target(record["target"], creation_action=record)
        await _show_retired_target_proposals(retired_messages)
        await _continue_product_chat("create_job", cl.user_session.get("product_job_id"))
    except Exception:
        await _csv_creation_feedback(record, 'uncertain' if record['state'] == 'CREATE_WRITE_UNCERTAIN' else 'history')
        mapped = record.get("job_id")
        await cl.Message(content=(f"Job `{mapped}` was created, but its page update did not complete. Reopen that Job." if mapped else "The target write needs verification. Preserve the diagnostic record and do not submit again.")).send()
    finally:
        await _release_completed_creation(record)


async def _csv_creation_feedback(record, status):
    state = _csv_import_state(create=False)
    if state is None or state['import_token'] != record.get('csv_import_token') or state['component'] is None:
        return
    try:
        await asyncio.wait_for(update_component(state['component'], {**state['component'].props, 'state': status}),
                               PRODUCT_UI_SEND_TIMEOUT_SECONDS)
    except Exception:
        logger.info('csv_creation_feedback_unavailable')


def _mesh_contract_copy() -> str:
    return (
        f"Upload one original Abaqus INP file, up to {PRODUCT_INP_MAX_SIZE_MB} MB. "
        "Confirm that coordinates are in mm and use the original coordinate system, with no rotation or mirroring. "
        "Review the C3D8/C3D8R domain and excluded regions after upload."
    )


async def send_product_start_card() -> None:
    await _send_job_stage_card(
        "entry",
        "product_start",
        (
            "# BoneMetGPT · Lattice Design for Bone Implants\n\n"
            "Start with a stiffness target, choose a lattice candidate, and map it to your input mesh.\n\n"
            "**1. Set a target → 2. Select a candidate → 3. Confirm the mesh → 4. Generate and view**\n\n"
            "Upload a 9D stiffness CSV in GPa, then choose one row; or ask for reference data in natural language.\n\n"
            "Review each target, candidate and computational domain before confirming. Reopening a Job displays saved records and does not resume computation."
        ),
        evidence=_DisplayDetails("UTF-8 9D CSV in GPa, up to 5 MiB; INP C3D8/C3D8R via the controlled upload entry. "
                                 "Use `/log-health` to check the Chat event log. "
                                 "CSV selection does not invoke the model. NOT_FULL_DUAL_SPACE_FUSION. " + VALIDATION_NOTE),
        actions=[
            cl.Action(
                name="product_start_entry",
                payload={},
                label="Start design",
            ),
            cl.Action(
                name="product_resume_entry",
                payload={},
                label="Reopen Job",
            ),
        ],
    )


async def send_product_mesh_entry_card(job: dict | None = None) -> None:
    if job is None:
        await send_product_start_card()
        return
    ready = bool(
        job
        and job.get("status") == "CANDIDATES_READY"
        and _confirmed_hash(job)
    )
    payload = {}
    if ready:
        payload = {
            "job_id": job["job_id"],
            "expected_revision": job["revision"],
            "confirmation_sha256": _confirmed_hash(job),
            "entry_token": secrets.token_hex(16),
            "session_generation": _session_generation(),
        }
    if ready:
        content = (
            "### Upload a hexahedral mesh\n\n"
            f"{_mesh_contract_copy()}\n\n"
            "The candidate is confirmed. Use the button below to review the mesh requirements, then browse for "
            "one .inp file."
        )
        action_label = "Upload hexahedral mesh (.inp)"
    else:
        raise ValueError("mesh upload entry requires a selected candidate Job")
    await _send_job_stage_card(
        job["job_id"] if job else "entry",
        "mesh_entry",
        content,
        card_revision=job["revision"],
        actions=[cl.Action(
            name="product_mesh_upload_entry",
            payload=payload,
            label=action_label,
        )],
    )


async def _close_mesh_entry(job: dict) -> None:
    try:
        state = _job_ui_state(job["job_id"])
    except Exception:
        return
    if "mesh_entry" in state["cards"]:
        await _send_job_stage_card(
            job["job_id"], "mesh_entry", "The upload entry is closed. Follow the current Job state.",
            card_revision=job["revision"], terminal=True,
        )


async def _refresh_mesh_entry(job_id: str) -> None:
    job = _workflow().get_consistent_job(job_id)
    if job.get("status") == "CANDIDATES_READY" and _confirmed_hash(job):
        await send_product_mesh_entry_card(job)
    else:
        await render_product_job(job, runtime_active=_RUNTIME_MANAGER.is_active(job_id))


async def _show_job_summary(job, *, waiting=None):
    status = job.get("status", "")
    position = (job.get('failure') or {}).get('stage',status) if status=='FAILED' else status
    phase = (1 if position in {"TARGET_PROPOSED", "TARGET_CONFIRMED", "INVERSE_RUNNING", "FORMAL_BUNDLE_PREFLIGHT"}
             else 2 if status == "CANDIDATES_READY" and not _confirmed_hash(job)
             else 3 if status in {"CANDIDATES_READY", "MESH_READY", "AWAITING_CONFORMAL_AUTHORIZATION"}
             else 4)
    text = job_status_text(status, job.get("failure"))
    if status == "CANDIDATES_READY" and _confirmed_hash(job):
        text = "Candidate confirmed · Waiting for your mesh upload"
    await _send_job_stage_card(job["job_id"], "summary", "", card_revision=job["revision"],
        model={"kind":"summary", "phase":phase, "statusText":waiting or text})


def _candidate_details(target, candidates):
    lines = [str(_target_details(target)), "Candidate measurements as recorded (predicted stiffness in GPa)."]
    for candidate in candidates:
        values = candidate.get("predicted_stiffness") or {}
        metrics = ("score", "nmse", "relative_density", "normalized_diameter")
        fields = [f"{key}={candidate[key]}" for key in metrics if type(candidate.get(key)) in (int, float)]
        fields.extend(f"{name}={values[name]}" for name in STIFFNESS_NAMES if type(values.get(name)) in (int, float))
        fields.extend(exclusion_labels(candidate))
        lines.append(f"{candidate['candidate_id']}: " + " · ".join(fields))
    return _DisplayDetails("\n".join(lines))


async def _show_candidates(job: dict) -> None:
    await _show_job_summary(job)
    from .contracts import candidate_ranking_view
    ranking = candidate_ranking_view(job["candidate_set"])
    candidates = sorted(job["candidate_set"]["candidates"], key=lambda item: item["rank"] or 99)
    confirmed = bool(_confirmed_hash(job))
    state = _job_ui_state(job['job_id'])
    running = bool(state.get('selection_running') and state.get('selection_revision') == job['revision'])
    if not running:
        state.pop('selection_pending', None)
        state.pop('selection_running', None)
    selection = job.get("selection") or {}
    selected_id = selection.get("selected_candidate_id") if confirmed else None
    actions, cells = [], []
    for position, item in enumerate(candidates, 1):
        label = f"Rank {item['rank']}" if item["rank"] is not None else f"Unranked candidate {position}"
        action = None
        if item["eligible"]:
            action = cl.Action(name="product_select_candidate", payload={"job_id":job["job_id"],
                "expected_revision":job["revision"], "candidate_id":item["candidate_id"],
                "expected_candidate_set_sha256":job["candidate_set"]["candidate_set_sha256"]},
                label="Confirm this candidate" if item['rank'] == 1 else f"Select rank {item['rank']}")
            actions.append(action)
        cells.append({"id":item["candidate_id"], "name":label, "rank":item["rank"],
            "eligible":item["eligible"], "score":display_number(item.get("score")), "rankingMetric":ranking["metric"],
            "diameter":display_number(item.get("normalized_diameter")),
            "density":display_number(item.get('relative_density')),
            "nmse":display_number(item.get('nmse')),
            "exclusionReasons":exclusion_labels(item),
            "recommended":item["candidate_id"] == selection.get("recommended_candidate_id"),
            "confirmed":item["candidate_id"] == selected_id,
            "previewState":"preparing", "previewUrl":None, "action":action_dict(action) if action else None})
    refresh = _refresh_actions(job)
    rows = [{"name":name, "target":display_number((job.get('target') or {}).get('physical_9d_gpa',{}).get(name)),
             "predictions":[display_number((c.get('predicted_stiffness') or {}).get(name)) for c in candidates]}
            for name in STIFFNESS_NAMES]
    model = {"kind":"candidates", "candidates":cells, "selectedId":selected_id,
             "confirmationState":"pending" if running else "confirmed" if confirmed else "ready", "locked":running,
             "comparison":{"rows":rows, "unit":"GPa"}, "actions":[action_dict(a) for a in refresh],
             "message":"Confirming your selection…" if running else
                f"{ranking['label']} determines rank. Only eligible candidates can be selected."
                if selection else "HOLD: no eligible candidate. Mesh upload is unavailable."}
    await _send_job_stage_card(job["job_id"], "candidates", "", card_revision=job["revision"],
        actions=[*actions,*refresh], model=model,
        evidence=_candidate_details(job.get("target"), candidates))
    snapshot = copy.deepcopy(job["candidate_set"])
    source = canonical_sha256(snapshot)
    target = _capture_preview_target(job["job_id"], "candidates", source)
    async def prepare():
        expected = snapshot.get("candidate_set_sha256")
        if not expected or canonical_sha256({k:v for k,v in snapshot.items() if k!='candidate_set_sha256'}) != expected:
            raise PreviewSourceError("Candidate set identity differs")
        ordered = sorted(snapshot["candidates"], key=lambda item:item["rank"] or 99)
        for item in ordered: item['preview_label']=standard_candidate_label(item)
        cache = _preview_cache(job['job_id'])
        keys = [f'{STANDARD_CELL_RENDERER}:{source}:{item["candidate_id"]}' for item in ordered]
        cached = [cache.get(k) for k in keys]
        if all(value is not None for value in cached):
            from .ui_previews import CandidatePreview
            rendered = [CandidatePreview(c['id'], c['name'], png, None) for c,png in zip(cells,cached)]
        else:
            rendered = await _PREVIEW_EXECUTOR.run('cells:'+source,lambda:render_candidate_previews(ordered),timeout=PRODUCT_PREVIEW_WAIT_SECONDS)
        if [r.candidate_id for r in rendered] != [c['id'] for c in cells]: raise PreviewSourceError('Candidate order differs')
        if not _preview_target_current(target):return
        updated = copy.deepcopy(target['card']['model']['candidates'])
        for cell, picture, key in zip(updated,rendered,keys):
            if picture.png is not None:
                if not picture.error:cache.put(key,picture.png)
                cell['previewUrl']=await _media_url(key,picture.png)
            cell['previewState']='unavailable' if picture.error or picture.png is None else 'ready'
        await _patch_card(target,{'candidates':updated})
    _start_preview_task(target,prepare)
    if confirmed and not running: await send_product_mesh_entry_card(job)


def _readonly_viewer_store():
    from .artifacts import load_viewer_readonly_compatibility
    if _PREFLIGHT is None:
        raise RuntimeError("product UI preflight is not configured")
    config = _product_config()
    return JobStore(config.paths.runtime_root,
        viewer_compatibility=load_viewer_readonly_compatibility(_PREFLIGHT),
        reader_identity={"generation": config.paths.active_generation,
                         "source_manifest_sha256": _PREFLIGHT.source_manifest_sha256,
                         "active_release_binding_sha256": _PREFLIGHT.active_release_binding_sha256})


def _readonly_result_content(job_id, *, include_preview=False):
    from .artifacts import resolve_product_package, resolve_product_package_file
    store = _readonly_viewer_store()
    job = store.get_consistent_for_viewer(job_id)
    package = resolve_product_package(store, job_id, _product_config().paths.conformal_root)
    mesh = package.mesh_view or normalize_job_mesh_binding(job)
    original_upload = mesh.get("original_upload") or (job.get("mesh_binding") or {}).get("original_upload") or {}
    content = (
        "### Conformal results · Read only\n\n"
        f"- Input mesh: `{_filename(mesh['original_filename'])}`\n"
        + (f"- Original upload: `{_filename(original_upload['original_filename'])}`\n" if original_upload.get("original_filename") else "")
        +
        f"- Selected unit cell: `{job['selection']['selected_candidate_id']}`\n"
        "- Geometry has been generated. View the structure and selected unit cell, and download existing files.\n\n"
        f"[Open 3D results]({_viewer_job_url(job_id)})\n\n"
        "Open the Viewer to access the available unit-cell and conformal files. "
        "The launcher includes the Viewer service; open the page to check its display.\n\n"
        "These outputs do not establish Abaqus/FEA, mechanical, or manufacturing validation. `NOT_FULL_DUAL_SPACE_FUSION`"
    )
    if not include_preview:
        return content
    # The package above was fully verified once. Resolve this file against that
    # same object, never rebuild the package just to add its image.
    path = resolve_product_package_file(package, "png")
    png, error = None, None
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("PNG exceeds preview byte budget")
        png = path.read_bytes()
        if (len(png) < 33 or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR"
                or not 0 < int.from_bytes(png[16:20], "big") <= 8192
                or not 0 < int.from_bytes(png[20:24], "big") <= 8192):
            raise ValueError("Unsupported preview PNG header or dimensions")
    except (OSError, ValueError):
        png = None
        error = "Image preview is unavailable. The verified result files remain available."
    return {"content": content, "png": png, "preview_error": error, "revision": job["revision"]}


def _load_result_view(job_id):
    from .artifacts import resolve_product_package, resolve_product_package_file
    store = _readonly_viewer_store()
    job = store.get_consistent_for_viewer(job_id)
    package = resolve_product_package(store, job_id, _product_config().paths.conformal_root)
    mesh = package.mesh_view or normalize_job_mesh_binding(job)
    upload = mesh.get('original_upload') or (job.get('mesh_binding') or {}).get('original_upload') or {}
    selected = job['selection']['selected_candidate_id']
    candidate = next((c for c in job['candidate_set']['candidates'] if c['candidate_id']==selected),{})
    model = {'kind':'result','fileName':_filename(upload.get('original_filename',mesh['original_filename'])),
             'verificationState':'verified',
             'selectedName':f"Rank {candidate.get('rank','—')}", 'previewUrl':None,
             'previewState':'preparing','statusText':'Result generated', 'viewerUrl':_viewer_job_url(job_id),
             'files':[]}
    source = canonical_sha256({'job':job_id,'revision':job['revision'],'package':package.package_id,
                              'attempts':job.get('attempts')})
    return {'revision':job['revision'],'model':model,'package':package,
            'stl_path':resolve_product_package_file(package,'stl'),'source':source}


async def _render_result_picture(view):
    from .ui_previews import render_stl_preview
    from .artifacts import resolve_product_package_file
    def draw():
        path = resolve_product_package_file(view['package'],'stl')
        return render_stl_preview(path)
    return await _PREVIEW_EXECUTOR.run('surface:'+view['source'],draw,timeout=PRODUCT_PREVIEW_WAIT_SECONDS)


def _job_details(job):
    selection = job.get("selection") or {}
    status = candidate_confirmation_status(job)
    lines = [job_status_text(job.get("status"), failure=job.get("failure")),
             f"Candidate confirmation: {status}."]
    selected = selection.get("selected_candidate_id")
    candidates = (job.get("candidate_set") or {}).get("candidates") or []
    match = next((item for item in candidates if selected and item.get("candidate_id") == selected), None)
    if match is not None:
        lines.append(_candidate_details(job.get("target"), [match]))
    else:
        lines.append(_target_details(job.get("target")))
    lines.append(VALIDATION_NOTE)
    return _DisplayDetails("\n\n".join(lines))


async def _show_readonly_result(job):
    await _show_job_summary(job)
    refresh = _refresh_actions(job)
    await _send_job_stage_card(job['job_id'],'conformal','',card_revision=job['revision'],terminal=True,
        actions=refresh, evidence=_job_details(job), model={'kind':'result','previewState':'preparing', 'verificationState':'checking',
        'statusText':'Checking recorded result files','actions':[action_dict(a) for a in refresh]})
    source = canonical_sha256({'job_id':job['job_id'],'revision':job['revision'],'attempts':job.get('attempts')})
    target = _capture_preview_target(job['job_id'],'conformal',source)
    async def prepare():
        try:
            view = await _INPUT_EXECUTOR.run('verify:'+source,lambda:_load_result_view(job['job_id']),timeout=PRODUCT_PREVIEW_WAIT_SECONDS)
        except (PreviewBusy, PreviewTimeout) as exc:
            reason = "File verification is busy." if isinstance(exc, PreviewBusy) else "File verification timed out."
            await _patch_card(target, {
                "verificationState": "review", "statusText": "File verification incomplete",
                "previewState": "unavailable", "previewMessage": reason + " Refresh Job status to check again.",
            })
            return
        except Exception as exc:
            await _patch_card(target,{'previewState':'unavailable','verificationState':'review','statusText':'Result verification needs review',
                'message':f"{recorded_error_code(exc)}: {error_summary(exc)} Refresh Job status."})
            return
        if view['revision'] != target['revision'] or not _preview_target_current(target):return
        if not await _patch_card(target,{**view['model'],'verificationState':'verified','previewState':'preparing'}):return
        cache = _preview_cache(job['job_id']);key='surface:'+view['source']
        picture = cache.get(key)
        if picture is None:
            picture = await _render_result_picture(view)
        if not _preview_target_current(target):return
        cache.put(key,picture)
        url = await _media_url(key,picture)
        await _patch_card(target,{'previewUrl':url,'previewState':'ready','previewMessage':None})
    _start_preview_task(target,prepare)



def _mesh_preview_data(content, cached=None):
    from .mesh_domain import parse_original_mesh, render_mesh_views
    parsed = parse_original_mesh(content)
    return {'views': cached if cached is not None else render_mesh_views(content),
            'originalNodeCount': len(parsed['nodes']), 'originalElementCount': len(parsed['elements'])}


def _restore_mesh_preview(job, cached=None):
    from .config import require_within
    from .mesh_domain import digest, render_mesh_views
    try: mesh=normalize_job_mesh_binding(job,verify_stored_file=True)
    except (ValueError,OSError) as exc:raise PreviewSourceError('Stored mesh could not be verified') from exc
    upload=next(item for item in job['mesh_uploads'] if item['upload_id']==mesh['upload_id'])
    source=require_within(upload['stored_path'],_job_store().job_directory(job['job_id']),label='preview mesh')
    content=source.read_bytes();expected=(mesh.get('original_upload') or {}).get('raw_sha256',mesh['raw_sha256'])
    if digest(content)!=expected:raise PreviewSourceError('Stored upload changed')
    return _mesh_preview_data(content, cached)


def _mesh_model(mesh, *, mode='bound'):
    domain=mesh.get('design_domain') or {}
    upload=mesh.get('original_upload') or {}
    return {'kind':'mesh','mode':mode,'fileName':_filename(upload.get('original_filename',mesh.get('original_filename','INP mesh'))),
        'nodeCount':mesh.get('node_count'),'elementCount':mesh.get('element_count'),
        'selectedNodeCount':domain.get('selected_node_count',mesh.get('node_count')),
        'originalElementCount':domain.get('selected_element_count',0)+domain.get('excluded_element_count',0) if domain else None,
        'selectedCount':domain.get('selected_element_count',mesh.get('element_count')),
        'excludedCount':domain.get('excluded_element_count',0),'excludedSolids':domain.get('excluded_solid_count',0),
        'excludedTypes':_excluded_counts(domain),'quality':_choice(mesh.get('precheck_status'), ('PASS','HOLD','FAIL','passed','failed'), 'Not checked'),
        'views':{},'previewState':'preparing'}


def _start_bound_mesh_preview(job):
    snapshot=copy.deepcopy(job);source=canonical_sha256(snapshot['mesh_binding'])
    target=_capture_preview_target(job['job_id'],'mesh',source)
    async def prepare():
        cache=_preview_cache(job['job_id']);keys={name:f'bound-domain:{source}:{name}' for name in ('original','selected','excluded')}
        cached={name:cache.get(key) for name,key in keys.items()}
        prepared=await _PREVIEW_EXECUTOR.run('bound-domain:'+source,
            lambda:_restore_mesh_preview(snapshot,cached if all(cached.values()) else None),timeout=PRODUCT_PREVIEW_WAIT_SECONDS)
        views=prepared['views']
        if not _preview_target_current(target):return
        urls={}
        for name,png in views.items():
            cache.put(keys[name],png);urls[name]=await _media_url(keys[name],png)
        await _patch_card(target,{'views':urls,'previewState':'ready',
            'originalNodeCount':prepared['originalNodeCount'],'originalElementCount':prepared['originalElementCount']})
    _start_preview_task(target,prepare)



async def render_product_job(job: dict, *, runtime_active: bool = False) -> None:
    if job.get("status") != "CANDIDATES_READY" or not _confirmed_hash(job):
        await _close_mesh_entry(job)
    projection = (build_readonly_product_job_projection(job) if job.get("status") in {"VIEWER_READY", "FAILED", "HOLD_RUNTIME_INTERRUPTED"}
                  else build_product_job_projection(job, runtime_active=runtime_active))
    view_state = projection["view_state"]
    if view_state not in {"candidate", "result"}:
        await _show_job_summary(job)
    if view_state == "result":
        await _show_readonly_result(job)
        return
    if view_state == "target":
        if job["status"] == "TARGET_PROPOSED":
            await _send_job_stage_card(
                job["job_id"],
                "target",
                _target_card(job["target"], job["job_id"]),
                card_revision=job["revision"],
                evidence=_target_details(job["target"]),
                actions=[cl.Action(
                    name="product_confirm_target",
                    payload={
                        "job_id": job["job_id"],
                        "expected_revision": job["revision"],
                    },
                    label="Confirm target and generate 3 candidates",
                )],
            )
            return
        await _send_job_stage_card(
            job["job_id"],
            "target",
            job_status_text(job.get("status"), failure=job.get("failure")) + ". Use Refresh Job status to read the latest record.",
            card_revision=job["revision"],
            actions=_refresh_actions(job),
        )
        return
    if view_state == "candidate":
        await _show_candidates(job)
        return
    if view_state == "mesh":
        try:
            mesh = normalize_job_mesh_binding(job)
        except (ValueError, OSError):
            notice = _format_product_error("Mesh verification", {"code": "MESH_RECORD_INVALID"},
                write_state="This read could not confirm the saved binding",
                retry_state="Reopen the Job before preview or conformal generation")
            await _send_job_stage_card(job["job_id"], "mesh",
                notice,
                actions=_refresh_actions(job), card_revision=job["revision"],
                evidence=_DisplayDetails(notice),
                model={'kind':'notice','title':'Mesh record unavailable','message':notice,
                       'actions':[action_dict(a) for a in _refresh_actions(job)]})
            return
        actions = _refresh_actions(job)
        if projection["capabilities"]["start_conformal"]:
            actions.insert(0, cl.Action(
                name="product_start_conformal",
                payload={
                    "job_id": job["job_id"],
                    "expected_revision": job["revision"],
                },
                label="Start conformal generation",
            ))
        await _send_job_stage_card(
            job["job_id"],
            "mesh",
            (
                "### INP bound\n\n"
                f"- File: `{_filename(mesh['original_filename'])}`\n"
                f"- {mesh['node_count']} nodes / {mesh['element_count']} elements.\n"
                f"- Recorded mesh quality: {_choice(mesh.get('precheck_status'), ('PASS','HOLD','FAIL','passed','failed'))}.\n"
                "- Coordinates: mm; original coordinate system.\n"
                + (f"- Original upload: `{_filename(mesh['original_upload']['original_filename'])}`; only the {mesh['design_domain']['selected_element_count']} confirmed hexahedra are included.\n"
                   f"- Excluded: {mesh['design_domain']['excluded_element_count']} elements ({mesh['design_domain']['excluded_solid_count']} solids). This does not establish full-bone coverage or shell mechanics validation.\n"
                   if mesh.get("design_domain") else "")
                +
                "The mesh is bound. Select Start conformal generation to confirm the start."
            ),
            actions=actions,
            card_revision=job["revision"],
            evidence=_mesh_details(job, mesh),
            model={**_mesh_model(mesh), 'actions':[action_dict(a) for a in actions]},
        )
        _start_bound_mesh_preview(job)
        return

    failure = job.get("failure") or {}
    content = (
        f"### {job_status_text(job.get('status'), failure=job.get('failure'))}\n\n"
        "Use Refresh Job status to check the record. No automatic retry."
    )
    if failure:
        content += "\n" + error_summary(failure, fallback="The recorded issue needs review. Reopen the Job to check its status.")
    await _send_job_stage_card(
        job["job_id"],
        "conformal",
        content,
        actions=_refresh_actions(job),
        card_revision=job["revision"],
        terminal=job.get("status") in {"FAILED", "HOLD_RUNTIME_INTERRUPTED", "VIEWER_READY"},
        evidence=_DisplayDetails(f"{_product_error_evidence(failure)}\n\n{_target_validation_evidence(job.get('target'))}") if failure else None,
    )


def _refresh_actions(job: dict) -> list:
    return [cl.Action(
        name="product_refresh_job",
        payload={"job_id": job["job_id"]},
        label="Refresh Job status",
    )]


async def resume_product_job(job_id: str) -> bool:
    try:
        job = _job_store().get_consistent(job_id)
    except Exception as exc:
        await cl.Message(content=_format_product_error(
            "Reopen Job",
            exc,
            write_state="The current session binding was not changed",
            retry_state="Check the full Job ID before retrying manually",
        ), elements=_technical_elements(_product_error_evidence(exc))).send()
        return False
    cl.user_session.set("product_mode", True)
    cl.user_session.set("product_chat_intent", None)
    await invalidate_product_waits()
    cl.user_session.set("product_job_id", job["job_id"])
    cl.user_session.set("product_job_revision", job["revision"])
    await render_product_job(
        job,
        runtime_active=_RUNTIME_MANAGER.is_active(job["job_id"]),
    )
    return True


async def _bind_uploaded_mesh(
    *,
    job_id: str,
    name: str,
    content: bytes,
    expected_revision: int,
    lease: dict,
) -> None:
    if not await _consume_mesh_lease(lease):
        await cl.Message(content=_workflow_status(_UPLOAD_STALE)).send()
        return
    try:
        job = await asyncio.to_thread(
            _workflow().bind_mesh,
            job_id,
            original_filename=name,
            content=content,
            expected_revision=expected_revision,
            confirmed_contract={**PRODUCT_MESH_CONTRACT, "design_domain": "selected_hex_domain"},
        )
    except Exception as exc:
        await cl.Message(content=_format_product_error(
            "INP binding and static checks",
            exc,
            write_state="Check the latest Job state",
            retry_state="No automatic retry",
        ), elements=_technical_elements(_product_error_evidence(exc))).send()
        return
    if (job.get("mesh_uploads") or [{}])[-1].get("status") == "DOMAIN_PENDING":
        try:
            job = await _confirm_uploaded_domain(job, content=content, lease=lease)
        except Exception as exc:
            await cl.Message(content=_format_product_error("Domain preview or confirmation", exc,
                write_state="The original upload was retained; check the latest Job binding state", retry_state="No automatic retry. Reopen the Job to check its state"), elements=_technical_elements(_product_error_evidence(exc))).send()
            return
        if job is None:
            return
    if job["status"] == "CANDIDATES_READY":
        uploads = job.get("mesh_uploads") or []
        rejected = uploads[-1] if uploads else {}
        precheck = rejected.get("domain_precheck") or rejected.get("precheck") or {}
        await cl.Message(
            content=_workflow_status(
                "INP static checks did not pass. "
                "The original upload and check evidence were retained. No conformal computation ran. Correct the mesh quality or remesh, then upload manually. Quality thresholds were not relaxed."
            ),
            elements=_technical_elements(_quality_details(precheck)),
        ).send()
        await _refresh_mesh_entry(job["job_id"])
        return
    if job["status"] != "AWAITING_CONFORMAL_AUTHORIZATION":
        await cl.Message(content=_format_product_error(
            "INP static checks did not pass",
            job.get("failure"),
            write_state="Failure evidence was recorded",
            retry_state="No automatic retry",
        ), elements=_technical_elements(_product_error_evidence(job.get("failure")))).send()
        return
    await render_product_job(job)
    await _continue_product_chat("mesh_confirmed", job["job_id"], generation=lease.get("session_generation"))


def _domain_lease_current(lease: dict, stage="AWAIT_DOMAIN", *, check_expiry=True) -> bool:
    state = _job_ui_state(lease["job_id"])
    return (state["busy"] is lease and lease["stage"] == stage
            and lease["session_generation"] == _session_generation()
            and cl.user_session.get("product_job_id") == lease["job_id"]
            and (not check_expiry or time.monotonic() < lease["expires_at"]))


async def _confirm_uploaded_domain(job: dict, *, content: bytes, lease: dict) -> dict | None:
    from .mesh_domain import render_mesh_views, digest
    upload = job["mesh_uploads"][-1]
    proposal = upload["domain_proposal"]
    domain = proposal["domain"]
    if digest(content) != domain["original_sha256"]:
        raise ValueError("preview differs from original upload")
    state = _job_ui_state(job["job_id"])
    async with state["lock"]:
        if (state["busy"] is not lease or lease["stage"] != "SUBMITTING"
            or lease["session_generation"] != _session_generation()
            or cl.user_session.get("product_job_id") != job["job_id"]):
            return None
        lease.update(stage="PREPARING_PREVIEW", expected_revision=job["revision"],
                     expires_at=time.monotonic()+PRODUCT_PREVIEW_WAIT_SECONDS)
    await _show_job_summary(job, waiting='Mesh uploaded · Preparing the computational domain preview')
    await _send_job_stage_card(job["job_id"], "domain",
        "### Uploaded mesh · Preparing preview\n\nThe original upload is retained. Domain confirmation will open when the preview is ready.",
        card_revision=job["revision"], model={"kind":"mesh","mode":"preparing", "quality":"Not checked",
            "fileName":_filename(upload.get("original_filename","INP mesh")),"previewState":"preparing"})
    source_key = "domain:" + domain["original_sha256"]
    target = _capture_preview_target(job["job_id"], "domain", proposal["proposal_sha256"])
    cache = _preview_cache(job["job_id"])
    async def preview_failed(exc):
        logger.warning("product_domain_preview_failed job_id=%s", job["job_id"])
        if _preview_target_current(target):
            await _update_stage_preview(target, [], content="### Mesh preview unavailable\n\nThe original upload was retained. No domain was confirmed and no computation started.")
            await _show_upload_recovery(lease, "Mesh preview could not be prepared. Reopen the current Job to recover.", submitted=True)
        async with state["lock"]:
            if state["busy"] is lease:
                state["busy"] = None
    try:
        keys={name:source_key+':'+name for name in ('original','selected','excluded')}
        preview={name:cache.get(key) for name,key in keys.items()}
        counts = state.get('original_mesh_counts')
        if not all(preview.values()) or not counts or counts['source'] != domain['original_sha256']:
            prepared = await _PREVIEW_EXECUTOR.run(source_key, lambda: _mesh_preview_data(content),
                timeout=max(0.01, lease["expires_at"] - time.monotonic()))
            preview = prepared['views']
            counts = {'source':domain['original_sha256'], 'originalNodeCount':prepared['originalNodeCount'],
                      'originalElementCount':prepared['originalElementCount']}
            state['original_mesh_counts'] = counts
        if not _domain_lease_current(lease, "PREPARING_PREVIEW", check_expiry=False):
            return None
        if not _domain_lease_current(lease, "PREPARING_PREVIEW"):
            raise PreviewTimeout("Mesh preview completed after its deadline")
        current = await asyncio.to_thread(_upload_job_matches, job["job_id"], job["revision"], lease["confirmation_sha256"])
        if not current or not _domain_lease_current(lease, "PREPARING_PREVIEW", check_expiry=False):
            return None
        if not _domain_lease_current(lease, "PREPARING_PREVIEW"):
            raise PreviewTimeout("Mesh preview identity read exceeded its deadline")
        urls={}
        for name,png in preview.items():
            cache.put(keys[name],png)
            urls[name]=await _media_url(keys[name],png)
    except Exception as exc:
        await preview_failed(exc)
        return None
    base = {"entry_token":lease["entry_token"],"upload_id":upload["upload_id"],"proposal_sha256":proposal["proposal_sha256"]}
    await _show_job_summary(job, waiting='Mesh uploaded · Waiting for domain confirmation')
    model={'kind':'mesh','mode':'await_domain','fileName':_filename(upload.get('original_filename','INP mesh')),
        'selectedCount':domain['selected_element_count'],'nodeCount':counts['originalNodeCount'],
        'selectedNodeCount':domain.get('selected_node_count'),
        'originalNodeCount':counts['originalNodeCount'],'originalElementCount':counts['originalElementCount'],
        'elementCount':domain['selected_element_count']+domain['excluded_element_count'],
        'excludedCount':domain['excluded_element_count'],'excludedSolids':domain['excluded_solid_count'],
        'excludedTypes':_excluded_counts(domain),'quality':'Not checked yet','views':urls,
        'previewState':'ready','askActive':False,'askPayload':base,'jobId':job['job_id'],
        'replayLabel':cl.user_session.get('product_replay_label')}
    element=cl.CustomElement(name='BoneTaskCard',props=model,display='inline')
    model['componentId']=element.id
    element.props=model
    card=target['card']
    async def ready(sent_element):
        current=await asyncio.to_thread(_upload_job_matches,job['job_id'],job['revision'],lease['confirmation_sha256'])
        async with state['lock']:
            if not current or not _domain_lease_current(lease,'PREPARING_PREVIEW'):
                return False
            deadline=time.monotonic()+120
            lease.update(stage='AWAIT_DOMAIN',expires_at=deadline)
            sent_element.props['_deadline']=deadline
        return True
    async def finished(sent_element):
        await sent_element.remove()
        if (cl.user_session.get('product_job_id')==job['job_id']
                and lease['session_generation']==_session_generation()
                and card.get('component') is sent_element):
            card['component']=None
            await _send_job_stage_card(job['job_id'],'domain','',card_revision=job['revision'],
                model={**model,'askActive':False,'historical':True,'mode':'closed',
                       'message':'Domain question ended. Follow the current task state.'})
    ask=DomainAsk(element=element,ready=ready,finished=finished)
    # Replace the provisional preparing message with this actual native Ask.
    if not _preview_target_current(target):return None
    await target['message'].remove()
    card.update(component=element,model=model)
    cl.user_session.set(f"product_stage_card:{job['job_id']}:domain",ask)
    try:
        answer=await _send_upload_ask(ask)
        if isinstance(answer,dict) and answer.get('submitted') is False:
            answer={'payload':{**base,'confirm':False}}
    except (asyncio.TimeoutError, AskTimeoutError):
        answer=None
    current = await asyncio.to_thread(_upload_job_matches, job["job_id"], job["revision"], lease["confirmation_sha256"])
    async with state["lock"]:
        checked_at = ask.received_at if answer is not None else time.monotonic()
        in_time = checked_at is not None and checked_at < lease["expires_at"]
        if not current or not in_time or not _domain_lease_current(lease, check_expiry=False):
            if state['busy'] is lease:state['busy']=None
            await cl.Message(content=_workflow_status("Domain confirmation expired. No computational domain was bound and no computation started. Reopen the Job.")).send()
            return None
        payload = answer.get("payload") if isinstance(answer, dict) else None
        confirmed = payload == {**base, "confirm": True}
        if (payload not in (None, {**base, "confirm": True}, {**base, "confirm": False})
            or (payload is not None and type(payload.get("confirm")) is not bool)):
            await cl.Message(content=_workflow_status(_UPLOAD_STALE)).send()
            return None
        lease["stage"] = "DOMAIN_SUBMITTING"
    _chat_trace("mesh_domain_confirmation", job_id=job["job_id"], revision=job["revision"],
                upload_id=upload["upload_id"], proposal_sha256=proposal["proposal_sha256"],
                confirmed=confirmed, authorization_source="trusted_ui")
    updated = await asyncio.to_thread(_workflow().confirm_mesh_domain, job["job_id"], expected_revision=job["revision"],
        upload_id=upload["upload_id"], proposal_sha256=proposal["proposal_sha256"], session_generation=lease["session_generation"], confirmed=confirmed)
    _chat_trace("mesh_domain_returned", job_id=updated["job_id"], revision=updated["revision"], status=updated["status"])
    if not confirmed:
        await cl.Message(content=_workflow_status("Domain selection was cancelled. The original upload was retained; no derived input or computation was produced.")).send()
        await _refresh_mesh_entry(job["job_id"])
        return None
    return updated


async def _handle_attachment(element) -> bool:
    name = str(getattr(element, "name", "") or Path(element.path).name)
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        state = await _begin_csv_import()
        if state is None:
            return True
        content = await asyncio.to_thread(Path(element.path).read_bytes)
        await _handle_csv_upload(content, filename=name, state=state)
        return True
    if suffix != ".inp":
        await cl.Message(content=_workflow_status("Upload a 9D stiffness CSV and choose one target row, or use the INP mesh upload entry.")).send()
        return True
    job_id = cl.user_session.get("product_job_id")
    if not job_id:
        await cl.Message(content=_workflow_status("Confirm the target and generate candidates before uploading an INP file.")).send()
        return True
    state = _job_ui_state(job_id)
    async with state["lock"]:
        lease = state["busy"]
        allowed = lease is not None and _lease_current(lease, "AWAIT_FILE")
        if allowed:
            # An attachment arrives in a different callback/context. Transfer a
            # new lease object before draining AskFile so its old finally cannot
            # clear this callback's busy claim or entry.
            lease = dict(lease)
            state["busy"] = lease
    if not allowed:
        await cl.Message(content=_workflow_status("Confirm a candidate and use the controlled upload entry to review the mesh requirements. This attachment was not saved to the Job.")).send()
        return True
    owner = None
    owner_token = None
    try:
        owner = await _begin_upload_loading(lease)
        owner_token = _UPLOAD_LOADING_OWNER.set(owner)
        if not await asyncio.to_thread(_upload_job_matches, job_id, lease["expected_revision"], lease["confirmation_sha256"]):
            raise ValueError(_UPLOAD_STALE)
        content = await asyncio.to_thread(Path(element.path).read_bytes)
        await _bind_uploaded_mesh(job_id=job_id, name=name, content=content, expected_revision=lease["expected_revision"], lease=lease)
    finally:
        async with state["lock"]:
            if state["busy"] is lease:
                state["busy"] = None
        try:
            await _finish_upload_loading(owner)
        finally:
            if owner_token is not None:
                _UPLOAD_LOADING_OWNER.reset(owner_token)
    return True


def classify_product_message(message, session_state) -> str:
    text = (getattr(message, "content", "") or "").strip()
    if _extract_resume_job_id(text) is not None:
        return "resume"
    if text.lower() in {"/product", "product"} or (re.search(r"(?:开始|启动).*(?:产品主链|产品设计|主链流程)", text) and not re.search(r"不要|别|不想|无需", text)):
        return "start"
    elements = [e for e in getattr(message, "elements", None) or [] if getattr(e, "path", None)]
    if elements:
        return "attachment"
    return classify_readonly_intent(text)

def _extract_resume_job_id(text: str) -> str | None:
    match = re.fullmatch(r"/resume\s+(job-[0-9a-fA-F]{24})", str(text or "").strip())
    if not match:
        return None
    prefix, suffix = match.group(1).split("-", 1)
    return f"{prefix}-{suffix.upper()}"


async def _answer_help(text):
    await cl.Message(content=("Ask about the workflow, query the reference database, or inspect a specified Job. Creating a design target requires confirmation. "
        "Workflow: confirm target → generate candidates → confirm candidate → review mesh requirements and upload → confirm conformal generation. "
        "B31 is an engineering beam mesh; STL/PNG provide geometry previews. A unit cell is the basic lattice structure. "
        "Database C/Eb, relative density, and normalized strut diameter are dimensionless. Target stiffness uses GPa. "
        "Candidate ranking and successful geometry generation do not establish mechanical, FEA, manufacturing, or clinical validation. NOT_FULL_DUAL_SPACE_FUSION.")).send()


async def _answer_current_job():
    job_id = cl.user_session.get("product_job_id")
    if not job_id:
        await cl.Message(content="No result is selected. Choose a Job or provide its full ID. The latest Job will not be guessed.").send()
        return
    try:
        job = await asyncio.to_thread(_job_store().get_consistent, job_id)
        build_readonly_product_job_projection(job)  # Preserve the read-only contract check.
        await cl.Message(content=(job_status_text(job.get("status"), failure=job.get("failure")) + ". "
            f"Candidate confirmation: {candidate_confirmation_status(job)}. "
            "This view explains recorded state only. Geometry does not establish mechanical, FEA, manufacturing, or clinical validation.\n"
            + stiffness_table((job.get("target") or {}).get("physical_9d_gpa"))),
            elements=_technical_elements(_job_details(job))).send()
    except Exception:
        logger.error("product_current_job_read_failed job_id=%s", job_id)
        await cl.Message(content="The Job could not be read consistently. Its results cannot be interpreted. Preserve the diagnostic record.").send()


def _database_details(result):
    data = result["data"]
    if result["kind"] == "overview":
        policy = data.get("eligibility_policy") or {}
        lines = ["Query fields: sample_id, relative_density, diameter, " + ", ".join(STIFFNESS_NAMES)]
        for key in ("is_singular", "is_pd_full", "has_periodic_mismatch", "is_degenerate"):
            if type(policy.get(key)) is int:
                lines.append(f"{key}: {policy[key]}")
        if policy.get("selected_9d") == "finite_positive":
            lines.append("All nine stiffness values must be finite and positive.")
        return _DisplayDetails("\n".join(lines))
    lines = ["Reference database · stiffness C/Eb, relative density and normalized diameter are dimensionless."]
    digest = (result.get("provenance") or {}).get("source_sha256")
    if isinstance(digest, str) and re.fullmatch("[a-fA-F0-9]{64}", digest):
        lines.append("source_sha256: " + digest)
    for row in data.get("rows", []):
        fields = ("sample_id", "relative_density", "diameter", *STIFFNESS_NAMES)
        lines.append(" · ".join(f"{key}={row[key]}" for key in fields if type(row.get(key)) in (int, float)))
    return _DisplayDetails("\n".join(lines))


def _database_conditions(request, *, label="Conditions used"):
    if request["kind"] == "overview":
        return ["Request: release-manifest overview."]
    operators = {"eq": "=", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "between": "between"}
    conditions = []
    for item in request["filters"]:
        value = item["value"]
        # A rounded cutoff could describe a different set of matching records.
        shown = " – ".join(map(str, value)) if isinstance(value, list) else str(value)
        unit = " C/Eb" if item["field"] in STIFFNESS_NAMES else ""
        conditions.append(f"{item['field']} {operators[item['op']]} {shown}{unit}")
    lines = [label + ": " + " AND ".join(conditions)] if conditions else []
    if request["kind"] == "records":
        lines.append("Order: " + ", ".join(
            f"{item['field']} {'descending' if item['direction'] == 'desc' else 'ascending'}"
            for item in request["order_by"]))
        lines.append(f"Limit: {request['limit']} records")
    return lines


def _database_turn_outcome(read, status, clarification=None):
    outcome = read.get("outcome")
    presented = read.get("presented")
    if outcome in {"ok", "empty"}:
        if not presented:
            summary = "The database returned a result, but its result card could not be confirmed on this page."
        elif status not in {"answered", "empty"}:
            explanation = {"timeout": "timed out", "error": "failed",
                           "budget_exhausted": "reached the session budget", "limit_reached": "reached its tool limit",
                           "needs_clarification": "requires clarification"}.get(status, "did not complete")
            summary = f"The database read succeeded. The follow-up explanation {explanation}. No automatic retry."
        elif outcome == "empty":
            summary = "The recorded conditions were kept. No automatic retry."
        else:
            summary = None
    elif not read.get("submitted"):
        summary = "The prepared request was not submitted to the database. This turn has no verified result."
    elif status == "timeout":
        summary = "The read timed out after submission. This turn has no verified result. No automatic retry."
    elif outcome == "read_failed":
        summary = "The database read failed after submission. This turn has no verified result. No automatic retry."
    elif outcome == "unverified_return":
        summary = "The database response failed verification. This turn has no verified result. No automatic retry."
    else:
        summary = "This turn has no verified database result. No automatic retry."
    lines = [summary] if summary else []
    if clarification:
        lines.append(clarification)
    if not presented or outcome == "error":
        label = "Submitted conditions" if read.get("submitted") else "Prepared conditions"
        lines.extend(_database_conditions(read["request"], label=label))
    if read["inherited"]:
        lines.append("The request inherited the previous query settings and applied this turn's changes.")
    if read["from_gpa"]:
        lines.append("Stiffness thresholds were converted from GPa to C/Eb using Eb = 110 GPa.")
    if read["from_percent"]:
        lines.append("Percentage thresholds were divided by 100 to obtain dimensionless ratios.")
    if read.get("references"):
        lines.append("Same-value references in this turn's planning context: " + "; ".join(read["references"]) + ".")
    return "\n\n".join(lines)


async def _show_database_result(result: dict, *, question: str, normalization=None):
    if result["status"] == "error":
        error = result["error"]
        await cl.Message(content=error_summary(error) + " This query returned no result. Submit a corrected query manually.",
                         elements=_technical_elements(_product_error_evidence(error))).send()
        return
    data, provenance = result["data"], result["provenance"]
    lines = []
    actions = []
    if result["kind"] == "overview":
        lines.append(f"Release-manifest statistics at {data['statistics_time']}: {data['scanned_rows']} source records; {data['eligible_rows']} eligible records.")
        lines.append("Stiffness C/Eb, relative density, and normalized strut diameter are dimensionless. See Technical details for fields, eligibility policy.")
    else:
        request = result["normalized_request"]
        lines.extend(_database_conditions(request))
        lines.append("C/Eb, relative density, and normalized strut diameter are dimensionless.")
        if normalization and normalization.get("unit_conversion"):
            lines.append("The query converted stiffness thresholds from GPa to dimensionless C/Eb using Eb = 110 GPa.")
        if result["kind"] == "count":
            lines.append(f"{data['matched_count']} records match.")
        else:
            result_ref = result.get("result_ref") or _product_chat_tools().register_records(result)
            columns = ["sample_id", "relative_density", "diameter"]
            table = ["| Record | Sample ID | Relative density | Normalized strut diameter |",
                     "| " + " | ".join(["---"] * (len(columns) + 1)) + " |"]
            query_id = secrets.token_hex(16)
            result_sha = canonical_sha256(result)
            cache = {"query_id": query_id, "result_sha256": result_sha, "issued_at": time.monotonic(), "chat_result_ref": result_ref,
                     "result": result, "actions": {}, "session_generation": _session_generation()}
            cl.user_session.set("product_database_result", cache)
            for position, row in enumerate(data["rows"], 1):
                token = secrets.token_hex(24)
                payload = {"query_id": query_id, "sample_id": row["sample_id"], "result_sha256": result_sha, "token": token}
                cache["actions"][token] = {"payload": payload, "used": False}
                table.append("| " + " | ".join([str(position), *[str(row[name]) if name == "sample_id" else display_number(row.get(name)) for name in columns]]) + " |")
                actions.append(cl.Action(name="product_choose_reference", payload=payload, label=f"Use record {position} as target (sample_id={row['sample_id']})"))
            if not data["rows"]:
                lines.append("No matching records. No Job was created.")
            else:
                lines.append(f"Showing {len(data['rows'])} records. Ask in natural language for the total matching count.")
                lines.append("\n".join(table))
                for position, row in enumerate(data["rows"], 1):
                    lines.append(f"Record {position} stiffness C/Eb: " + " · ".join(f"{name}={display_number(row.get(name))}" for name in STIFFNESS_NAMES))
                lines.append("The query is complete. To use a record as a design target, select its button and confirm the proposed target.")
    lines.append(f"Read at {provenance['read_at']}. Technical details")
    await cl.Message(content="\n\n".join(lines), actions=actions,
                     elements=_technical_elements(_database_details(result))).send()


async def _answer_database(text):
    try:
        query = _query_service()
        if re.search(r"解释.*(?:B31|STL|流程|单胞)", text, re.I):
            await _answer_help(text)
        if re.search(r"(?:共有|总共|总量).*(?:其中|满足)|其中|某条件", text):
            await _show_database_result(await asyncio.to_thread(query.execute_request, {"schema_version": DATABASE_REQUEST_SCHEMA, "kind": "overview"}), question=text)
        planned = await asyncio.wait_for(asyncio.to_thread(plan_database_read, text, planner=_planner), timeout=30)
        cl.user_session.set("product_query_planner_evidence", planned)
        if planned["status"] == "needs_clarification":
            await cl.Message(content=_query_clarification_text(planned["question"]), elements=[]).send()
            return
        result = await asyncio.to_thread(query.execute_request, planned["request"])
        await _show_database_result(result, question=text, normalization=planned)
    except ReadonlyQueryError as exc:
        await cl.Message(content=error_summary(exc.error) + " No automatic retry.", elements=_technical_elements(_product_error_evidence(exc.error))).send()
    except Exception:
        logger.error("product_readonly_query_unrecognized")
        await cl.Message(content="The request could not be interpreted. Specify the query fields or full Job ID. No Job was created; no automatic retry.").send()


async def handle_product_message(message: cl.Message) -> bool:
    original_text = message.content or ""
    text = original_text.strip()
    attached = list(getattr(message, "elements", None) or [])
    if len(attached) > 1 or any(not getattr(el, "path", None) for el in attached):
        await cl.Message(content="Attach one readable CSV or INP file at a time. No target was processed. Do not submit repeated uploads.").send()
        return True
    if text.lower().startswith("/resume") and _extract_resume_job_id(text) is None:
        await cl.Message(content="Use the full `/resume job-...` command. Invalid resume commands are not sent to the model.").send()
        return True
    if not attached and not text.startswith("/") and text.lower() != "product":
        await _run_product_chat(original_text)
        return True
    route = classify_product_message(message, cl.user_session)
    if route == "resume":
        await resume_product_job(_extract_resume_job_id(text))
    elif route in {"start", "design"}:
        cl.user_session.set("product_mode", True)
        await cl.Message(content="Query reference data and select a record, or upload a 9D stiffness CSV in GPa and choose one row. Review the target proposal; a Job is created only after confirmation.").send()
    elif route == "attachment":
        try:
            await _handle_attachment(attached[0])
        except Exception as exc:
            logger.error("product_attachment_processing_failed")
            await cl.Message(content=_format_product_error("Attachment reading or target proposal", exc, write_state="Needs verification", retry_state="Check the current Job before uploading again"), elements=_technical_elements(_product_error_evidence(exc))).send()
    elif route == "help":
        await _answer_help(text)
    elif route == "current_job":
        await _answer_current_job()
    elif route == "database":
        await _answer_database(text)
    else:
        await cl.Message(content="Ask about the workflow, query reference data, or provide a full Job ID.").send()
    return True

@cl.action_callback("product_start_entry")
async def product_start_entry(_action: cl.Action):
    cl.user_session.set("product_mode", True)
    await cl.Message(content=_workflow_status(
        "Set a design target. Describe your reference-data query in natural language, "
        "or upload a 9D stiffness CSV in GPa and choose one row."
    )).send()


@cl.action_callback("product_resume_entry")
async def product_resume_entry(_action: cl.Action):
    await cl.Message(content=_workflow_status(
        "Send `/resume job-XXXXXXXXXXXXXXXXXXXXXXXX` with the exact full Job ID. "
        "The latest Job will not be selected automatically."
    )).send()


@cl.action_callback("product_refresh_job")
async def product_refresh_job(action: cl.Action):
    payload = action.payload or {}
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        await cl.Message(content=_workflow_status("Refresh failed: Job ID is missing.")).send()
        return
    if "expected_revision" in payload or "session_generation" in payload:
        valid = (JOB_ID_PATTERN.fullmatch(job_id) is not None
                 and _valid_revision(payload.get("expected_revision"))
                 and payload.get("session_generation") == _session_generation()
                 and cl.user_session.get("product_job_id") == job_id)
        if valid:
            try:
                job = await asyncio.to_thread(_workflow().get_consistent_job, job_id)
                valid = job.get("job_id") == job_id and job.get("revision") == payload["expected_revision"]
            except Exception:
                valid = False
        if not valid:
            await cl.Message(content=_workflow_status(
                "This entry expired because the Job revision or session changed. Use the current Job entry or the full /resume command."
            )).send()
            return
    await resume_product_job(job_id)


@cl.action_callback("product_choose_reference")
async def choose_reference(action: cl.Action):
    payload = action.payload or {}
    cache = cl.user_session.get("product_database_result")
    row = None
    async with _creation_state()["lock"]:
        record = cache["actions"].get(payload.get("token")) if cache else None
        if (record and not record["used"] and record["payload"] == payload
                and cache["session_generation"] == _session_generation()
                and time.monotonic() - cache["issued_at"] < 900):
            record["used"] = True
            row = next((r for r in cache["result"]["data"]["rows"] if r["sample_id"] == payload["sample_id"]), None)
    if row is None:
        await cl.Message(content=_workflow_status("This reference-record button expired. Query again to obtain a current record.")).send()
        return
    try:
        result = await _product_chat_tools().execute("prepare_design_target", {
            "result_ref": cache["chat_result_ref"], "sample_id": row["sample_id"]})
        await _present_chat_tool("prepare_design_target", result)
    except Exception:
        logger.error("product_reference_action_rejected")
        await cl.Message(content="The reference-record identity could not be verified. No Job was created. Query again to obtain a current record.").send()


@cl.action_callback("product_replay_frozen_query")
async def replay_frozen_query(action: cl.Action):
    """Replay a frozen query plan without invoking an LLM or starting runtime."""

    payload = action.payload or {}
    replay = replay_frozen_query_plan(
        payload.get("query_plan"),
        planner=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("LLM must not be called for R3 replay")
        ),
    )
    proposals = await asyncio.to_thread(
        proposals_from_formal_query,
        replay["query_plan"],
        _query_service(),
        query_text=str(payload.get("query_text") or "R3_FROZEN_QUERY_REPLAY"),
        planner_evidence={"kind": "R3_FROZEN_QUERY_REPLAY", "llm_called": False},
    )
    cl.user_session.set("product_reference_proposals", proposals)
    await cl.Message(
        content=_workflow_status(
            f"The frozen query plan was read in replay mode: {len(proposals)} reference records returned. No LLM or computation was started."
        )
    ).send()


async def _inverse_with_feedback(payload, generation):
    started = time.monotonic()
    def current():
        return generation == _session_generation() and cl.user_session.get('product_job_id') == payload['job_id']
    message = None
    try:
        message = await cl.Message(content='Processing target confirmation; elapsed 0 s. Computation starts only if validation passes.').send()
    except Exception:
        logging.getLogger(__name__).warning('Inverse wait feedback could not be sent')
    if not current():
        return None
    task = asyncio.create_task(asyncio.to_thread(
        _workflow().confirm_target_and_run_inverse, payload['job_id'],
        expected_revision=int(payload['expected_revision']),
        authorization={'authorized': True, 'authorized_by': 'chainlit_user_action'}))
    async def feedback(content):
        if message is not None and current():
            try:
                message.content = content
                await message.update()
            except Exception:
                logging.getLogger(__name__).warning('Inverse wait feedback could not be updated')
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=5.)
            if not task.done() and current():
                await feedback(f'Processing target confirmation / inverse computation; elapsed {int(time.monotonic()-started)} s. No automatic retry.')
        return await task
    finally:
        if current():
            await feedback(f'Target processing returned; elapsed {int(time.monotonic()-started)} s. See the recorded Job for the outcome.'
                           if task.done() else 'Response wait ended. Computation status must be checked in the recorded Job; no automatic retry.')


@cl.action_callback("product_confirm_target")
async def confirm_target(action: cl.Action):
    generation = _session_generation()
    if not await _consume_workflow_confirmation(action, "product_confirm_target"):
        await cl.Message(content="Target confirmation expired. Reopen the current Job and confirm again.").send()
        return
    payload = action.payload or {}
    try:
        job = await _inverse_with_feedback(payload, generation)
    except Exception as exc:
        if generation != _session_generation() or cl.user_session.get('product_job_id') != payload.get('job_id'):
            return
        await cl.Message(content=_format_product_error(
            "Target confirmation and inverse design",
            exc,
            write_state="Check the recorded Job events",
            retry_state="No automatic retry",
        ), elements=_technical_elements(_product_error_evidence(exc))).send()
        await _continue_product_chat("confirm_target_and_generate", payload.get("job_id"), generation=generation, failed=True)
        return
    if generation != _session_generation() or cl.user_session.get('product_job_id') != payload.get('job_id'):
        return
    if job["status"] != "CANDIDATES_READY":
        failure = job.get("failure") or {}
        await cl.Message(content=_format_product_error(
            "Target validation" if failure.get("stage") == "TARGET_CONFIRMED" else "Inverse design",
            failure,
            write_state="Failure evidence was recorded",
            retry_state="No automatic retry",
        ), elements=_technical_elements(_DisplayDetails(f"{_product_error_evidence(failure)}\n\n{_target_validation_evidence(job.get('target'))}"))).send()
        await _continue_product_chat("confirm_target_and_generate", job["job_id"], generation=generation, failed=True)
        return
    await _show_candidates(job)
    await _continue_product_chat("confirm_target_and_generate", job["job_id"], generation=generation)


@cl.action_callback("product_select_candidate")
async def select_candidate(action: cl.Action):
    generation = _session_generation()
    if not await _consume_workflow_confirmation(action, "product_select_candidate"):
        await cl.Message(content=_workflow_status("Candidate selection expired. Reopen the current Job.")).send()
        return
    payload = action.payload or {}
    state = _job_ui_state(str(payload.get("job_id") or ""))
    token = getattr(action, "id", None)
    async with state["lock"]:
        retired = state.setdefault("retired_selection_actions", set())
        if (token is None or token in retired or state.get('selection_pending') is not None
                or cl.user_session.get("product_job_id") != payload.get("job_id")):
            rejected = True
        else:
            retired.add(token)
            state['selection_pending'] = token
            state['selection_running'] = True
            state['selection_revision'] = payload.get('expected_revision')
            state["entry"] = None
            state["busy"] = None
            rejected = False
    if rejected:
        await cl.Message(content=_workflow_status("Candidate selection expired. Reopen the Job.")).send()
        return
    await _safe_candidate_feedback(payload['job_id'],'pending','Confirming your selection…',payload['expected_revision'])
    try:
        job = await asyncio.to_thread(_workflow().select_candidate,
            payload["job_id"], payload["candidate_id"],
            expected_revision=payload["expected_revision"],
            expected_candidate_set_sha256=payload["expected_candidate_set_sha256"],
            reason="Chainlit user selection",
        )
    except CandidateConfirmationCommitError as exc:
        if generation != _session_generation() or cl.user_session.get('product_job_id') != payload.get('job_id'):
            return
        outcome = exc.outcome
        cl.user_session.set(f"product_confirmation_outcome:{payload['job_id']}", outcome)
        status={'NOT_COMMITTED':'not_committed','COMMITTED':'committed_ui_pending'}.get(outcome['status'],'uncertain')
        await _safe_candidate_feedback(payload['job_id'],status,
            'Confirmation was recorded; reopen this task to restore its page.' if outcome['status']=='COMMITTED'
            else 'Selection needs review. Reopen the task to check its recorded state.',payload['expected_revision'])
        messages = {"NOT_COMMITTED": "Confirmation did not complete.", "COMMITTED": "The candidate was confirmed, but its page update did not complete."}
        await cl.Message(content=_workflow_status(messages.get(outcome["status"], "The confirmation record could not be verified consistently. Preserve the session.") + " Upload is unavailable. Reopen the Job using its full ID. No automatic retry.")).send()
        return
    except Exception as exc:
        if generation != _session_generation() or cl.user_session.get('product_job_id') != payload.get('job_id'):
            return
        await _safe_candidate_feedback(payload['job_id'],'uncertain','Selection needs review. Reopen the task to check its recorded state.',payload['expected_revision'])
        await cl.Message(content=_format_product_error(
            "Candidate selection",
            exc,
            write_state="The candidate confirmation write needs verification; upload is unavailable",
            retry_state="Reopen the Job to check its state. No automatic retry",
        ), elements=_technical_elements(_product_error_evidence(exc))).send()
        return
    if generation != _session_generation() or cl.user_session.get('product_job_id') != payload.get('job_id'):
        return
    try:
        await _show_candidates(job)
        if generation != _session_generation() or cl.user_session.get("product_job_id") != job["job_id"]:
            return
        selection = job["selection"]
        content = f"Selected Rank {selection['selected_rank']} · `{selection['selected_candidate_id']}`."
        if build_product_job_projection(job)["capabilities"]["upload_mesh"]:
            content += ' Next, use "Upload hexahedral mesh (.inp)".'
        await cl.Message(content=content).send()
        await _continue_product_chat("select_candidate", job["job_id"], generation=generation)
    except Exception:
        state["entry"] = None
        logger.error("candidate_confirmation_ui_pending job_id=%s revision=%s", job["job_id"], job["revision"])
        await cl.Message(content=_workflow_status(f"The candidate was confirmed, but its page update did not complete. Reopen Job {job['job_id']}. The confirmation will not be written again.")).send()


async def _end_ask_file_loading_state() -> None:
    await cl.context.emitter.task_end()


async def _ask_for_product_mesh() -> list | None:
    try:
        return await _send_upload_ask(cl.AskFileMessage(
            content=_mesh_contract_copy() + "\n\nYou have 10 minutes to select a file. If you cancel or time out, reopen the current Job.",
            accept={
                "application/octet-stream": [".inp"],
                "text/plain": [".inp"],
            },
            max_size_mb=PRODUCT_INP_MAX_SIZE_MB,
            max_files=1,
            timeout=PRODUCT_UPLOAD_WAIT_SECONDS,
            raise_on_timeout=True,
        ))
    finally:
        if _UPLOAD_LOADING_OWNER.get() is None:
            await _end_ask_file_loading_state()


@cl.action_callback("product_upload_mesh")
async def upload_mesh(action: cl.Action):
    await _run_mesh_upload(action)


@cl.action_callback("product_mesh_upload_entry")
async def product_mesh_upload_entry(action: cl.Action):
    await _run_mesh_upload(action)


def _upload_job_matches(job_id: str, revision: int, confirmation_sha256: str) -> bool:
    job = _workflow().get_consistent_job(job_id)
    return (job.get("job_id") == job_id and job.get("status") == "CANDIDATES_READY"
            and _valid_revision(job.get("revision")) and job["revision"] == revision
            and _confirmed_hash(job) == confirmation_sha256)


def _lease_context_current(lease: dict, stage: str) -> bool:
    state = _job_ui_state(lease["job_id"])
    return (state["busy"] is lease and lease["stage"] == stage
            and state["entry"] is not None
            and state["entry"]["payload"]["entry_token"] == lease["entry_token"]
            and lease["session_generation"] == _session_generation()
            and cl.user_session.get("product_job_id") == lease["job_id"])


def _lease_current(lease: dict, stage: str) -> bool:
    return _lease_context_current(lease, stage) and time.monotonic() < lease["expires_at"]


async def _consume_mesh_lease(lease: dict) -> bool:
    state = _job_ui_state(lease["job_id"])
    async with state["lock"]:
        valid = _lease_current(lease, "AWAIT_FILE")
    if not valid:
        return False
    try:
        consistent = await asyncio.to_thread(_upload_job_matches, lease["job_id"], lease["expected_revision"], lease["confirmation_sha256"])
    except Exception:
        return False
    async with state["lock"]:
        if not consistent or not _lease_current(lease, "AWAIT_FILE"):
            return False
        lease["stage"] = "SUBMITTING"
        state["entry"] = None
        return True


async def _ask_for_mesh_contract(lease: dict):
    return await _send_upload_ask(cl.AskActionMessage(
        content=_mesh_contract_copy() + "\n\nYou have 10 minutes to confirm the mesh requirements. File selection opens after confirmation.",
        actions=[
            cl.Action(name="product_mesh_contract_confirm", label="Confirm requirements and choose file",
                      payload={"entry_token": lease["entry_token"], "confirm": True}),
            cl.Action(name="product_mesh_contract_cancel", label="Cancel",
                      payload={"entry_token": lease["entry_token"], "confirm": False}),
        ], timeout=PRODUCT_UPLOAD_WAIT_SECONDS, raise_on_timeout=True,
    ))


async def _send_upload_ask(ask):
    async def send():
        try:
            return await ask.send()
        finally:
            # Chainlit does not remove action buttons when an outer wait cancels its send.
            owner = _UPLOAD_LOADING_OWNER.get()
            try:
                for action in getattr(ask, "actions", ()):
                    await asyncio.wait_for(action.remove(), timeout=PRODUCT_UI_SEND_TIMEOUT_SECONDS)
                if (owner is not None and owner["state"]["owner"] is owner
                        and owner["session_generation"] == _session_generation()
                        and not _upload_runtime_active(owner)):
                    await asyncio.wait_for(owner["emitter"].clear("clear_ask"), timeout=PRODUCT_UI_SEND_TIMEOUT_SECONDS)
            except BaseException as exc:
                if owner is not None and owner["state"]["owner"] is owner:
                    owner["state"]["failure"] = exc
                raise
    return await _tracked_upload_ask(send)


async def _show_upload_recovery(lease: dict, reason: str, *, refresh_available=True, submitted=False):
    actions = [cl.Action(name="product_refresh_job", label="Reopen Job",
        payload={"job_id": lease["job_id"], "expected_revision": lease["expected_revision"],
                 "session_generation": lease["session_generation"]})] if refresh_available else []
    guidance = ("Select Reopen Job to read the saved state." if refresh_available
                else f"Send `/resume {lease['job_id']}` to read the saved Job state.")
    await cl.Message(content=_workflow_status(
        reason + (" The original INP was retained; no domain was confirmed. " if submitted else " No INP was submitted. ")
        + guidance + " Upload and computation will not restart automatically."
    ), actions=actions).send()


async def _run_mesh_upload(action: cl.Action) -> None:
    payload = action.payload or {}
    job_id = payload.get("job_id")
    revision = payload.get("expected_revision")
    if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None or not _valid_revision(revision):
        await cl.Message(content=_workflow_status(_UPLOAD_STALE)).send()
        return
    state = _job_ui_state(job_id)
    lease = None
    rejection = _UPLOAD_STALE
    try:
        consistent = await asyncio.to_thread(_upload_job_matches, job_id, revision, payload.get("confirmation_sha256"))
        async with state["lock"]:
            previous = state["busy"]
            if previous is not None and (previous["session_generation"] != _session_generation() or time.monotonic() >= previous["expires_at"]):
                state["busy"] = None
                state["entry"] = None
            if state["busy"] is not None:
                rejection = _UPLOAD_BUSY
            elif (consistent and state["entry"] is not None
                  and state["entry"]["action_id"] == getattr(action, "id", None)
                  and state["entry"]["payload"] == payload
                  and payload.get("session_generation") == _session_generation()
                  and cl.user_session.get("product_job_id") == job_id):
                lease = {**payload, "stage": "AWAIT_CONTRACT", "expires_at": time.monotonic() + PRODUCT_UPLOAD_WAIT_SECONDS}
                state["busy"] = lease
                rejection = None
    except Exception as exc:
        logger.warning("product_upload_entry_rejected")
    if rejection:
        await cl.Message(content=_workflow_status(rejection)).send()
        return
    owner = None
    owner_token = None
    try:
        owner = await _begin_upload_loading(lease)
        owner_token = _UPLOAD_LOADING_OWNER.set(owner)
        timed_out = False
        try:
            answer = await asyncio.wait_for(_ask_for_mesh_contract(lease), timeout=PRODUCT_UPLOAD_WAIT_SECONDS)
        except (asyncio.TimeoutError, AskTimeoutError):
            answer = None
            timed_out = True
        consistent = await asyncio.to_thread(_upload_job_matches, job_id, revision, lease["confirmation_sha256"])
        async with state["lock"]:
            context_current = _lease_context_current(lease, "AWAIT_CONTRACT")
            current = _lease_current(lease, "AWAIT_CONTRACT")
            confirmed = (isinstance(answer, dict) and isinstance(answer.get("payload"), dict)
                         and answer["payload"] == {"entry_token": lease["entry_token"], "confirm": True})
            if consistent and current and confirmed and not timed_out:
                lease["stage"] = "AWAIT_FILE"
                lease["expires_at"] = time.monotonic() + PRODUCT_UPLOAD_WAIT_SECONDS
        _chat_trace("mesh_contract_confirmation", job_id=job_id, revision=revision,
                    accepted=bool(consistent and current and confirmed and not timed_out),
                    timed_out=timed_out, authorization_source="trusted_ui")
        if not context_current or not consistent:
            await _show_upload_recovery(lease, _UPLOAD_STALE, refresh_available=False)
            return
        if timed_out or not current:
            await _show_upload_recovery(lease, "Mesh confirmation timed out after 10 minutes.")
            return
        if not confirmed:
            await _show_upload_recovery(lease, "Mesh requirements were not confirmed or were cancelled.")
            return
        timed_out = False
        try:
            files = await asyncio.wait_for(_ask_for_product_mesh(), timeout=PRODUCT_UPLOAD_WAIT_SECONDS)
        except (asyncio.TimeoutError, AskTimeoutError):
            files = None
            timed_out = True
        async with state["lock"]:
            context_current = _lease_context_current(lease, "AWAIT_FILE")
            current = _lease_current(lease, "AWAIT_FILE")
        if not context_current or not await asyncio.to_thread(_upload_job_matches, job_id, revision, lease["confirmation_sha256"]):
            await _show_upload_recovery(lease, _UPLOAD_STALE, refresh_available=False)
            return
        if timed_out or not current:
            await _show_upload_recovery(lease, "File selection timed out after 10 minutes.")
            return
        if not files:
            await _show_upload_recovery(lease, "No file was selected, or file selection was cancelled.")
            return
        if len(files) != 1:
            raise ValueError("Only one INP file is accepted")
        uploaded = files[0]
        uploaded_content = await asyncio.to_thread(Path(uploaded.path).read_bytes)
        await _bind_uploaded_mesh(
            job_id=job_id, name=uploaded.name,
            content=uploaded_content, expected_revision=revision, lease=lease,
        )
    except Exception as exc:
        logger.warning("product_upload_flow_failed job_id=%s", job_id)
        await cl.Message(content=_format_product_error(
            "Mesh upload", exc, write_state="Check the latest Job state", retry_state="Reopen the Job. No automatic retry",
        ), elements=_technical_elements(_product_error_evidence(exc))).send()
    finally:
        async with state["lock"]:
            if state["busy"] is lease:
                state["busy"] = None
                # Invalidate this action even if its visual removal fails.
                if state["entry"] is not None and state["entry"]["payload"]["entry_token"] == lease["entry_token"]:
                    state["entry"] = None
        try:
            try:
                card = cl.user_session.get(f"product_stage_card:{job_id}:mesh_entry")
                entry_card = state['cards'].get('mesh_entry', {})
                entry_actions = entry_card.get('actions', getattr(card,'actions',()))
                retired = []
                for old_action in list(entry_actions):
                    if old_action.payload.get("entry_token") == lease["entry_token"]:
                        await asyncio.wait_for(old_action.remove(), timeout=PRODUCT_UI_SEND_TIMEOUT_SECONDS)
                        retired.append(old_action)
                component = entry_card.get('component')
                if retired and component is not None:
                    async with entry_card['lock']:
                        # Retire only this entry generation, never its replacement.
                        if any(a.payload.get('entry_token') == lease['entry_token'] for a in entry_card.get('actions',())):
                            props={**component.props,'actions':[],'locked':True,
                                   'message':'Upload question ended. Reopen the task to continue.'}
                            await update_component(component,props)
                            entry_card['model']=props
                            entry_card['actions']=[a for a in entry_card['actions'] if a not in retired]
            finally:
                await _finish_upload_loading(owner)
        finally:
            if owner_token is not None:
                _UPLOAD_LOADING_OWNER.reset(owner_token)

@cl.action_callback("product_start_conformal")
async def start_conformal(action: cl.Action):
    generation = _session_generation()
    if not await _consume_workflow_confirmation(action, "product_start_conformal"):
        await cl.Message(content="Conformal confirmation expired. Reopen the current Job and confirm again.").send()
        return
    payload = action.payload or {}
    try:
        job_id = str(payload["job_id"])
        expected_revision = int(payload["expected_revision"])
        workflow = _workflow()
        async def run_once():
            return await asyncio.to_thread(
                workflow.start_conformal,
                job_id,
                expected_revision=expected_revision,
                authorization={"authorized": True, "authorized_by": "chainlit_user_action"},
            )

        loading = _upload_loading_state(create=False)
        if loading is None:
            runtime_task, created = await _RUNTIME_MANAGER.start_once(job_id, run_once)
        else:
            async with loading["lock"]:
                await _retire_upload_loading_locked(loading)
                runtime_task, created = await _RUNTIME_MANAGER.start_once(job_id, run_once)
    except Exception as exc:
        await _send_runtime_status(_format_product_error(
            "Conformal start request",
            exc,
            write_state="Start and saved state need verification; reopen the Job",
            retry_state="No automatic retry",
        ), evidence=_product_error_evidence(exc))
        await _continue_product_chat("start_conformal", payload.get("job_id"), generation=generation, failed=True)
        return
    if not created:
        await _send_runtime_status(
            f"Job `{job_id}` already has an active runtime task. No new attempt was created. "
            "Use Refresh Job status or the Viewer to follow its progress."
        )
        return
    logger.info(
        "product_conformal_action_accepted job_id=%s expected_revision=%s",
        job_id,
        expected_revision,
    )
    await _send_runtime_status(
        "The conformal start request was accepted. Waiting for the Job record to update. "
        "The remaining steps are unit-cell conversion, conformal generation, and result verification. Use Refresh Job status to check the recorded stage.", job_id=job_id
    )
    try:
        job = await runtime_task
    except Exception as exc:
        await _send_runtime_status(_format_product_error(
            "Adapter or conformal generation",
            exc,
            write_state="Failure evidence may have been recorded",
            retry_state="No automatic retry",
        ), evidence=_product_error_evidence(exc))
        await _continue_product_chat("start_conformal", job_id, generation=generation, failed=True)
        return
    logger.info(
        "product_conformal_action_finished job_id=%s revision=%s status=%s",
        job_id,
        job.get("revision"),
        job.get("status"),
    )
    if job["status"] != "VIEWER_READY":
        await _send_runtime_status(
            _format_product_error(
                recorded_stage((job.get("failure") or {}).get("stage")),
                job.get("failure"),
                write_state="Failure evidence was recorded",
                retry_state="No automatic retry",
            ),
            job_id=job["job_id"],
            card_revision=job["revision"],
            terminal=job["status"] in {"FAILED", "HOLD_RUNTIME_INTERRUPTED"},
            evidence=_product_error_evidence(job.get("failure")))
        await _continue_product_chat("start_conformal", job_id, generation=generation, failed=True)
        return
    await _show_readonly_result(job)
    await _continue_product_chat("start_conformal", job["job_id"], generation=generation)
