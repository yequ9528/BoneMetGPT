"""Read-only display helpers; never change records, units, ranking or permissions."""
from __future__ import annotations

import math
import re
import json

from .contracts import BASE_MATERIAL_MODULUS_GPA, STIFFNESS_NAMES


VALIDATION_NOTE = (
    "Candidate predictions, ranking and generated geometry do not establish mechanical, FEA, "
    "manufacturing or clinical validation. NOT_* records an unverified or excluded claim, never a pass."
)

_STATUS_TEXT = {
    "TARGET_PROPOSED": "Target prepared; waiting for your confirmation",
    "TARGET_CONFIRMED": "Target confirmed; waiting for candidate generation results",
    "INVERSE_RUNNING": "Candidate computation is running",
    "CANDIDATES_READY": "Candidates generated",
    "MESH_READY": "Mesh bound; waiting for your confirmation to start",
    "AWAITING_CONFORMAL_AUTHORIZATION": "Mesh bound; waiting for your confirmation to start conformal generation",
    "ADAPTER_RUNNING": "Selected unit cell conversion is running",
    "CONFORMAL_RUNNING": "Conformal geometry generation is running",
    "PACKAGE_VERIFYING": "Result file verification is running",
    "VIEWER_READY": "Geometry results generated; available for read-only viewing",
    "FAILED": "Job stopped with a recorded failure; check the retained record. No automatic retry",
    "HOLD_RUNTIME_INTERRUPTED": "Job run interrupted; completion is unconfirmed. This is not a confirmed computation failure. No automatic retry",
}

_ACTION_LABELS = {
    "confirm_target_and_generate": "Confirm target and generate 3 candidates",
    "request_mesh_upload": "Upload hexahedral mesh (.inp)",
    "start_conformal": "Start conformal generation",
}


def display_number(value, *, signed=False):
    if not _finite_number(value):
        return "Not recorded"
    return format(value, "+.6g" if signed else ".6g")


def _finite_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def matching_query_references(planning, request):
    """Equal field values from the actual planning projection, compared in database units."""
    bounds = {}
    for condition in request.get("filters", []):
        value = condition["value"]
        bounds.setdefault(condition["field"], set()).update(value if isinstance(value, list) else [value])
    if not bounds:
        return []
    matches = {}

    def check(label, values, stiffness_unit):
        for field, endpoints in bounds.items():
            value = values.get(field)
            if not _finite_number(value):
                continue
            unit = stiffness_unit if field in STIFFNESS_NAMES else {
                "relative_density": "ratio", "diameter": "normalized ratio", "sample_id": ""}[field]
            if field in STIFFNESS_NAMES and unit not in {"GPa", "C/Eb"}:
                continue
            normalized = value / BASE_MATERIAL_MODULUS_GPA if unit == "GPa" else value
            if normalized in endpoints:
                key = (label, field, normalized)
                matches.setdefault(key, f"{label}: {field}={value} {unit}".rstrip())

    records = (planning.get("records") or {}).get("result") or {}
    if records.get("status") == "ok":
        for row in records.get("data", {}).get("rows", []):
            if type(row.get("sample_id")) is int:
                check(f"prior sample {row['sample_id']}", row, records["units"]["stiffness"])
    job = planning.get("job") or {}
    target = job.get("target") or {}
    for key, unit in (("physical_9d_gpa", "GPa"), ("normalized_9d", "C/Eb")):
        if (target.get("units") or {}).get(key) == unit:
            check(f"current Job target (revision {job['revision']})", target[key], unit)
    for candidate in job.get("candidates", []):
        if type(candidate.get("rank")) is int:
            label = f"candidate rank {candidate['rank']}"
            units = job.get("candidate_units") or {}
            check(label, {"relative_density": candidate.get("relative_density"),
                          "diameter": candidate.get("normalized_diameter")}, None)
            check(label + " surrogate prediction", candidate.get("predicted_stiffness") or {}, units.get("predicted_stiffness"))
    csv = planning.get("pending_csv_target") or {}
    if csv.get("unit") == "GPa" and type(csv.get("record_index")) is int:
        check(f"selected CSV record {csv['record_index']}", csv["physical_9d_gpa"], "GPa")
    shown = list(matches.values())[:3]
    if len(matches) > len(shown):
        shown.append(f"{len(matches) - len(shown)} more matching references")
    return shown


