"""Product-owned service events, independent of native stdout/stderr."""
import json
import logging
import os
from pathlib import Path
import stat
from uuid import uuid4


_CHAT = "latticegpt.chat"
_RUNTIME = "product_workflow.runtime_manager"
_UI = "product_workflow.chainlit_ui"
_QUERY = "product_workflow.formal_150w_query"
_VIEWER = "product_viewer_server"
_UI_EVENTS = frozenset("""
product_upload_card_update_failed product_upload_card_notice_failed candidate_feedback_unavailable
product_preview_failed product_preview_delivery_failed product_runtime_status_send_timed_out
product_runtime_status_send_failed product_chat_receipt_failed product_chat_confirmation_rejected
product_target_create_uncertain product_target_created product_target_ui_pending
product_domain_preview_failed product_current_job_read_failed product_readonly_query_unrecognized
product_attachment_processing_failed product_reference_action_rejected candidate_confirmation_ui_pending
product_upload_entry_rejected product_upload_flow_failed product_conformal_action_accepted
product_conformal_action_finished""".split())


class ServiceEvents(logging.Handler):
    """Write approved event fields only. Never format a general LogRecord."""
    def __init__(self, runtime_root, role):
        super().__init__()
        if role not in ("chat", "viewer"):
            raise ValueError("unrecognized service log role")
        directory = Path(runtime_root) / "startup_logs"
        directory.mkdir(parents=True, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or directory.is_symlink()
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("invalid service event directory")
        self.path = directory / f"{role}-{os.getpid()}-{uuid4().hex}.events.jsonl"
        self.stream = self.path.open("x", encoding="utf-8", newline="\n")
        self.health_code = "product_event_log_ok"
        names = (_CHAT, _UI, _QUERY, _RUNTIME) if role == "chat" else (_VIEWER,)
        self.sources = [logging.getLogger(name) for name in names]
        self.levels = [source.level for source in self.sources]
        for source in self.sources:
            if source.getEffectiveLevel() > logging.INFO:
                source.setLevel(logging.INFO)
            source.addHandler(self)

    def emit(self, record):
        if record.name == _CHAT:
            # Only ChatCase.trace attaches this already projected payload.
            event = getattr(record, "product_event", None)
            if not isinstance(event, dict):
                return
        elif record.name == _VIEWER and record.msg == "viewer_request status=%s" and record.args:
            status = record.args[0]
            valid = isinstance(status, str) and len(status) == 3 and status.isascii() and status.isdigit()
            event = {"event": "viewer_request", "http_status": int(status) if valid else None}
        elif record.name == _RUNTIME:
            event = {"event": "runtime_task_failed"}
        elif record.name == _QUERY:
            event = getattr(record, "product_event", {"event": "readonly_query_failed"})
        elif record.name == _UI:
            label = record.msg.split(" ", 1)[0] if isinstance(record.msg, str) else ""
            event = {"event": label if label in _UI_EVENTS else "chat_ui_event"}
        elif record.name == _VIEWER:
            event = getattr(record, "product_event", {"event": "viewer_transport_or_read_event"})
        else:
            return
        # Do not use Handler.handleError: its default reports original arguments.
        try:
            line = {"time_unix": record.created, "level": record.levelname, **event}
            self.stream.write(json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n")
            self.stream.flush()
        except (OSError, ValueError, TypeError):
            # Latch event loss even if a later record writes successfully.
            self.health_code = "product_event_write_failed"

    def close(self):
        for source, level in zip(getattr(self, "sources", ()), getattr(self, "levels", ())):
            if self in source.handlers:
                source.removeHandler(self)
                source.setLevel(level)
        stream = getattr(self, "stream", None)
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except OSError:
                self.health_code = "product_event_write_failed"
        super().close()


def open_service_events(runtime_root, role):
    try:
        return ServiceEvents(runtime_root, role)
    except (OSError, ValueError):
        return None


class ServiceEventHealth:
    """One service lifetime. Expose only its current fixed code to requests."""
    NOT_STARTED = "product_event_log_not_started"

    def __init__(self):
        self._current = self.NOT_STARTED

    def start(self, runtime_root, role):
        if self._current != self.NOT_STARTED:
            raise RuntimeError("product_event_log_already_started")
        self._current = open_service_events(runtime_root, role) or "product_event_log_unavailable"

    @property
    def code(self):
        current = self._current
        return current.health_code if isinstance(current, ServiceEvents) else current

    def close(self):
        previous = self._current
        self._current = self.NOT_STARTED
        if isinstance(previous, ServiceEvents):
            previous.close()

