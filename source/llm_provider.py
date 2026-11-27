import hashlib
import json
import logging
from contextvars import ContextVar
from functools import wraps
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from openai import NOT_GIVEN



load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)


API_PROVIDER_NAMES = {
    "api",
    "openai",
    "api模型",
    "api 模型",
}

MODEL_CATALOG = {
    "api": ("gpt-4o-mini", "gpt-4", "gpt-5.5", "gpt-5-mini"),
}
API_DEFAULT_SAMPLING_MODELS = {"gpt-5.5", "gpt-5-mini"}
MODEL_DEFAULTS = {
    "api": "gpt-4o-mini",
}
MODEL_RUNTIME_PROFILE_SCHEMA_VERSION = "latticegpt_model_runtime_profile_v1"
STRUCTURED_LLM_RESPONSE_SCHEMA_VERSION = "latticegpt_structured_llm_response_v1"


def _env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def normalize_provider(provider: Optional[str]) -> str:
    value = " ".join((provider or "").strip().lower().split())
    if not value or value in API_PROVIDER_NAMES or value.replace(" ", "") in API_PROVIDER_NAMES:
        return "api"
    raise ValueError("V1 supports only the API backend; select api.")


def chat_agent_timeout_seconds(provider: Optional[str] = None) -> int:
    """Bound API chat turns without changing the existing no-retry contract."""
    normalize_provider(provider or os.getenv("LLM_PROVIDER") or "api")
    return 30


def get_llm_config(provider: Optional[str] = None, model_name: Optional[str] = None) -> dict:
    resolved_provider = normalize_provider(provider or os.getenv("LLM_PROVIDER") or "api")
    temperature = _env_float("LLM_TEMPERATURE", 0.0)
    timeout = _env_int("LLM_TIMEOUT", 60)
    max_retries = _env_int("LLM_MAX_RETRIES", 2)

    base_url = _env_first(
        "API_LLM_BASE_URL",
        "LLM_API_BASE_URL",
        "OPENAI_API_BASE",
    )
    model = model_name or _env_first(
        "API_LLM_MODEL",
        "LLM_API_MODEL",
        default=MODEL_DEFAULTS["api"],
    )
    api_key = _env_first(
        "API_LLM_API_KEY",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
    )
    provider_detail = "openai_compatible"

    return {
        "provider": resolved_provider,
        "provider_detail": provider_detail,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "api_key_present": bool(api_key),
        "temperature": temperature,
        "timeout": timeout,
        "max_retries": max_retries,
    }