def stiffness_table(values, *, label="Target stiffness (GPa)"):
    values = values if isinstance(values, dict) else {}
    lines = [f"| Component | {label} |", "| --- | ---: |"]
    lines.extend(f"| {name} | {display_number(values.get(name))} |" for name in STIFFNESS_NAMES)
    return "\n".join(lines)


def job_status_text(status, failure=None):
    if status == "FAILED":
        stage = (failure or {}).get("stage")
        failed_stages = {
            "TARGET_CONFIRMED": "Target validation did not pass; candidate computation was not started",
            "FORMAL_BUNDLE_PREFLIGHT": "Preflight did not pass; candidate computation was not started",
            "INVERSE_RUNNING": "Candidate computation failed",
            "ADAPTER_RUNNING": "Selected unit cell conversion failed",
            "CONFORMAL_RUNNING": "Conformal geometry computation failed",
            "PACKAGE_VERIFYING": "Result file verification failed",
        }
        if stage in failed_stages:
            return failed_stages[stage] + ". Preserve the failure record; no automatic retry"
    return _STATUS_TEXT.get(status, "Job status is missing or needs review; progress cannot be confirmed")


_ERROR_TEXT = {
    "ACTION_ARGUMENT_INVALID": "The assistant action arguments need correction. Keep the current target.",
    "ACTION_STATE_NOT_ALLOWED": "The current Job does not permit this action.",
    "JOB_CONTEXT_STALE": "The current Job changed or could not be read consistently.",
    "JOB_NOT_OPEN": "Open the current Job by its complete ID.",
    "RESULT_NOT_READY": "The final geometry package is not recorded as ready.",
    "RESULT_FILE_MISSING": "A recorded result file is missing.",
    "RESULT_IDENTITY_MISMATCH": "The result files do not match their recorded identities.",
    "RESULT_READ_FAILED": "The saved result package could not be verified or read.",
    "MESH_RECORD_INVALID": "The saved mesh binding could not be validated.",
    "CSV_SIZE": "The target CSV exceeds the 5 MB limit.",
    "CSV_ENCODING": "The target CSV must use UTF-8 encoding.",
    "CSV_EMPTY": "The CSV needs a header and at least one target row.",
    "CSV_INVALID": "Check the UTF-8 CSV header and the nine target stiffness columns in GPa.",
    "CSV_BUSY": "CSV preparation is busy.",
    "CSV_TIMEOUT": "CSV preparation timed out.",
    'INVERSE_PREFLIGHT_FAILED': 'Inverse service preflight failed before model computation. Check the recorded model, source and environment diagnostics; the target is not automatically invalid.',
    'INVERSE_TIMEOUT': 'Candidate computation reached its time limit. Preserve the record; no automatic retry. Review the runtime budget before a new confirmed run.',
    'INVERSE_EXECUTION_FAILED': 'Candidate computation failed in the recorded stage. Inspect its diagnostic record; this alone does not mean the target was invalid.',
    'INVERSE_OUTPUT_INVALID': 'The computed output did not satisfy the result contract. Preserve its diagnostics; no candidate is approved and no automatic retry was performed.',
    "INVALID_QUERY": "The query conditions are invalid. Specify supported fields and units, then submit the corrected query manually.",
    "UNSUPPORTED_QUERY_KIND": "This query type is unavailable. Ask for a database overview, records or a matching count.",
    "QUERY_BUSY": "Another query is running. Wait for it to finish before submitting a query manually.",
    "DATABASE_BUSY": "The database is busy. Wait before submitting a query manually.",
    "QUERY_LIMIT_EXCEEDED": "The query reached its resource limit; no partial result was returned. Narrow the request before querying manually.",
    "BINDING_MISMATCH": "The recorded source identity does not match the binding. Querying stopped; check the release configuration.",
    "DATABASE_UNAVAILABLE": "The bound database or manifest could not be read. Check the recorded source files.",
    "QUERY_EXECUTION_FAILED": "The query did not complete. Preserve its diagnostic record for inspection.",
    "JOB_NOT_FOUND": "No saved Job was found for this ID. Check the complete Job ID before reopening it.",
    "JOB_CONTRACT_INVALID": "The saved Job record could not be read consistently. Preserve the record for inspection.",
    "PACKAGE_CONTRACT_INVALID": "The saved result package did not pass its contract check. Preserve the package and logs; do not regenerate it.",
    "FILE_MISSING": "A required result file is missing. Preserve the package and logs; do not regenerate it.",
    "FILE_IDENTITY_MISMATCH": "A result file does not match its recorded identity. Preserve the package and logs; do not regenerate it.",
    "SCALER_RANGE": "The effective target is outside the recorded supported interval. P-09 passed; candidate computation was not started.",
    "P09": "Target P-09 validation did not pass. The supported-interval check passed; candidate computation was not started.",
    "BOTH": "The target failed both its supported-interval and P-09 checks. Candidate computation was not started.",
    "INVALID_CONVERSION": "Target numeric conversion or validation could not be evaluated safely. Candidate computation was not started.",
}


