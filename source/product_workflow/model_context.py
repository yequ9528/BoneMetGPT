"""Read-only projections for model messages. Execution uses the original objects."""
from __future__ import annotations

import re

from product_viewer_display import (
    KNOWN_CLAIMS, choice, codes, count, enum, identifier, mapping, nine, number, project, timestamp,
)
from .contracts import BASE_MATERIAL_NAME, STIFFNESS_NAMES
from .formal_150w_query import DATABASE_REQUEST_SCHEMA, FILTER_OPERATORS, QUERY_FIELDS
from .job_state import KNOWN_STATUSES
from .presentation import recorded_error_code, result_file_roles
from .store import JOB_ID_PATTERN


ACTIONS = ("create_job", "confirm_target_and_generate", "select_candidate", "request_mesh_upload", "start_conformal")
QUERY_KINDS = ("records", "count", "overview")
READ_STATES = ("ok", "empty", "error", "pending", "not_queried")
EXCLUSIONS = ("CAD_READY_REQUIRED", "B14_PASS_REQUIRED", "TARGET_RANGE_PASS_REQUIRED", "COMPLETE_FINITE_9D_REQUIRED")
FAILURE_STAGES = (*KNOWN_STATUSES, "FORMAL_BUNDLE_PREFLIGHT", "action_prepare", "result_read")


def boolean(value):
    return value if type(value) is bool else None


def job_id(value):
    return value if isinstance(value, str) and JOB_ID_PATTERN.fullmatch(value) else None


def candidate_id(value):
    valid = identifier(value)
    if valid and not re.match(r"(?i)(?:sk-|ghp_|github_pat_|AKIA[A-Z0-9]{16}$|Bearer[-_])", valid):
        return valid
    return None


def _records(value, fields, limit):
    if isinstance(value, list):
        return [project(item, fields) for item in value[:limit] if isinstance(item, dict)]
    return None


def _nested(fields):
    return lambda value: project(value, fields) if isinstance(value, dict) else None


def _numbers(*names):
    return dict.fromkeys(names, number)


def _counts(*names):
    return dict.fromkeys(names, count)


def _filter_value(value):
    if isinstance(value, list) and len(value) == 2 and all(number(item) is not None for item in value):
        return list(value)
    return number(value)


def query_facts(value):
    condition = {"field": enum(*QUERY_FIELDS), "op": enum(*FILTER_OPERATORS, "between"),
                 "value": _filter_value, "unit": enum("id", "ratio", "%", "GPa", "C/Eb")}
    order = {"field": enum(*QUERY_FIELDS), "direction": enum("asc", "desc")}
    fields = {"schema_version": enum(DATABASE_REQUEST_SCHEMA), "kind": enum(*QUERY_KINDS),
              "conditions": lambda x: _records(x, condition, 8), "filters": lambda x: _records(x, condition, 8),
              "order_by": lambda x: _records(x, order, 3), "limit": count, "inherit": boolean}
    # The one optional read requirement contains a request, never another requirement.
    return project(value, {**fields, "request": _nested(fields)})


def _error(value):
    if not isinstance(value, dict) or not value:
        return None
    return {"code": recorded_error_code(value),
            **project(value, {"stage": enum(*FAILURE_STAGES),
                              "compute_stage": enum("preflight", "inference", "result_conversion", "data2FEM", "diameter_calculate"),
                              "can_query_again": boolean, "automatic_retry": boolean,
                              "requery_condition": enum("CHANGE_REQUEST", "AFTER_IDLE", "NARROW_REQUEST", "REPAIR_BINDING",
                                                        "RESTORE_SOURCE", "INSPECT_DIAGNOSTIC"),
                              "limit_reason": enum("TIME_BUDGET", "INSTRUCTION_BUDGET")})}


def _database(value, *, planning=False):
    source = mapping(value)
    result = project(source, {"kind": enum(*QUERY_KINDS), "status": enum(*READ_STATES), "error": _error})
    data = mapping(source.get("data"))
    row = {"sample_id": count, **_numbers("relative_density", "diameter", *STIFFNESS_NAMES)}
    fields = {"rows": lambda x: _records(x, row, 5), **_counts("returned_count", "matched_count")}
    if not planning:
        units = ("dimensionless_C_over_Eb", "dimensionless", "integer_identifier")
        fields.update(scanned_rows=count, eligible_rows=count, statistics_time=timestamp,
                      fields=lambda x: _records(x, {"name": enum(*QUERY_FIELDS), "unit": enum(*units)}, len(QUERY_FIELDS)),
                      eligibility_policy=_nested({**_counts("is_singular", "is_pd_full", "has_periodic_mismatch", "is_degenerate"),
                                                  "selected_9d": enum("finite_positive")}))
    if data:
        result["data"] = project(data, fields)
    if result.get("kind") == "records":
        result["units"] = {"stiffness": "C/Eb", "relative_density": "ratio", "diameter": "normalized_ratio"}
        if planning:
            result["source"] = "formal_150w_database"
    if not planning:
        result.update(project(source, {"non_claims": lambda x: codes(x, KNOWN_CLAIMS),
                                       "normalized_request": query_facts}))
    return result