def resolve_model_runtime_profile(
    *,
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    selection_source: str,
    session_bound: bool,
    interface_version: str = "chat-interface-v1",
    prompt_template_version: str = "query-plan-prompt-v1",
    temperature: float = 0.0,
    top_p: float | None = None,
    provider_seed_supported: bool = False,
) -> dict:
    """Resolve the product-safe model identity without persisting credentials."""

    if selection_source not in {"session_selection", "central_default"}:
        raise ValueError("unsupported model selection_source")
    if session_bound is not (selection_source == "session_selection"):
        raise ValueError("session_bound must match selection_source")
    if not isinstance(interface_version, str) or not interface_version.strip():
        raise ValueError("interface_version is required")
    if not isinstance(prompt_template_version, str) or not prompt_template_version.strip():
        raise ValueError("prompt_template_version is required")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("temperature must be finite and in [0,2]")
    if top_p is not None and (isinstance(top_p, bool) or not isinstance(top_p, (int, float)) or not math.isfinite(top_p) or not 0 < top_p <= 1):
        raise ValueError("top_p must be null or finite and in (0,1]")
    if not isinstance(provider_seed_supported, bool):
        raise ValueError("provider_seed_supported must be boolean")

    config = get_llm_config(provider=provider, model_name=model_name)
    resolved_provider = config["provider"]
    resolved_model = str(config["model"])
    if resolved_model not in MODEL_CATALOG[resolved_provider]:
        raise ValueError(
            f"model {resolved_model!r} is not available for provider {resolved_provider}"
        )

    profile = {
        "schema_version": MODEL_RUNTIME_PROFILE_SCHEMA_VERSION,
        "provider": resolved_provider,
        "model": resolved_model,
        "base_url": config.get("base_url"),
        "provider_detail": config.get("provider_detail"),
        "selection_source": selection_source,
        "session_bound": bool(session_bound),
        "interface_version": interface_version,
        "prompt_template_version": prompt_template_version,
        "temperature": temperature,
        "top_p": top_p,
        "provider_seed_supported": provider_seed_supported,
        "online_text_determinism": "NOT_CLAIMED",
    }
    if resolved_provider == "api" and resolved_model in API_DEFAULT_SAMPLING_MODELS:
        profile["temperature"] = None  # Provider default; not sent on the wire.
    payload = json.dumps(
        profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    profile["profile_sha256"] = hashlib.sha256(payload).hexdigest().upper()
    return profile


# Default synchronous provider calls share SDK loggers with other callers.
# Suppress verbose transport records only inside this product's own call.
_PRODUCT_PROVIDER_CALL = ContextVar("product_provider_call", default=False)


def _outside_product_call(record):
    return not _PRODUCT_PROVIDER_CALL.get()


for _name in ("openai._base_client", "openai._response", "openai._legacy_response",
              "openai._client", "httpx", "httpcore.connection", "httpcore.http11",
              "httpcore.http2", "httpcore.proxy", "httpcore.socks",
              "langchain_openai.chat_models.base"):
    logging.getLogger(_name).addFilter(_outside_product_call)


def _provider_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _PRODUCT_PROVIDER_CALL.set(True)
        try:
            return function(*args, **kwargs)
        finally:
            _PRODUCT_PROVIDER_CALL.reset(token)
    return wrapped


def _response_text(response):
    value = response if isinstance(response, str) else getattr(response, "content", None)
    if not isinstance(value, str):
        raise ValueError("model response must contain text")
    return value.strip()


@_provider_scope
def invoke_json_object(llm, messages, *, raw_excerpt_limit: int = 1000) -> dict:
    """Invoke one model once and return provider-neutral JSON diagnostics."""

    if not callable(getattr(llm, "invoke", None)):
        raise TypeError("llm must provide an invoke method")
    if isinstance(raw_excerpt_limit, bool) or raw_excerpt_limit < 1:
        raise ValueError("raw_excerpt_limit must be a positive integer")
    response = llm.invoke(messages)
    raw = _response_text(response)
    raw_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
    candidate = raw
    if candidate.startswith("```"):
        candidate = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip()
    result = {
        "schema_version": STRUCTURED_LLM_RESPONSE_SCHEMA_VERSION,
        "raw_sha256": raw_sha,
        "raw_excerpt": None,
        "parsed": None,
        "error": None,
    }
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        result["status"] = "invalid_json"
        result["error"] = f"response is not valid JSON: {exc.msg}"
        return result
    if not isinstance(parsed, dict):
        result["status"] = "invalid_shape"
        result["error"] = "structured response must be a JSON object"
        return result
    result["status"] = "parsed"
    result["parsed"] = parsed
    return result


@_provider_scope
def get_chat_llm(
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    purpose: str = "default",
    temperature: Optional[float] = None,
):
    config = get_llm_config(provider=provider, model_name=model_name)
    if purpose == "chat_agent":
        turn_timeout = chat_agent_timeout_seconds(config["provider"])
        config["timeout"] = max(1, min(config["timeout"], turn_timeout))
        config["max_retries"] = 0
    if temperature is not None:
        config["temperature"] = float(temperature)

    if not config["api_key_present"]:
        raise RuntimeError("当前选择了 api 模型，但未配置对应密钥。")

    kwargs = {
        "model": config["model"],
        "api_key": config["api_key"],
        "temperature": config["temperature"],
        "timeout": config["timeout"],
        "max_retries": config["max_retries"],
    }
    if config.get("base_url"):
        kwargs["base_url"] = config["base_url"]
    llm = ChatOpenAI(**kwargs)
    if config["model"] in API_DEFAULT_SAMPLING_MODELS:
        # The locked LangChain client always supplies a temperature. The SDK
        # sentinel omits it without changing the client or reasoning defaults.
        return llm.bind(temperature=NOT_GIVEN)
    return llm


@_provider_scope
def invoke_chat_action(llm, messages, *, trace=None) -> dict:
    """One invocation, strict JSON, no compatibility retry or model-authored execution."""
    started = time.monotonic()
    def emit(event, **fields):
        if trace is not None:
            try:
                trace(event, elapsed_seconds=time.monotonic()-started, **fields)
            except Exception:
                pass  # Diagnostic transport must not change the single invocation.

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate chat action key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"non-finite chat value: {value}")

    stage = 'model_call'
    emit('model_call_started', stage=stage)
    try:
        response = llm.invoke(messages)
        stage = 'response_text'
        raw = _response_text(response)
        emit('model_raw_response', stage=stage, raw_length=len(raw))
        if len(raw) > 24000:
            raise ValueError("chat action exceeds response budget")
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
        stage = 'json_parse'
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError("chat action must be an object")
        emit('model_json_parsed', stage=stage)
        return value
    except Exception as exc:
        emit('model_invocation_error', stage=stage, error_type="timeout" if isinstance(exc, TimeoutError) else stage)
        raise


def describe_llm_config_safe(config: Optional[dict] = None) -> str:
    cfg = dict(config or get_llm_config())
    safe_items = {
        "provider": "api" if cfg.get("provider") == "api" else "unrecognized",
        "model": cfg.get("model") if cfg.get("model") in MODEL_CATALOG["api"] else "unrecognized",
        "base_url_configured": bool(cfg.get("base_url")),
        "api_key_present": bool(cfg.get("api_key_present")),
    }
    for key in ("temperature", "timeout", "max_retries"):
        value = cfg.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            safe_items[key] = value
    return ", ".join(f"{key}={value}" for key, value in safe_items.items())


def format_llm_connection_error(exc: Exception, provider: Optional[str] = None) -> str:
    try:
        normalize_provider(provider or os.getenv("LLM_PROVIDER") or "api")
    except ValueError:
        return "V1 仅支持 API 后端，请选择 api。"
    message = str(exc or "")
    lower_message = message.lower()
    if "未配置对应密钥" in message or ("api key" in lower_message and "missing" in lower_message):
        return "当前选择了 api 模型，但未配置对应密钥。请在 .env 或系统环境变量中配置后重启应用。"
    return "API 模型连接失败。请检查 API_LLM_BASE_URL、API_LLM_MODEL 和 API_LLM_API_KEY 是否配置正确。"