def recorded_error_code(error):
    code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
    return code if isinstance(code, str) and code in _ERROR_TEXT else "UNCLASSIFIED"


def recorded_stage(value):
    return value if isinstance(value, str) and value in (*_STATUS_TEXT, "FORMAL_BUNDLE_PREFLIGHT") else "Unknown"


def error_summary(error, *, fallback="The operation could not be completed."):
    """Translate known diagnostic codes for display, without changing errors."""
    return _ERROR_TEXT.get(recorded_error_code(error), fallback)


_TURN_STATUS_TEXT = {
    "stale": "The Job changed or its current state could not be checked. The outdated answer was discarded. Read the latest saved Job; no computation or model retry was started.",
    "busy": "This chat request is being processed. Wait for the turn to finish.",
    "budget_exhausted": "The assistant's explanation stopped at the session budget. No automatic retry; check the Job record for computation status.",
    "timeout": "The assistant's response wait timed out. Late responses from this turn will not be accepted. No automatic retry; check the Job record for computation status.",
    "limit_reached": "The assistant's explanation stopped at this turn's tool limit. No automatic retry; check the Job record for computation status.",
    "incomplete": "The required query evidence is missing for this turn. No additional read or automatic retry was performed.",
    "waiting_for_user": "A confirmation card is ready. Review it and use its existing confirmation button.",
    "empty": "No matching records. Conditions were not relaxed and no automatic retry was performed.",
    "error": "The assistant could not complete this turn or read the required data. Check the saved Job. No automatic retry; computation status is recorded separately in the Job.",
}


def _comparison_candidates(job, *, ranks=None, candidate_ids=None):
    """Resolve read-only comparison objects without substituting another scope."""
    candidates = job.get('candidates') or []
    if ranks is not None and candidate_ids is not None:
        raise ValueError('Use ranks or candidate_ids, not both.')
    selector, key = (ranks, 'rank') if ranks is not None else (candidate_ids, 'candidate_id')
    if selector is not None:
        valid = (lambda value: type(value) is int and value > 0) if key == 'rank' else (
            lambda value: isinstance(value, str) and bool(value))
        if (not isinstance(selector, list) or not 1 <= len(selector) <= 3
                or not all(valid(value) for value in selector) or len(set(selector)) != len(selector)):
            raise ValueError('Specify one to three unique candidate ranks or IDs.')
        chosen = []
        for value in selector:
            matches = [candidate for candidate in candidates if candidate.get(key) == value]
            if len(matches) != 1:
                raise ValueError('A comparison object does not uniquely identify a candidate in this Job.')
            chosen.append(matches[0])
        candidates = chosen
    return sorted(candidates, key=lambda candidate: candidate.get('rank') or math.inf)


def _candidate_name(candidate):
    rank = candidate.get('rank')
    label = f'Rank {rank}' if type(rank) is int and rank > 0 else 'Unranked candidate'
    return f"{label} (`{candidate['candidate_id']}`)"


def _recorded_metric(candidate, key):
    value = candidate.get(key)
    return value if _finite_number(value) and value >= 0 else None


def _confirmed_candidate(job):
    if job.get('candidate_confirmation') != 'CONFIRMED':
        return None
    selected_id = (job.get('selection') or {}).get('selected_candidate_id')
    matches = [candidate for candidate in job.get('candidates') or [] if candidate.get('candidate_id') == selected_id]
    if len(matches) != 1:
        raise ValueError('The confirmed candidate cannot be resolved in the current Job.')
    return matches[0]


def _confirmation_fact(job):
    selected = _confirmed_candidate(job)
    if selected is not None:
        return f'Confirmed selection: {_candidate_name(selected)}. Comparing candidates does not change this selection.'
    if job.get('candidate_confirmation') == 'UNCONFIRMED':
        return 'No candidate has been confirmed. A recommendation or a pending confirmation card is not a confirmed selection.'
    return 'Candidate confirmation: Not recorded.'