def _p09(value):
    reasons = ("COMPLETE_FINITE_9D_REQUIRED", "POSITIVE_DIAGONAL_REQUIRED", "NEGATIVE_COUPLING_UNSUPPORTED_SCOPE",
               "NONPOSITIVE_REDUCED_9D_STABILITY", "BORDERLINE_REDUCED_9D_STABILITY", "INVALID_FLOAT32_CONVERSION",
               "P09_NUMERIC_EVALUATION_FAILED", "NONFINITE_P09_RESULT")
    return project(value, {"status": enum("PASS", "HOLD", "FAIL"), "evaluated": boolean,
                           "reasons": lambda x: codes(x, reasons), "error_code": enum(*reasons),
                           **_numbers("lambda_min", "lambda_max", "lambda_min_over_lambda_max", "threshold")})


def _target(value, *, planning=False):
    source = {"kind": enum("formal_150w_database", "csv_upload", "frozen_technical_reference",
                           "determinism_technical_reference_target"),
              "units": enum("dimensionless_C_over_Eb", "GPa"), **_counts("sample_id", "record_index", "record_count")}
    fields = {"status": enum("TARGET_PROPOSED", "TARGET_CONFIRMED"), "confirmed": boolean,
              "source": _nested(source), "physical_9d_gpa": nine, "normalized_9d": nine}
    if planning:
        target = project(value, fields)
        units = {name: unit for name, unit in (("physical_9d_gpa", "GPa"), ("normalized_9d", "C/Eb")) if target.get(name)}
        if units:
            target["units"] = units
        return target
    return project(value, {**fields, "base_material": _nested({"name": enum(BASE_MATERIAL_NAME), "elastic_modulus_gpa": number}),
                           "range_status": enum("UNCHECKED", "PASS", "HOLD"), "scaler_range_status": enum("PASS", "HOLD"),
                           "validation_failure_kind": enum("SCALER_RANGE", "P09", "BOTH", "INVALID_CONVERSION"),
                           "p09_validation": _p09, "non_claims": lambda x: codes(x, KNOWN_CLAIMS)})


def job_facts(value, *, phase="execution"):
    source = mapping(value)
    result = project(source, {"job_id": job_id, "revision": count, "status": enum(*KNOWN_STATUSES),
                              "candidate_confirmation": enum("CONFIRMED", "UNCONFIRMED", "INVALID"),
                              "allowed_actions": lambda x: codes(x, ACTIONS)})
    candidate = {"candidate_id": candidate_id, "rank": count, "eligible": boolean}
    ranking = project(source.get("ranking"), {"metric": enum("score", "nmse"), "label": enum("P-10", "NMSE"),
                    "lower_is_better": boolean, "tie_break": enum("legacy_p10", "source_order")})
    if ranking:
        result["ranking"] = ranking
    if phase in {"planning", "cold_conversation"}:
        result.update(project(source, {"target": lambda value: _target(value, planning=True)}))
        if source.get("candidates"):
            candidate.update(origin=enum("inverse_generated_structure"), relative_density=number,
                             normalized_diameter=number)
            units = {"relative_density": "ratio", "normalized_diameter": "normalized_ratio"}
            if source.get("prediction_units") == "GPa":
                candidate["predicted_stiffness"] = nine
                units["predicted_stiffness"] = "GPa"
            if phase == "cold_conversation":
                candidate["nmse"] = number
                if any(number(mapping(row).get("nmse")) is not None for row in source["candidates"][:3]):
                    units["nmse"] = "ratio"
                if ranking.get("metric") == "score":
                    candidate["score"] = number
                if any(nine(mapping(row).get("predicted_stiffness")) for row in source["candidates"][:3]):
                    result["candidate_interpretation"] = {"prediction": "forward_surrogate", "validation": "surrogate prediction only"}
            result["candidate_units"] = units
    else:
        candidate.update(origin=enum("inverse_generated_structure"), nmse=number, source_order=count,
                         normalized_diameter=number, relative_density=number, p09_validation=_p09,
                         predicted_stiffness=nine, stiffness_delta_gpa=nine, exclusion_reasons=lambda x: codes(x, EXCLUSIONS))
        if ranking.get("metric") == "score":
            candidate["score"] = number
        domain = {"units": enum("mm"), **_counts("selected_element_count", "selected_node_count", "excluded_element_count",
                                               "excluded_solid_count"), "non_claims": lambda x: codes(x, KNOWN_CLAIMS)}
        mesh = {"precheck_status": enum("PASS", "HOLD", "FAIL", "passed", "failed"), "design_domain": _nested(domain)}
        diagnostics = {"target_index": count, "diversity_threshold": number, "diversity_fallback": boolean}
        inverse = {"status": enum("success", "warning", "failed", "missing", "invalid_result_type", "invalid_summary"),
                   "algorithm": _nested({"version": enum("truss_latent_20261110"), "prediction_kind": enum("forward_surrogate")}),
                   "diagnostics": lambda x: _records(x, diagnostics, 3), "failure": _error}
        result.update(project(source, {"target": _target, "prediction_units": enum("GPa"),
                                       "mesh": _nested(mesh), "failure": _error, "runtime_permitted": boolean,
                                       "inverse_summary": _nested(inverse), "non_claims": lambda x: codes(x, KNOWN_CLAIMS)}))
        if source.get("candidates"):
            result["candidate_interpretation"] = {
                "origin": "inverse_generated_structure", "prediction": "forward_surrogate",
                "nmse": "target matching squared relative L2 error (ratio)",
                "delta": "predicted minus target in GPa", "relative_density": "material fraction (ratio)",
                "normalized_diameter": "dimensionless", "validation": "surrogate prediction only"}
    result.update(project(source, {"candidates": lambda x: _records(x, candidate, 3),
                  "selection": _nested({"selected_candidate_id": candidate_id, "recommended_candidate_id": candidate_id,
                                        "selected_rank": count, "selection_source": enum("user_override", "default_rank1", "recommended_candidate")}),
                  "comparison_facts": _nested({"recommended_candidate_id": candidate_id, "eligible_count": count,
                                               "scope": enum("recorded_eligible_candidates_only")})}))
    return result


