"""Product-only readonly Viewer backend for LatticeGPT V1."""

from __future__ import annotations

from io import BytesIO
from collections import OrderedDict
import logging
import threading
import hashlib

from werkzeug.serving import WSGIRequestHandler
from werkzeug.exceptions import RequestedRangeNotSatisfiable
from flask import Flask, jsonify, request, send_file

from product_viewer_exports import FILES, SCENES, DeliveryUnavailable, clean_image, delivery, scene_bytes, snapshot
from product_viewer_payloads import build_product_job_viewer_payload
from product_workflow.artifacts import (
    load_viewer_readonly_compatibility,
    resolve_product_package,
)
from product_workflow.service_events import ServiceEventHealth
from product_workflow.config import LatticeGPTConfig, load_latticegpt_config
from product_workflow.preflight import PreflightBindingV1, require_preflight
from product_workflow.store import JOB_ID_PATTERN, JobStore
from product_workflow.contracts import canonical_sha256
from product_workflow.ui_previews import STANDARD_CELL_RENDERER, render_standard_candidate_png


class ProductViewerApp(Flask):
    def log_exception(self, exc_info):
        logging.getLogger("product_viewer_server").error("viewer_request_failed code=PRODUCT_VIEWER_INTERNAL_ERROR")


def create_product_viewer_app(
    config: LatticeGPTConfig,
    *,
    preflight: PreflightBindingV1,
    payload_builder=build_product_job_viewer_payload,
) -> Flask:
    if not isinstance(preflight, PreflightBindingV1) or preflight.profile != "viewer_only":
        raise TypeError("viewer_only PreflightBindingV1 is required")
    app = ProductViewerApp(__name__)
    app.event_log_health = ServiceEventHealth()
    store = JobStore(
        config.paths.runtime_root,
        viewer_compatibility=load_viewer_readonly_compatibility(preflight),
        reader_identity={
            "generation": config.paths.active_generation,
            "source_manifest_sha256": preflight.source_manifest_sha256,
            "active_release_binding_sha256": preflight.active_release_binding_sha256,
        },
    )
    app.extensions["latticegpt_job_store"] = store
    app.extensions["latticegpt_manifest_sha256"] = {
        "source": preflight.source_manifest_sha256,
        "artifact": preflight.artifact_manifest_sha256,
        "environment": preflight.environment_manifest_sha256,
    }

    @app.after_request
    def configured_cors(response):
        if request.headers.get("Origin") == config.services.viewer_frontend_origin:
            response.headers["Access-Control-Allow-Origin"] = (
                config.services.viewer_frontend_origin
            )
            response.headers["Access-Control-Expose-Headers"] = "ETag"
            response.headers["Vary"] = "Origin"
        return response

    @app.get("/api/viewer/product/log-health")
    def log_health():
        response = jsonify({"role": "viewer", "code": app.event_log_health.code})
        response.cache_control.no_store = True
        return response

    @app.get("/api/viewer/product/jobs/<job_id>")
    def product_job(job_id: str):
        if JOB_ID_PATTERN.fullmatch(job_id) is None:
            return _error("Product Job not found.", 404, "JOB_NOT_FOUND", job_id)
        include = request.args.get('include')
        if include not in (None, 'original_mesh'):
            return _error('Unsupported Viewer projection.', 400, 'INVALID_VIEWER_RESPONSE', job_id)
        try:
            options = {'include_original_mesh': True} if include == 'original_mesh' else {}
            payload = payload_builder(
                job_id,
                store=store,
                conformal_root=config.paths.conformal_root,
                **options,
            )
            payload['navigation'] = {'chat_url': f'http://{config.services.chainlit_host}:{config.services.chainlit_port}/'}
        except Exception as exc:
            return _read_error(exc, job_id, "Job")
        return jsonify({"status": "success", "data": payload})

    preview_cache = OrderedDict()
    preview_lock = threading.Lock()

    @app.get('/api/viewer/product/jobs/<job_id>/candidates/<candidate_id>/preview.png')
    def candidate_preview(job_id, candidate_id):
        if JOB_ID_PATTERN.fullmatch(job_id) is None:
            return _error('Product Job not found.',404,'JOB_NOT_FOUND',job_id)
        try:
            job = store.get_consistent_for_viewer(job_id)
            collection = job.get('candidate_set') or {}
            identity = collection.get('candidate_set_sha256')
            if (not identity or canonical_sha256({k:v for k,v in collection.items() if k != 'candidate_set_sha256'}) != identity
                or request.args.get('candidate_set_sha256') != identity
                or request.args.get('renderer_version') != STANDARD_CELL_RENDERER):
                return _error('Candidate preview identity changed. Refresh the Job.',409,'FILE_IDENTITY_MISMATCH',job_id)
            candidates = [c for c in collection.get('candidates',[]) if c.get('candidate_id') == candidate_id]
            if len(candidates) != 1:
                return _error('Candidate not found.',404,'FILE_MISSING',job_id)
            key = (identity,candidate_id,STANDARD_CELL_RENDERER)
            with preview_lock:
                png = preview_cache.get(key)
                if png is None:
                    png = clean_image(render_standard_candidate_png(candidates[0]), "PNG")
                    preview_cache[key] = png
                    while len(preview_cache)>24: preview_cache.popitem(last=False)
                preview_cache.move_to_end(key)
            response = send_file(BytesIO(png),mimetype='image/png',download_name=f'{candidate_id}.png')
            response.headers['Cache-Control'] = 'no-store'
            return response
        except Exception as exc:
            return _read_error(exc,job_id,'Job')

    @app.get("/api/viewer/product/jobs/<job_id>/deliver/<role>", defaults={"scene": False})
    @app.get("/api/viewer/product/jobs/<job_id>/scene/<role>", defaults={"scene": True})
    def result_resource(job_id, role, scene):
        if not JOB_ID_PATTERN.fullmatch(job_id) or role not in (SCENES if scene else FILES):
            return _error("File unavailable.", 404, "FILE_MISSING", job_id)
        try:
            job = store.get_consistent_for_viewer(job_id)
            package = resolve_product_package(store, job_id, config.paths.conformal_root)
            if request.args.get("snapshot") != snapshot(job, package, role):
                return _error("Result changed. Reopen the result.", 409, "FILE_IDENTITY_MISMATCH", job_id)
            if scene:
                data, mime = scene_bytes(package, role)
                filename = None
            else:
                filename, mime = FILES[role]
                data = delivery(job, package, role)
            if store.get_consistent_for_viewer(job_id) != job:
                return _error("Result changed. Reopen the result.", 409, "FILE_IDENTITY_MISMATCH", job_id)
            response = send_file(BytesIO(data), mimetype=mime, as_attachment=not scene,
                                 download_name=filename, etag=hashlib.sha256(data).hexdigest(), conditional=True)
            response.cache_control.no_store = True
            return response
        except RequestedRangeNotSatisfiable:
            raise
        except DeliveryUnavailable:
            return _error("This result does not provide the requested file.", 404, "FILE_MISSING", job_id)
        except Exception as exc:
            return _read_error(exc, job_id, "file")

    return app


class ProductRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        # Polling the log sink must not itself write to that sink.
        if (getattr(self, "command", None) in {"GET", "HEAD"}
                and getattr(self, "environ", {}).get("PATH_INFO") == "/api/viewer/product/log-health"
                and str(code) == "200"):
            return
        # Werkzeug otherwise logs the request target, including its query.
        status = str(code)
        logging.getLogger("product_viewer_server").info("viewer_request status=%s", status if status.isdigit() else "unknown")

    def log(self, type, message, *args):
        level = logging.ERROR if type == "error" else logging.INFO
        logging.getLogger("product_viewer_server").log(level, "viewer_transport_event")


def run_product_viewer_server(config: LatticeGPTConfig | None = None) -> None:
    selected = load_latticegpt_config() if config is None else config
    preflight = require_preflight(selected, profile="viewer_only")
    app = create_product_viewer_app(selected, preflight=preflight)
    app.event_log_health.start(selected.paths.runtime_root, "viewer")
    try:
        app.run(
            host=selected.services.viewer_host,
            port=selected.services.viewer_port,
            debug=False,
            use_reloader=False,
            request_handler=ProductRequestHandler,
        )
    finally:
        app.event_log_health.close()


def _error(message: str, status_code: int, code: str, job_id: str):
    return jsonify({"status": "error", "data": None, "message": message, "code": code,
                    "job_id": job_id if JOB_ID_PATTERN.fullmatch(job_id) else None}), status_code


def _read_error(exc, job_id, role):
    code = getattr(exc, "code", None)
    known = {"JOB_NOT_FOUND", "JOB_CONTRACT_INVALID", "PACKAGE_CONTRACT_INVALID", "FILE_MISSING", "FILE_IDENTITY_MISMATCH", "PRODUCT_VIEWER_INTERNAL_ERROR"}
    if not isinstance(code, str) or code not in known:
        code = ("FILE_MISSING" if isinstance(exc, FileNotFoundError) else
                "JOB_CONTRACT_INVALID" if isinstance(exc, ValueError) and role == "Job" else
                "PACKAGE_CONTRACT_INVALID" if isinstance(exc, ValueError) else "PRODUCT_VIEWER_INTERNAL_ERROR")
    logging.getLogger("product_viewer_server").warning(
        "viewer_read_rejected role=%s code=%s", role, code,
        extra={"product_event": {"event": "viewer_read_rejected", "role": role, "code": code}},
    )
    status = 404 if code in {"JOB_NOT_FOUND", "FILE_MISSING"} else 500 if code == "PRODUCT_VIEWER_INTERNAL_ERROR" else 400
    message = f"Product {role} not found." if status == 404 else f"Invalid product {role} request." if status == 400 else "Product Viewer request failed."
    return _error(message, status, code, job_id)


if __name__ == "__main__":
    run_product_viewer_server()