def _tradeoff_fact(candidate, baseline, *, nmse_ranking=False):
    pieces = []
    for key, label in (('relative_density', 'relative density'), ('score', 'P-10 score'), ('nmse', 'NMSE')):
        if nmse_ranking and key == 'score': continue
        value, reference = _recorded_metric(candidate, key), _recorded_metric(baseline, key)
        if value is None or reference is None:
            pieces.append(label + ' comparison: Not recorded')
            continue
        delta = value - reference
        direction = 'lower' if delta < 0 else 'higher' if delta > 0 else 'equal'
        meaning = '' if key == 'relative_density' or not delta else (
            '; better target match by this metric' if delta < 0 else '; worse target match by this metric')
        pieces.append(f'{label} is {direction} ({display_number(value)} versus {display_number(reference)}; '
                      f'Δ {display_number(delta, signed=True)}{meaning})')
    return f'Compared with {_candidate_name(baseline)}, {_candidate_name(candidate)}: ' + '; '.join(pieces) + '.'


def _metric_definitions(*, nmse_ranking=False):
    ranking = ('NMSE determines the recorded ranking; the first eligible candidate is recommended, and equal raw NMSE uses source output order. '
               if nmse_ranking else 'P-10 uses the recorded weighted component-normalized RMSE; lower is better and its score is not a percentage. NMSE does not determine the recommendation. ')
    return (ranking + 'NMSE = sum((prediction - target)^2) / sum(target^2), over the nine stiffness components. '
            'NMSE (%) = 100 × NMSE; relative L2 error (%) = 100 × sqrt(NMSE). '
            'NMSE ratio 0.0025 means NMSE 0.25% and relative L2 error 5%, not accuracy 99.75%. '
            'These measure surrogate prediction-to-target match, not error against measured or FEA-validated performance.')


def _recorded_recommendation(job):
    facts = job.get('comparison_facts') or {}
    identity = facts.get('recommended_candidate_id') or facts.get('p10_recommended_candidate_id')
    return next((c for c in job.get('candidates') or []
                 if c.get('eligible') is True and c.get('candidate_id') == identity), None)


_STIFFNESS_DIFFERENCE = (
    'A stiffness difference is prediction minus target in GPa. A negative difference means '
    'the prediction is below the target and does not mean negative stiffness.'
)


def _read_result_candidate_facts(job, selected, *, nmse_ranking):
    if not job.get('candidates'):
        return []
    recommended = _recorded_recommendation(job)
    label = 'NMSE' if nmse_ranking else 'P-10'
    if recommended is None:
        comparison = f'Recorded {label} recommendation: Not recorded. Comparison is unavailable.'
    elif selected is None:
        comparison = f'Recorded {label} recommendation: {_candidate_name(recommended)}.'
    elif selected['candidate_id'] == recommended['candidate_id']:
        comparison = f'The confirmed candidate is the recorded {label} recommendation.'
    else:
        comparison = f'Recorded {label} recommendation: {_candidate_name(recommended)}. ' + _tradeoff_fact(
            selected, recommended, nmse_ranking=nmse_ranking)
    return [comparison, _STIFFNESS_DIFFERENCE + ' Matching error is based on surrogate predictions.']


def exclusion_labels(candidate):
    known = {"CAD_READY_REQUIRED", "B14_PASS_REQUIRED", "TARGET_RANGE_PASS_REQUIRED", "COMPLETE_FINITE_9D_REQUIRED"}
    return [reason if isinstance(reason, str) and reason in known else "Eligibility check failed"
            for reason in candidate.get("exclusion_reasons") or []]


def result_file_roles(files):
    from .artifacts import PRODUCT_FILE_MAP
    return [key for key in files if key in PRODUCT_FILE_MAP]


def _result_summary_lines(summary):
    lines = []
    measurements = ("nodes", "beams", "elements", "node_count", "beam_count", "element_count",
                    "source_node_count", "source_element_count", "b31_node_count", "b31_beam_count",
                    "minimum_scaled_jacobian", "reference_length_mm")
    for key, label in (("counts", "Geometry counts"), ("statistics", "Recorded statistics"),
                       ("geometry_summary", "Geometry summary")):
        record = summary.get(key)
        if isinstance(record, dict):
            values = [f"{name}={record[name]}" for name in measurements if _finite_number(record.get(name))]
            if values:
                lines.append(label + ": " + ", ".join(values) + ".")
    if not lines:
        lines.append("Geometry counts and summary: Not recorded in the result summary.")
    for key in ("warnings", "errors"):
        if isinstance(summary.get(key), list):
            lines.append(f"Recorded {key}: {len(summary[key])}.")
    return lines