def _design_result(value):
    source = mapping(value)
    result = project(source, {"status": enum("verified", "error"), "job_status": enum(*KNOWN_STATUSES),
                              "job_id": job_id, "revision": count, "selected_candidate_id": candidate_id, "error": _error,
                              "p13_status": enum("PACKAGE_CONTRACT_VALIDATED", "NOT_EVALUATED"),
                              "viewer_display_verified": boolean, "non_claims": lambda x: codes(x, KNOWN_CLAIMS)})
    if isinstance(source.get("files"), dict):
        result["files"] = result_file_roles(source["files"])
    values = {**_counts("nodes", "beams", "elements", "node_count", "beam_count", "element_count", "source_node_count",
                        "source_element_count", "b31_node_count", "b31_beam_count"),
              **_numbers("minimum_scaled_jacobian", "reference_length_mm")}
    summary = mapping(source.get("summary"))
    if summary:
        result["summary"] = project(summary, dict.fromkeys(("counts", "statistics", "geometry_summary"), _nested(values)))
        for key in ("warnings", "errors"):
            if isinstance(summary.get(key), list):
                result["summary"][key + "_count"] = len(summary[key])
    return result


def tool_facts(name, result):
    """Model output only. Never use this view to validate, execute or consume an action."""
    if name in ("describe_database", "query_records", "count_records"):
        return _database(result)
    if name == "get_job_context":
        return job_facts(result)
    if name == "get_design_result":
        return _design_result(result)
    if name in ("prepare_design_target", "prepare_workflow_action"):
        return project(result, {"status": enum("awaiting_confirmation"), "action": enum(*ACTIONS),
                                "job_id": job_id, "candidate_id": candidate_id, "target": _target})
    return {}


def session_facts(snapshot, *, phase):
    source = mapping(snapshot)
    planning = phase == "planning"
    current = job_id(source.get("active_job_id"))
    job = mapping(source.get("job"))
    result = {"active_job_id": current, "job": job_facts(job, phase=phase) if current and job.get("job_id") == current else None,
              "last_query": query_facts(source["last_query"]) if isinstance(source.get("last_query"), dict) else None,
              "last_database_read": project(source.get("last_database_read"), {"kind": enum(*QUERY_KINDS), "status": enum(*READ_STATES)}),
              "records": None, "pending_csv_target": None}
    rows = mapping(mapping(source.get("records")).get("result"))
    if rows:
        records = _database(rows, planning=planning)
        result["records"] = {"result": records}
        if phase == "conversation":
            # The previous row values and units support numeric follow-up questions.
            # Query provenance and explanations of contract limits stay in their local/result views.
            records.pop("normalized_request", None)
    csv = source.get("pending_csv_target")
    if isinstance(csv, dict):
        fields = {"state": enum("PROPOSED", "CREATING", "CREATE_WRITE_UNCERTAIN"),
                  **_counts("record_index", "record_count", "valid_count", "invalid_count")}
        if (fields["state"](csv.get("state")) and type(csv.get("record_index")) is int
                and type(csv.get("record_count")) is int and 1 <= csv["record_index"] <= csv["record_count"]):
            fields["physical_9d_gpa"] = nine
        selected = project(csv, fields)
        if planning and selected.get("physical_9d_gpa"):
            selected.update(source="csv_upload", unit="GPa")
        result["pending_csv_target"] = selected
    return result
