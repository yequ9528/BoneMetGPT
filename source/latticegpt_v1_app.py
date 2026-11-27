"""Chainlit product-only A-page entry for LatticeGPT V1."""

from __future__ import annotations

from datetime import datetime, timezone
from importlib.metadata import version
import json
from os import getpid
from pathlib import Path
import stat
import sys
from uuid import uuid4

import chainlit as cl
from chainlit.config import config as _chainlit_runtime_config
from chainlit.input_widget import Select

from llm_provider import MODEL_CATALOG, MODEL_DEFAULTS
from product_workflow.config import load_latticegpt_config
from product_workflow.service_events import ServiceEventHealth
from product_workflow.preflight import require_preflight
from product_workflow.chainlit_ui import (
    configure_product_ui,
    handle_product_message,
    reconcile_product_runtime_state,
    send_product_start_card,
    invalidate_product_waits,
)

_attachment_startup_attempted = False
_chat_event_health = ServiceEventHealth()


def _require_plain_attachment_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f"Attachment startup HOLD: non-plain directory: {path}")


async def _configure_product_attachment_root() -> None:
    """Chainlit 2.9.6 internal adaptation; errors must escape lifespan startup."""
    global _attachment_startup_attempted
    if _attachment_startup_attempted:
        raise RuntimeError("Attachment startup HOLD: repeated startup/rebind")
    _attachment_startup_attempted = True
    if version("chainlit") != "2.9.6":
        raise RuntimeError("Attachment startup HOLD: unsupported Chainlit version")
    config_module = sys.modules.get("chainlit.config")
    server_module = sys.modules.get("chainlit.server")
    if (config_module is None or server_module is None
            or getattr(config_module, "config", None) is not _chainlit_runtime_config
            or getattr(server_module, "config", None) is not _chainlit_runtime_config):
        raise RuntimeError("Attachment startup HOLD: missing Chainlit interface")
    if _chainlit_runtime_config.run.watch:
        raise RuntimeError("Attachment startup HOLD: watch/reload is unsupported")
    default_root = (Path(config_module.APP_ROOT) / ".files").resolve()
    if (getattr(config_module, "FILES_DIRECTORY", None) != default_root
            or getattr(server_module, "FILES_DIRECTORY", None) != default_root):
        raise RuntimeError("Attachment startup HOLD: initial FILES_DIRECTORY mismatch")

    product = load_latticegpt_config()
    runtime = Path(product.paths.runtime_root)
    if not runtime.is_absolute() or runtime != runtime.resolve(strict=True):
        raise ValueError("Attachment startup HOLD: runtime path is not canonical")
    _require_plain_attachment_directory(runtime)
    if runtime == default_root or runtime.is_relative_to(default_root):
        raise ValueError("Attachment startup HOLD: runtime overlaps the old attachment root")
    config_sha = product.paths.config_sha256
    if (not isinstance(config_sha, str) or len(config_sha) != 64
            or any(char not in "0123456789ABCDEFabcdef" for char in config_sha)):
        raise ValueError("Attachment startup HOLD: missing config identity")
    startup_id = uuid4().hex
    if len(startup_id) != 32 or any(char not in "0123456789abcdef" for char in startup_id):
        raise ValueError("Attachment startup HOLD: invalid startup path component")
    parent = runtime / "chainlit_attachments"
    if not parent.exists():
        parent.mkdir()
    _require_plain_attachment_directory(runtime)
    _require_plain_attachment_directory(parent)
    child = parent / startup_id
    if child.resolve().parent != parent or child == default_root:
        raise ValueError("Attachment startup HOLD: attachment path escaped its parent")
    child.mkdir()  # Exclusive: one ID, no reuse, fallback or retry.
    for path in (runtime, parent, child):
        _require_plain_attachment_directory(path)

    config_module.FILES_DIRECTORY = child
    server_module.FILES_DIRECTORY = child
    for path in (runtime, parent, child):
        _require_plain_attachment_directory(path)
    config_root = config_module.FILES_DIRECTORY
    server_root = server_module.FILES_DIRECTORY
    if config_root != child or server_root != child or child.resolve().parent != parent:
        raise RuntimeError("Attachment startup HOLD: bound FILES_DIRECTORY mismatch")
    witness = {
        "schema": "latticegpt.attachment_root.v1",
        "startup_id": startup_id,
        "pid": getpid(),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "generation": product.paths.active_generation,
        "config_sha256": config_sha,
        "config_files_directory": str(config_root),
        "server_files_directory": str(server_root),
        "equality": config_root == server_root,
    }
    with (child / ".latticegpt_attachment_root_v1.json").open(
        "x", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(witness, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")

    _chat_event_health.start(runtime, "chat")
    previous_shutdown = _chainlit_runtime_config.code.on_app_shutdown

    async def close_service_events():
        try:
            if previous_shutdown is not None:
                await previous_shutdown()
        finally:
            _chat_event_health.close()

    _chainlit_runtime_config.code.on_app_shutdown = close_service_events


# The public decorator swallows Exception in 2.9.6. Register the raw async
# callback so a failure occurs before lifespan yield and its cleanup block.
_chainlit_runtime_config.code.on_app_startup = _configure_product_attachment_root


_SELECTABLE_PROVIDERS = tuple(MODEL_CATALOG)


def _selectable_session_model() -> tuple[str, str]:
    previous_provider = cl.user_session.get("provider")
    previous_model = cl.user_session.get("model_name")
    provider = previous_provider if previous_provider in _SELECTABLE_PROVIDERS else "api"
    model = previous_model if previous_model in MODEL_CATALOG[provider] else MODEL_DEFAULTS[provider]
    if (provider, model) != (previous_provider, previous_model):
        if previous_provider is not None or previous_model is not None:
            from product_workflow.chainlit_ui import invalidate_product_chat
            invalidate_product_chat()
        cl.user_session.set("provider", provider)
        cl.user_session.set("model_name", model)
    return provider, model


async def send_product_model_settings() -> None:
    provider, model = _selectable_session_model()
    await cl.ChatSettings([
        Select(id="chat_model", label="Chat model",
               items={_model_label(p, m): f"{p}/{m}"
                      for p in _SELECTABLE_PROVIDERS for m in MODEL_CATALOG[p]},
               initial_value=f"{provider}/{model}",
               description="Used for chat and tool coordination. Apply to use for new messages."),
    ]).send()


def _model_label(provider: str, model: str) -> str:
    names = {"qwen3.5:9b": "Qwen 3.5 9B", "qwen2.5:14b-instruct": "Qwen 2.5 14B",
             "gpt-oss:120b-cloud": "GPT-OSS 120B", "gpt-4o-mini": "GPT-4o mini", "gpt-4": "GPT-4",
             "gpt-5.5": "GPT-5.5", "gpt-5-mini": "GPT-5 mini"}
    sources = {"api": "API"}
    return f"{names.get(model, model)} · {sources[provider]}"


@cl.on_chat_start
async def on_v1_chat_start() -> None:
    config = load_latticegpt_config()
    preflight = require_preflight(config, profile="full_product")
    configure_product_ui(config, preflight=preflight)
    await invalidate_product_waits()
    cl.user_session.set("provider", "api")
    cl.user_session.set("model_name", MODEL_DEFAULTS["api"])
    cl.user_session.set("product_mode", True)
    await reconcile_product_runtime_state()
    await send_product_start_card()
    await send_product_model_settings()


@cl.on_chat_end
async def on_v1_chat_end() -> None:
    await invalidate_product_waits()


@cl.on_settings_update
async def on_v1_settings_update(settings: dict) -> None:
    from product_workflow.chainlit_ui import invalidate_product_chat
    current_provider, current_model = _selectable_session_model()
    if "chat_model" in settings:
        choices = {f"{p}/{m}": (p, m) for p in _SELECTABLE_PROVIDERS for m in MODEL_CATALOG[p]}
        selected = choices.get(str(settings["chat_model"]))
        if selected is None:
            await cl.Message(content="Model selection was not recognized. Choose a listed model.").send()
            return
        provider, model_name = selected
        settings = {"provider": provider, f"model_{provider}": model_name}
    provider = str(settings.get("provider") or settings.get("Provider") or current_provider).lower()
    if provider not in _SELECTABLE_PROVIDERS:
        await cl.Message(content="Model selection was not recognized. Choose a listed model.").send()
        return
    model_name = str(
        settings.get(f"model_{provider}")
        or settings.get("model_name")
        or settings.get("Model")
        or (current_model if provider == current_provider else None)
        or MODEL_DEFAULTS[provider]
    )
    if model_name not in MODEL_CATALOG[provider]:
        model_name = MODEL_DEFAULTS[provider]
    if (provider, model_name) != (current_provider, current_model):
        invalidate_product_chat()
    else:
        return
    cl.user_session.set("provider", provider)
    cl.user_session.set("model_name", model_name)
    await cl.Message(
        content=f"Chat model: **{_model_label(provider, model_name)}**. Applies to new messages."
    ).send()


@cl.on_message
async def on_v1_message(message: cl.Message) -> None:
    if (message.content or "").strip() == "/log-health":
        await cl.Message(content=f"Chat event log: {_chat_event_health.code}").send()
        return
    _selectable_session_model()
    if await handle_product_message(message):
        return
    await cl.Message(
        content=(
            "Ask about the workflow, query reference data, or provide a full Job ID. "
            "Use `/resume job-...` to reopen a Job. Design actions require explicit confirmation. "
            "NOT_FULL_DUAL_SPACE_FUSION."
        )
    ).send()