def build_job_fact_reply(job, *, focus='overall', ranks=None, candidate_ids=None, component=None,
                         design_result=None, receipt=None, display_only=False):
    """Build facts from the current tool payload; callers must verify its run evidence.

    This function never reads a file, ranks a design, writes a Job or selects an
    action. A program-origin marker alone is not evidence of a current read.
    """
    allowed = {'overall', 'density', 'matching', 'selection', 'metrics', 'component_delta', 'status', 'read_result', 'result'}
    if focus not in allowed:
        raise ValueError('Unsupported comparison focus.')
    if component is not None and (focus != 'component_delta' or component not in STIFFNESS_NAMES):
        raise ValueError('A component must be one of the nine stiffness names and use component_delta focus.')
    if focus == 'selection' and (ranks is not None or candidate_ids is not None):
        raise ValueError('Selection status comes from the confirmation record, not a comparison selector.')
    candidates = _comparison_candidates(job, ranks=ranks, candidate_ids=candidate_ids)
    selected = _confirmed_candidate(job)
    nmse_ranking = (job.get('ranking') or {}).get('metric') == 'nmse'
    label = 'NMSE' if nmse_ranking else 'P-10'
    if receipt is not None:
        if (receipt.get('action') == 'select_candidate' and not receipt.get('failed')
                and selected is not None):
            return f"Selected Rank {selected['rank']} · `{selected['candidate_id']}`."
        focus = 'status'
    if focus in {'status', 'read_result'}:
        lines = [job_status_text(job.get('status'), job.get('failure')) + '.', _confirmation_fact(job)]
        if focus == 'read_result':
            lines.extend(_read_result_candidate_facts(job, selected, nmse_ranking=nmse_ranking))
        if receipt and receipt.get('failed'):
            lines.append('The recorded action did not complete successfully. No action was retried.')
        actions = job.get('allowed_actions') or []
        next_action = next((action for action in ('request_mesh_upload', 'confirm_target_and_generate', 'start_conformal')
                            if action in actions), None)
        if next_action:
            lines.append(f'Next: use "{_ACTION_LABELS[next_action]}" on the current Job card.')
        elif 'select_candidate' in actions:
            lines.append('Next: review the candidates and confirm your selection using the existing interface.')
        elif job.get('status') == 'TARGET_PROPOSED':
            lines.append('Chat has no generation action available for this saved target. Use "Refresh Job status" to review the current Job card.')
        else:
            lines.append('No next-stage action is available in this session. Use "Refresh Job status" to check the saved record.')
        return ' '.join(lines)
    if focus == 'result':
        if (not isinstance(design_result, dict) or design_result.get('status') != 'verified'
                or not job.get('job_id') or job.get('revision') is None
                or design_result.get('job_id') != job['job_id'] or design_result.get('revision') != job['revision']
                or selected is None or design_result.get('selected_candidate_id') != selected['candidate_id']):
            raise ValueError('A matching verified result read and confirmed candidate are required.')
        lines = [f"The existing saved result package was verified for Job `{job['job_id']}`, revision {job['revision']}.",
                 _confirmation_fact(job)]
        files = design_result.get('files')
        if not isinstance(files, dict):
            raise ValueError('The verified result must contain its recorded file map.')
        roles = result_file_roles(files) if display_only else files
        lines.append(f"Recorded files ({len(roles)}): " + (', '.join(f'`{key}`' for key in roles) or 'None recorded') + '.')
        summary = design_result.get('summary') or {}
        if display_only:
            lines.extend(_result_summary_lines(summary))
        else:
            # Preserve the canonical agent history and evidence record.
            for key, label in (('counts', 'Geometry counts'), ('statistics', 'Recorded statistics'),
                               ('geometry_summary', 'Geometry summary'), ('warnings', 'Recorded warnings'), ('errors', 'Recorded errors')):
                if key in summary:
                    lines.append(label + ': ' + json.dumps(summary[key], ensure_ascii=False, separators=(', ', ': ')) + '.')
            if not any(key in summary for key in ('counts', 'statistics', 'geometry_summary')):
                lines.append('Geometry counts and summary: Not recorded in the result summary.')
        lines.extend(_read_result_candidate_facts(job, selected, nmse_ranking=nmse_ranking))
        lines.extend(['Viewer display has not been verified by this file read.', VALIDATION_NOTE])
        return '\n\n'.join(lines)
    if focus == 'metrics':
        return _metric_definitions(nmse_ranking=nmse_ranking) + '\n\n' + build_job_fact_reply(
            job, focus='overall', ranks=ranks, candidate_ids=candidate_ids)
    component_lines = []
    if focus == 'component_delta':
        target = (job.get('target') or {}).get('physical_9d_gpa') or {}
        names = [component] if component else STIFFNESS_NAMES
        component_lines.append(_STIFFNESS_DIFFERENCE + ' These are surrogate predictions, not FEA or measured performance.')
        for candidate in candidates:
            predictions = candidate.get('predicted_stiffness') or {}
            values = []
            for name in names:
                prediction, reference = predictions.get(name), target.get(name)
                delta = prediction - reference if _finite_number(prediction) and _finite_number(reference) else None
                values.append(f'{name}: prediction {display_number(prediction)}, target {display_number(reference)}, '
                              f'Δ {display_number(delta, signed=True)} GPa')
            component_lines.append(_candidate_name(candidate) + ': ' + '; '.join(values) + '.')
        if not candidates:
            component_lines.append('No candidates are recorded for the current Job.')
    eligible = [candidate for candidate in candidates if candidate.get('eligible') is True]
    recommended = _recorded_recommendation(job)
    rule = ('Raw NMSE ascending; ties retain source output order. Only eligible candidates can be recommended.' if nmse_ranking else
            'This recommendation follows the recorded P-10 ranking; NMSE does not determine the recommendation.')
    lines = [(f'Global {label} recommendation: ' + _candidate_name(recommended) + '. ' + rule)
             if recommended else f'Global {label} recommendation: Not recorded. No recommendation has been inferred from the row order.']
    explicit = ranks is not None or candidate_ids is not None
    scope = 'the specified eligible candidates' if explicit else 'all eligible candidates'
    if not eligible:
        lines.append('No eligible candidate is available in this comparison scope; no candidate is recommended from it.')
    else:
        for key, metric_label in (('relative_density', 'relative density'), ('nmse', 'NMSE')):
            recorded = [candidate for candidate in eligible if _recorded_metric(candidate, key) is not None]
            if not recorded:
                lines.append(f'Lowest recorded {metric_label} among {scope}: Not recorded.')
                continue
            minimum = min(_recorded_metric(candidate, key) for candidate in recorded)
            tied = [candidate for candidate in recorded if _recorded_metric(candidate, key) == minimum]
            names = ', '.join(_candidate_name(candidate) for candidate in tied)
            lines.append(f'Lowest recorded {metric_label} among {scope}: {names}'
                         + (' (tied)' if len(tied) > 1 else '') + f', {display_number(minimum)}.'
                         + (f' Only {len(recorded)} of {len(eligible)} values are recorded; the others are Not recorded.'
                            if len(recorded) != len(eligible) else ''))
        baseline = eligible[0] if explicit else recommended
        if baseline is not None:
            if explicit:
                lines.append('Comparison baseline within the specified candidates: ' + _candidate_name(baseline)
                             + '. This baseline does not change the global recommendation.')
            comparisons = [candidate for candidate in eligible if candidate['candidate_id'] != baseline['candidate_id']]
            if len(eligible) == 1 and recommended is not None and baseline['candidate_id'] != recommended['candidate_id']:
                lines.append(f'For reference against the global {label} recommendation: ' + _tradeoff_fact(baseline, recommended, nmse_ranking=nmse_ranking))
            else:
                lines.extend(_tradeoff_fact(candidate, baseline, nmse_ranking=nmse_ranking) for candidate in comparisons)
    for candidate in candidates:
        if candidate.get('eligible') is not True:
            reasons = ', '.join(exclusion_labels(candidate)) or 'Reason not recorded'
            lines.append(f'{_candidate_name(candidate)} is not eligible for selection: {reasons}.')
    lines.append(_confirmation_fact(job))
    if candidates and not component_lines:
        lines.append(_STIFFNESS_DIFFERENCE)
    lines.append('These are inverse-generated structures; the database or CSV supplies the reference target. '
                 'Lower relative density means a lower material fraction, not proven strength or fatigue safety. '
                 'Stiffness and matching errors are surrogate predictions, not FEA or measured validation.')
    return '\n\n'.join(component_lines + lines)


def _candidate_comparison(job, *, ranks=None, candidate_ids=None):
    candidates = _comparison_candidates(job, ranks=ranks, candidate_ids=candidate_ids)
    if not candidates:
        return "No candidates are recorded for the current Job."
    target = (job.get("target") or {}).get("physical_9d_gpa") or {}
    labels = [f"Rank {c['rank']}" if c.get("rank") is not None else f"Unranked {i}"
              for i, c in enumerate(candidates, 1)]
    nmse_ranking = (job.get("ranking") or {}).get("metric") == "nmse"
    metric = "nmse" if nmse_ranking else "score"
    label = "NMSE" if nmse_ranking else "P-10 score"
    lines = ["### Candidate comparison", "", f"| Rank | Candidate | {label} |", "| --- | --- | ---: |"]
    for label, candidate in zip(labels, candidates):
        rank = candidate.get("rank")
        lines.append(f"| {rank if rank is not None else label} | `{candidate['candidate_id']}` | {display_number(candidate.get(metric))} |")
    lines.extend(['', '| Candidate | Relative density | NMSE (ratio) | NMSE (%) | Relative L2 error (%) | Eligibility |',
                  '| --- | ---: | ---: | ---: | ---: | --- |'])
    for candidate in candidates:
        reasons = ', '.join(exclusion_labels(candidate))
        eligibility = 'Eligible' if candidate.get('eligible') else reasons or 'Not eligible'
        recorded_nmse = candidate.get('nmse')
        nmse = recorded_nmse if _finite_number(recorded_nmse) and recorded_nmse >= 0 else None
        nmse_percent = 100 * nmse if nmse is not None else None
        relative_l2_percent = 100 * math.sqrt(nmse) if nmse is not None else None
        lines.append(f"| `{candidate['candidate_id']}` | {display_number(candidate.get('relative_density'))} | {display_number(nmse)} | {display_number(nmse_percent)} | {display_number(relative_l2_percent)} | {eligibility} |")
    lines.extend(['', 'These are inverse-generated candidates; the database/CSV supplies the reference target.',
                  '', '**Metric definitions**', '', _metric_definitions(nmse_ranking=nmse_ranking)])
    for diagnostic in (job.get('inverse_summary') or {}).get('diagnostics') or []:
        if diagnostic.get('diversity_fallback'):
            lines.extend(['', 'Diversity selection fell back to the best three matches; their geometries may be similar.'])
    lines.extend(["", "**Predicted stiffness (GPa)** with Δ in parentheses (prediction − target). A negative delta means below target, not negative stiffness.", "",
                  "| Component | " + " | ".join(labels) + " |",
                  "| --- |" + " ---: |" * len(labels)])
    for name in STIFFNESS_NAMES:
        cells = []
        for candidate in candidates:
            prediction = (candidate.get("predicted_stiffness") or {}).get(name)
            reference = target.get(name)
            delta = prediction - reference if _finite_number(prediction) and _finite_number(reference) else None
            cells.append(f"{display_number(prediction)} ({display_number(delta, signed=True)})")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


# Free-form model prose has no field schema. Keep protocol references, diagnostic
# dumps and recognizable machine paths out of this last, user-facing text sink.
_INTERNAL_PROSE = re.compile(
    r"(?i)(?:\b[A-Z]:[\\/]|\\\\[^\\\s]+\\|(?<!\w)/(?:home|users|tmp|var|srv|mnt|opt|root)/"
    r"|\b(?:confirmation_ref|proposal_ref|result_ref|job_ref|formal_bundle_binding|execution_context|stored_path)\b"
    r"|\b[0-9a-f]{48}\b|\b(?:stdout|stderr|cwd|api[_-]?key|password|authorization|secret)[\"']?\s*[:=]"
    r"|\bBearer\s+[A-Za-z0-9._-]+|\bsk-[A-Za-z0-9_-]{8,}|Traceback \(most recent call last\)"
    r"|\b(?:python(?:\.exe)?\s+-[mc]|powershell(?:\.exe)?\s+-|cmd(?:\.exe)?\s+/c)\b)"
)


def _model_display_text(text):
    if not isinstance(text, str) or _INTERNAL_PROSE.search(text):
        return "The reply contains unavailable technical detail. Ask about the target, candidates or current Job status."
    return text


DATABASE_READ_USED = "This turn used the bound read-only reference database."


def recorded_chat_reply(result, snapshot):
    """Display the accepted canonical body; unmarked Job prose is not trusted."""
    status = result.get("status")
    failure = result.get('failure')
    if status == "stale" and result.get("display_read_stale"):
        return "The session or its bound inputs changed or could not be verified. The outdated database reply was discarded. No automatic retry."
    if status == 'error' and isinstance(failure, dict) and failure.get('stage') in ('action_prepare', 'result_read'):
        next_step = ('Read the current Job and use its available action. No operation was executed or retried.'
                     if failure['stage'] == 'action_prepare' else
                     'Inspect the recorded Job and result files. The result package has not been verified in this turn.')
        return (f"{recorded_error_code(failure)}: {error_summary(failure)} "
                f"Recorded Job status: {recorded_stage(result.get('job_status'))}. {next_step}")
    if status in _TURN_STATUS_TEXT:
        return _TURN_STATUS_TEXT[status]
    if status != "answered":
        if status == "needs_clarification":
            if result.get("clarification_origin") == "model" and result.get("text"):
                return _model_display_text(result["text"])
            for field in STIFFNESS_NAMES:
                if result.get("text") == f"请明确{field}使用GPa还是C/Eb。":
                    return f"For {field}, should the threshold use GPa or dimensionless C/Eb?"
            return ("Please clarify the query field, threshold unit, candidate or intended next step. "
                    "Use GPa for target stiffness and dimensionless C/Eb for database stiffness. "
                    "No new action was authorized.")
        return "The turn outcome could not be verified. Reopen the current Job before proceeding."
    if result.get('answer_origin') == 'program_job_facts':
        # The caller has already checked current-run Job evidence. The marker
        # only selects display behavior; it is never a verification credential.
        text = result.get('text', '')
        request = result.get('fact_request') or {}
        if result.get("design_result") is not None:
            return build_job_fact_reply(snapshot.get("job") or {}, focus="result",
                                        design_result=result["design_result"], display_only=True)
        if (result.get('workflow_intent') == 'compare_candidates'
                and request.get('focus', 'overall') in {'overall', 'density', 'matching'}):
            return text + '\n\n' + _candidate_comparison(snapshot.get('job') or {},
                ranks=request.get('ranks'), candidate_ids=request.get('candidate_ids'))
        return text
    basis = result.get("answer_basis")
    if basis == "database":
        if result.get("query_state", {}).get("status") != "ok":
            return "Query status needs review. Check the program result card before drawing conclusions."
        if result.get("explanation_requested") is True:
            return _model_display_text(result.get("text", ""))
        return ("Query completed. Use the values and conditions in the result card above. "
                "Database C/Eb and normalized strut diameter are dimensionless; target and candidate predictions use GPa.")
    if basis not in {"workflow", "receipt"}:
        return _model_display_text(result.get("text", ""))
    job = snapshot.get("job") or {}
    if not job:
        return 'No Job record is open. Use "Start design" or "Reopen Job" to continue.'
    if result.get("workflow_intent") == "compare_candidates":
        # Compatibility display only; the Agent must attach current evidence
        # before accepting a factual reply into history or publishing it.
        return build_job_fact_reply(job) + '\n\n' + _candidate_comparison(job)
    allowed = job.get("allowed_actions", [])
    text = ("Candidates are recorded; no next-stage action is available in this session"
            if job.get("status") == "CANDIDATES_READY" and not allowed
            else job_status_text(job.get("status"), job.get("failure"))) + "."
    # Upload is already permitted only after the existing selection gate. This
    # changes the instruction's priority, never the actions or permissions.
    next_labels = ([_ACTION_LABELS["request_mesh_upload"]] if "request_mesh_upload" in allowed else
                   [_ACTION_LABELS[a] for a in allowed if a in _ACTION_LABELS])
    if not next_labels and "select_candidate" in allowed:
        eligible = sorted((c for c in job.get("candidates", []) if c.get("eligible")), key=lambda c: c.get("rank") or 99)
        if eligible:
            rank = eligible[0].get("rank")
            next_labels = ["Confirm recommended candidate" if rank == 1 else f"Select rank {rank}"]
    if next_labels:
        text += '\n\nNext: use "' + next_labels[0] + '" on the current Job card.'
    else:
        text += '\n\nUse "Refresh Job status" to check the saved record and currently available actions.'
    return text + "\n\n" + VALIDATION_NOTE
