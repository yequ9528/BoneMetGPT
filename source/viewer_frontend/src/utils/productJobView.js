const PRODUCT_VIEW_STATES = new Set([
  'candidate',
  'mesh',
  'running',
  'result',
  'failed'
])

const RUNNING_JOB_STATUSES = new Set([
  'TARGET_PROPOSED',
  'TARGET_CONFIRMED',
  'INVERSE_RUNNING',
  'ADAPTER_RUNNING',
  'CONFORMAL_RUNNING',
  'PACKAGE_VERIFYING'
])

const PRODUCT_JOB_ID_RE = /^job-[0-9A-F]{24}$/

export const PRODUCT_JOB_POLL_MS = 2000

export function isSafeProductJobId(jobId) {
  return typeof jobId === 'string' && PRODUCT_JOB_ID_RE.test(jobId)
}

export function formatProductJobIdentity(payload) {
  const jobId = payload?.job_id || 'unknown-job'
  const revision = Number.isInteger(payload?.revision) ? payload.revision : 'unknown'
  const disposition = payload?.workflow_disposition || 'UNKNOWN'
  const updatedAt = payload?.updated_at || 'unknown'
  return `${jobId} · revision ${revision} · ${disposition} · updated ${updatedAt}`
}

export function resolveProductViewState(payload) {
  if (PRODUCT_VIEW_STATES.has(payload?.view_state)) {
    return payload.view_state
  }
  if (payload?.progress?.failure || payload?.status === 'FAILED') {
    return 'failed'
  }
  if (payload?.status === 'VIEWER_READY' && payload?.result) {
    return 'result'
  }
  if (
    Number(payload?.progress?.attempt_count || 0) > 0
    || RUNNING_JOB_STATUSES.has(payload?.status)
  ) {
    return 'running'
  }
  if (payload?.mesh?.status === 'available') {
    return 'mesh'
  }
  return 'candidate'
}

export function shouldPollProductJob(payload) {
  const state = resolveProductViewState(payload)
  return state !== 'result' && state !== 'failed'
}

// Product-page projections do not change the embedded Viewer's legacy resolver.
export function getProductPageState(payload) {
  if (!PRODUCT_VIEW_STATES.has(payload?.view_state)) return 'unknown'
  if (payload.view_state === 'result' && (payload.progress?.job_status !== 'VIEWER_READY' || !payload.result?.package_id)) return 'unknown'
  return payload.view_state
}

export function formatProductNumber(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Not recorded'
  return Number(value.toPrecision(6)).toString()
}

export function productTargetComponents(target) {
  return ['C11', 'C12', 'C13', 'C22', 'C23', 'C33', 'C44', 'C55', 'C66'].map(name => ({
    name, raw: target?.physical_9d_gpa?.[name], display: formatProductNumber(target?.physical_9d_gpa?.[name])
  }))
}

export function candidateMetric(candidate, ranking, { recordedOnly = false } = {}) {
  const useNmse = ranking?.metric === 'nmse' || (!ranking?.metric && Number.isFinite(candidate?.nmse))
  const value = useNmse ? candidate?.nmse : candidate?.score
  if (recordedOnly && !Number.isFinite(value)) return null
  const label = useNmse ? 'NMSE (dimensionless, lower is better)' : 'P-10 score (lower is better)'
  return `${label}: ${formatProductNumber(value)}`
}

const eligibilityReasons = {
  CAD_READY_REQUIRED: 'CAD-ready geometry required',
  B14_PASS_REQUIRED: 'Candidate geometry check required',
  TARGET_RANGE_PASS_REQUIRED: 'Target range check required',
  COMPLETE_FINITE_9D_REQUIRED: 'Nine finite stiffness components required'
}
export function candidateEligibility(candidate) {
  if (candidate?.eligible === true) return 'Eligible'
  if (candidate?.eligible !== false) return 'Not recorded'
  const reasons = (candidate.exclusion_reasons || []).filter(key=>Object.hasOwn(eligibilityReasons,key)).map(key=>eligibilityReasons[key])
  return reasons.length ? `Ineligible · ${reasons.join(', ')}` : 'Ineligible'
}

const warningLabels = {
  'default diameter used':'The model uses the default diameter.',
  'interface node set was not provided and was not inferred':'No interface node set was supplied or inferred.',
  'per-beam source-element lineage is not produced by the current core':'Individual beams are not mapped back to their source elements.',
}
export function resultWarnings(warnings) {
  const messages = new Set()
  for (const warning of warnings || []) {
    if (Object.hasOwn(warningLabels,warning)) messages.add(warningLabels[warning])
    else if (/^Additional recorded warnings: \d+\. Review the result with the project owner\.$/.test(warning)) messages.add(warning)
  }
  return [...messages]
}

// Display existing recorded states; this does not decide capabilities or polling.
export function productTaskPresentation(payload) {
  const refresh = 'Use "Refresh Job status" on the chat page to read the latest record. '
  switch (payload?.progress?.job_status) {
    case 'TARGET_PROPOSED': return { kind: 'waiting', title: 'Target prepared; confirmation pending', next: 'Review the target on the chat page and use "Confirm target and generate 3 candidates".' }
    case 'TARGET_CONFIRMED': return { kind: 'waiting', title: 'Target confirmed; candidate-generation record pending', next: refresh }
    case 'CANDIDATES_READY': if (payload?.workflow_disposition === 'HOLD') return { kind: 'hold', title: 'Candidate step on hold', next: `Review the recorded eligibility evidence on the chat page. ${refresh}` }; return payload?.selection?.confirmation_status === 'CONFIRMED'
      ? { kind: 'waiting', title: 'Candidate confirmed; mesh upload pending', next: 'Use the existing mesh upload button on the chat page, review the input contract and upload the INP.' }
      : { kind: 'waiting', title: 'Candidates generated; confirmation pending', next: 'Use the existing candidate selection buttons on the chat page to explicitly confirm your choice.' }
    case 'MESH_READY': return { kind: 'waiting', title: 'Mesh record prepared; launch-status update pending', next: refresh }
    case 'AWAITING_CONFORMAL_AUTHORIZATION': return { kind: 'waiting', title: 'Mesh bound; explicit start pending', next: 'Review the mesh on the chat page and use "Start conformal generation".' }
    case 'INVERSE_RUNNING': return { kind: 'running', title: 'Generating candidates', next: refresh }
    case 'ADAPTER_RUNNING': return { kind: 'running', title: 'Preparing geometry input from the selected unit cell', next: refresh }
    case 'CONFORMAL_RUNNING': return { kind: 'running', title: 'Generating conformal geometry', next: refresh }
    case 'PACKAGE_VERIFYING': return { kind: 'running', title: 'Checking generated files', next: refresh }
    case 'FAILED': {
      const titles = {
        TARGET_CONFIRMED: 'Target validation did not pass; inverse design not started',
        MESH_PRECHECK: 'Input mesh precheck failed',
        FORMAL_BUNDLE_PREFLIGHT: 'Preflight did not pass',
        INVERSE_RUNNING: 'Candidate generation failed',
        ADAPTER_RUNNING: 'Geometry-input preparation failed',
        CONFORMAL_RUNNING: 'Geometry generation failed',
        PACKAGE_VERIFYING: 'Generated-file verification failed'
      }
      return { kind: 'failed', title: titles[payload?.progress?.failure?.stage] || 'Job failed; inspect the recorded failure', next: `${refresh}Preserve the failure record. No automatic retry.` }
    }
    case 'HOLD_RUNTIME_INTERRUPTED': return { kind: 'interrupted', title: 'Run interrupted; no usable generated result', next: `${refresh}Reopening the Job does not resume computation. No automatic retry.` }
    case 'VIEWER_READY': if (getProductPageState(payload) === 'result') return { kind: 'result', title: 'Result generated', next: 'View the generated shape, selected unit cell and recorded files. Generation does not establish engineering validation.' }; break
  }
  return { kind: 'unknown', title: 'Task status needs review', next: `${refresh}Computation completion cannot be determined from this record.` }
}

const TARGET_CONFIRMED_STATUSES = new Set([
  'TARGET_CONFIRMED', 'INVERSE_RUNNING', 'CANDIDATES_READY', 'MESH_READY',
  'AWAITING_CONFORMAL_AUTHORIZATION', 'ADAPTER_RUNNING', 'CONFORMAL_RUNNING',
  'PACKAGE_VERIFYING', 'VIEWER_READY'
])

export function productProgressSummary(payload) {
  const status = payload?.progress?.job_status
  const task = productTaskPresentation(payload)
  return [
    payload?.target?.target_sha256 ? TARGET_CONFIRMED_STATUSES.has(status) ? 'Target confirmed'
      : status === 'TARGET_PROPOSED' ? 'Target confirmation pending' : 'Target confirmation needs review' : 'Target confirmation not recorded',
    task.kind === 'hold' ? 'Candidate selection on hold' : payload?.selection?.confirmation_status === 'CONFIRMED' ? 'Candidate explicitly confirmed' :
      payload?.selection?.confirmation_status === 'LEGACY_NOT_RECORDED' ? 'Historical selection (explicit confirmation not recorded)' :
        payload?.selection?.selected_candidate_id ? 'System recommendation (confirmation pending)' : 'Candidate selection pending',
    payload?.mesh?.mesh_binding_sha256 ? 'Mesh bound' : 'Mesh binding pending',
    task.title
  ].join(' → ')
}

export function productContractText(result) {
  switch (result?.compatibility?.package_format) {
    case 'PRODUCT_SIX_FILES_V1': return 'Saved result read under the product result-package contract.'
    case 'LEGACY_SIX_FILES_V1': return 'Viewing a registered historical result; the original production record is unchanged.'
    default: return !result?.compatibility && result?.p13_evidence?.status === 'verified'
      && result.p13_evidence.integrity_status === 'PASS' && result.p13_evidence.package_contract
      ? 'Read under the independent P13 result contract.' : 'Result-reading contract needs review'
  }
}

export function productReadFailure(failure, previousPayload) {
  const code = failure?.code || 'INVALID_VIEWER_RESPONSE'
  const retainSnapshot = Boolean(previousPayload) && ['VIEWER_UNREACHABLE', 'INVALID_VIEWER_RESPONSE'].includes(code)
  const summaries = {
    JOB_NOT_FOUND: 'The specified Job was not found. Check the full Job ID.',
    VIEWER_UNREACHABLE: 'Cannot connect to Viewer. Check the service status, then refresh this page to read again.',
    FILE_MISSING: 'A required result file is missing. Preserve the record and review the read diagnostics.',
    FILE_IDENTITY_MISMATCH: 'A result file does not match its recorded identity. Preserve the record and review the read diagnostics.',
    JOB_CONTRACT_INVALID: 'The Job or event record did not pass the consistency check. Preserve the record and review the read diagnostics.',
    PACKAGE_CONTRACT_INVALID: 'The result package or its linked identity did not pass the consistency check. Preserve the record and review the read diagnostics.'
  }
  const message = retainSnapshot ? 'The latest read failed. The last successfully read snapshot is shown below; its status has not been updated.'
    : summaries[code] || 'Reading did not complete. Preserve the information on this page and review the logs.'
  return { code, message, reason: failure?.message || '', retainSnapshot, downloadsEnabled: false }
}

export function createProductJobReader(fetchJob) {
  const pending = new Map()
  return jobId => {
    if (!pending.has(jobId)) {
      const request = fetchJob(jobId).finally(() => { if (pending.get(jobId) === request) pending.delete(jobId) })
      pending.set(jobId, request)
    }
    return pending.get(jobId)
  }
}

export function productReadyTime(payload) {
  if (payload?.progress?.job_status !== 'VIEWER_READY' || payload?.progress?.attempt_count !== 1) return null
  const events = (payload.progress.events || []).filter(event => event.status === 'VIEWER_READY')
  if (events.length !== 1 || events[0].revision !== payload.revision || !events[0].recorded_at) return null
  const instant = new Date(events[0].recorded_at)
  if (!Number.isFinite(instant.getTime())) return null
  const parts = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(instant)
  return `${parts} (Beijing time, UTC+08:00)`
}

const NON_CLAIM_EXPLANATIONS = {
  SELECTED_DOMAIN_ONLY: 'Only the selected C3D8/C3D8R domain is used for geometry generation.',
  EXCLUDED_REGIONS_NOT_COMPUTED: 'Excluded regions do not participate in this geometry computation.',
  NOT_FULL_BONE_COVERAGE: 'This result does not establish coverage of the full bone model.',
  NOT_MECHANICAL_VALIDATION: 'This result does not establish mechanical validation of the selected domain.',
  NOT_EXCLUDED_ELEMENT_MAPPING: "Conformal mapping of excluded elements is outside this result's scope.",
  NOT_SHELL_MECHANICAL_VALIDATION: 'This result does not establish mechanical validation of shell regions.',
  NOT_MECHANICAL_PASS: 'No mechanical pass conclusion is available for the generated structure.',
  NOT_OOD_PASS: 'No out-of-distribution applicability pass conclusion is available.',
  NOT_FEA_VALIDATION: 'No finite-element validation conclusion is available.',
  NOT_MANUFACTURING_VALIDATION: 'No manufacturing validation conclusion is available.',
  NOT_CLINICAL_VALIDATION: 'No clinical validation conclusion is available.',
  NOT_FULL_DUAL_SPACE_FUSION: 'Full dual-space fusion has not been validated.',
  REPRODUCIBILITY_NOT_VERIFIED: 'Reproducibility has not been verified.',
  NOT_EVALUATED: 'No formal result-package assessment is recorded; this does not mean geometry generation failed.'
}

export function productNonClaims(payload) {
  const records = new Map()
  function visit(value, path) {
    if (!value || typeof value !== 'object') return
    for (const [key, child] of Object.entries(value)) {
      const source = path ? `${path}.${key}` : key
      if (key === 'non_claims' && Array.isArray(child)) {
        for (const code of child.filter(item => typeof item === 'string')) {
          if (!records.has(code)) records.set(code, { code, explanation: NON_CLAIM_EXPLANATIONS[code] || 'No explanation is recorded for this statement.', sources: [] })
          records.get(code).sources.push(source)
        }
      } else if (!Array.isArray(child)) visit(child, source)
    }
  }
  visit(payload, '')
  return [...records.values()]
}

export function productScopeSummary(payload) {
  const claims = productNonClaims(payload)
  const codes = new Set(claims.map(claim => claim.code))
  const domain = ['SELECTED_DOMAIN_ONLY', 'EXCLUDED_REGIONS_NOT_COMPUTED']
    .filter(code => codes.has(code)).map(code => NON_CLAIM_EXPLANATIONS[code])
  const validation = [
    [codes.has('NOT_MECHANICAL_VALIDATION') || codes.has('NOT_SHELL_MECHANICAL_VALIDATION'), 'mechanical validation'],
    [codes.has('NOT_MECHANICAL_PASS') && !codes.has('NOT_MECHANICAL_VALIDATION') && !codes.has('NOT_SHELL_MECHANICAL_VALIDATION'), 'a mechanical pass'],
    [codes.has('NOT_OOD_PASS'), 'an out-of-distribution applicability pass'],
    [codes.has('NOT_FEA_VALIDATION'), 'finite-element validation'],
    [codes.has('NOT_MANUFACTURING_VALIDATION'), 'manufacturing validation'],
    [codes.has('NOT_CLINICAL_VALIDATION'), 'clinical validation']
  ].filter(([recorded]) => recorded).map(([, text]) => text)
  const summary = [
    domain.join(' ') || 'Computation scope is not recorded.'
  ]
  if (validation.length) summary.push(`The saved geometry has no recorded evidence of ${validation.join(', ')}.`)
  else summary.push('Scientific validation status is not recorded.')
  const reminders = []
  if (codes.has('NOT_FULL_DUAL_SPACE_FUSION')) reminders.push(NON_CLAIM_EXPLANATIONS.NOT_FULL_DUAL_SPACE_FUSION)
  if (codes.has('REPRODUCIBILITY_NOT_VERIFIED')) reminders.push(NON_CLAIM_EXPLANATIONS.REPRODUCIBILITY_NOT_VERIFIED)
  if (codes.has('NOT_EVALUATED')) reminders.push('Assessment is incomplete; review this result before use.')
  if (claims.some(claim => !NON_CLAIM_EXPLANATIONS[claim.code])) reminders.push('Additional unrecognized limitations require review before this result is used.')
  if (reminders.length) summary.push(reminders.join(' '))
  return summary
}

export function productPackageAssessment(payload) {
  const evidence = payload?.result?.p13_evidence
  if (evidence?.integrity_status == null && evidence?.status == null) {
    return { state: 'P13 not recorded', meaning: 'No result-package assessment status is recorded.', attention: false }
  }
  const status = evidence.integrity_status
  if (status === 'NOT_EVALUATED' && (!evidence.status || evidence.status === 'not_available_legacy_package')) {
    return { state: 'P13 not evaluated', meaning: 'No formal result-package assessment is recorded. This does not mean geometry generation failed.', attention: false }
  }
  if (status === 'PASS' && evidence.status === 'verified' && evidence.package_contract && typeof evidence.package_contract === 'object' && !Array.isArray(evidence.package_contract)) {
    return { state: 'P13 package checks passed', meaning: 'Recorded package consistency checks passed. This does not establish mechanical, finite-element, manufacturing or clinical validation.', attention: false }
  }
  if (status === 'FAIL' && (!evidence.status || evidence.status === 'failed')) {
    return { state: 'P13 package checks failed', meaning: 'The record reports failed package checks; this is not a geometry-generation conclusion.', attention: true }
  }
  if (status === 'NOT_APPLICABLE' && !evidence.status) {
    return { state: 'P13 not applicable', meaning: 'The record explicitly records this package assessment as not applicable; no validation pass is implied.', attention: false }
  }
  return { state: 'P13 status needs review', meaning: 'The recorded assessment fields do not establish a complete package-check conclusion. Review the result package before using its files.', attention: true }
}

export function productRecordSummary(payload) {
  return [
    ['Job', payload?.job_id],
    ['Record revision', payload?.revision],
    ['Viewer record schema', payload?.schema_version],
    ['Historical producer generation', payload?.result?.compatibility?.producer?.generation],
    ['Current reader generation', payload?.result?.compatibility?.reader?.generation],
    ['Record updated time (not result-ready time)', payload?.updated_at]
  ].map(([name, value]) => [name, value == null || value === '' ? 'Not recorded' : value])
}

export function productGeometryBounds(positions) {
  if (!positions || positions.length < 9 || positions.length % 9 !== 0) throw productStlError('EMPTY_GEOMETRY')
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity]
  const origin = [positions[0], positions[1], positions[2]]
  if (!origin.every(Number.isFinite)) throw productStlError('NONFINITE_GEOMETRY')
  const projectedMin = [Infinity, Infinity], projectedMax = [-Infinity, -Infinity]
  const sqrt6 = Math.sqrt(6)
  // Cache only the actual vertices' fixed-view range, relative to a nearby finite origin.
  for (let index = 0; index < positions.length; index += 3) {
    const x = positions[index], y = positions[index + 1], z = positions[index + 2]
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) throw productStlError('NONFINITE_GEOMETRY')
    min[0] = Math.min(min[0], x); max[0] = Math.max(max[0], x)
    min[1] = Math.min(min[1], y); max[1] = Math.max(max[1], y)
    min[2] = Math.min(min[2], z); max[2] = Math.max(max[2], z)
    const dx = x - origin[0], dy = y - origin[1], dz = z - origin[2]
    const right = (dx - dz) / Math.SQRT2
    const up = (dy - dx) / sqrt6 + (dy - dz) / sqrt6
    if (!Number.isFinite(right) || !Number.isFinite(up)) throw productStlError('NONFINITE_GEOMETRY')
    projectedMin[0] = Math.min(projectedMin[0], right); projectedMax[0] = Math.max(projectedMax[0], right)
    projectedMin[1] = Math.min(projectedMin[1], up); projectedMax[1] = Math.max(projectedMax[1], up)
  }
  if (!min.every((value, axis) => Number.isFinite(max[axis] - value))
    || !projectedMin.every((value, axis) => Number.isFinite(projectedMax[axis] - value))) throw productStlError('NONFINITE_GEOMETRY')
  if (projectedMin.every((value, axis) => value === projectedMax[axis])) throw productStlError('EMPTY_GEOMETRY')
  return { min, max, projection: { origin, min: projectedMin, max: projectedMax } }
}

export function computeProductCameraFit(bounds, width, height) {
  const { min, max, projection } = bounds || {}
  if (![min, max, projection?.origin].every(value => Array.isArray(value) && value.length === 3 && value.every(Number.isFinite))
    || ![projection?.min, projection?.max].every(value => Array.isArray(value) && value.length === 2 && value.every(Number.isFinite))
    || min.some((value, axis) => value > max[axis])
    || projection.min.some((value, axis) => value > projection.max[axis])) throw productStlError('NONFINITE_GEOMETRY')
  const extent = max.map((value, axis) => value - min[axis])
  const diagonal = Math.hypot(...extent)
  const span = projection.min.map((value, axis) => projection.max[axis] - value)
  if (!Number.isFinite(diagonal) || !span.every(Number.isFinite)) throw productStlError('NONFINITE_GEOMETRY')
  if (diagonal <= 0 || span.every(value => value === 0)) throw productStlError('EMPTY_GEOMETRY')
  if (!(width > 0) || !(height > 0) || !Number.isFinite(width) || !Number.isFinite(height)) return null
  const center = min.map((value, axis) => value + extent[axis] / 2)
  const local = center.map((value, axis) => value - projection.origin[axis])
  const sqrt6 = Math.sqrt(6)
  const offsetRight = projection.min[0] + span[0] / 2 - (local[0] - local[2]) / Math.SQRT2
  const offsetUp = projection.min[1] + span[1] / 2 - ((local[1] - local[0]) / sqrt6 + (local[1] - local[2]) / sqrt6)
  const target = [center[0] + offsetRight / Math.SQRT2 - offsetUp / sqrt6,
    center[1] + 2 * offsetUp / sqrt6, center[2] - offsetRight / Math.SQRT2 - offsetUp / sqrt6]
  const aspect = width / height
  const h = Math.max(span[1], span[0] / aspect) / 0.8
  const w = h * aspect
  const distance = diagonal * 2
  const fit = { left: -w / 2, right: w / 2, top: h / 2, bottom: -h / 2,
    near: diagonal * 0.01, far: diagonal * 4,
    target, position: target.map(value => value + distance / Math.sqrt(3)) }
  if (![fit.left, fit.right, fit.top, fit.bottom, fit.near, fit.far, ...fit.target, ...fit.position].every(Number.isFinite)) throw productStlError('NONFINITE_GEOMETRY')
  if (!(fit.right > fit.left) || !(fit.top > fit.bottom) || !(fit.far > fit.near)) throw productStlError('EMPTY_GEOMETRY')
  return fit
}

export function productCameraState(fit, current, restore) {
  return { ...current, left: fit.left, right: fit.right, top: fit.top, bottom: fit.bottom, near: fit.near, far: fit.far,
    ...(restore ? { position: [...fit.position], target: [...fit.target], quaternion: null, zoom: 1 } : {}) }
}

export const PRODUCT_STL_MAX_BYTES = 300 * 1024 * 1024
export const PRODUCT_STL_MAX_TRIANGLES = 6000000
export const PRODUCT_STL_MAX_GEOMETRY_BYTES = 432000000
const PREVIEW_MESSAGES = {
  STL_TOO_LARGE: 'The STL file exceeds the 300 MiB read limit.',
  STL_TRIANGLE_LIMIT: 'The STL triangle count exceeds the allowed limit.',
  STL_LENGTH_INVALID: 'The STL file length does not match its declared triangle count.',
  STL_FORMAT_INVALID: 'The STL format could not be validated as a supported format.',
  STL_PARSE_FAILED: 'The STL data could not be parsed. The saved task record remains available.',
  EMPTY_GEOMETRY: 'The STL does not contain valid nonempty geometry.',
  NONFINITE_GEOMETRY: 'The STL geometry contains invalid numeric values.',
  STL_RESOURCE_LIMIT: 'The STL geometry exceeds the allowed resource limit.',
  STL_STREAM_UNAVAILABLE: 'Bounded STL reading is unavailable in this environment.',
  HTTP_ERROR: 'The STL preview file could not be read. The saved result information and file links remain available.',
  READ_TIMEOUT: 'The 3D preview read timed out.',
  NETWORK_ERROR: 'The 3D preview read failed because of a connection error.',
  WEBGL_UNAVAILABLE: 'The canvas could not establish a 3D display.',
  CONTEXT_LOST: 'The 3D display context was lost.',
  RENDER_ERROR: 'The 3D display failed. The saved task record remains available.'
}

export function productStlError(code, stage = 'parse', extra = {}) {
  return Object.assign(new Error(PREVIEW_MESSAGES[code] || PREVIEW_MESSAGES.RENDER_ERROR), { code, stage, ...extra })
}

export function productPreviewState(requestKey) {
  return { request_key: requestKey, status: requestKey ? 'loading' : 'missing', parsed: false, diagnostic: null, timings: {} }
}

export function reduceProductPreview(state, event) {
  if (event.request_key !== state.request_key || state.status === 'missing') return state
  if (event.type === 'error') return { ...state, status: event.diagnostic.stage === 'read' ? 'load_error' : `${event.diagnostic.stage}_error`, diagnostic: event.diagnostic, timings: event.timings || state.timings }
  if (state.status !== 'loading') return state
  if (event.type === 'parsed') return { ...state, parsed: true, timings: event.timings || state.timings }
  if (event.type === 'frame' && state.parsed) return { ...state, status: 'ready', timings: event.timings || state.timings }
  return state
}

export function productPreviewDiagnostic(error, context) {
  const code = Object.hasOwn(PREVIEW_MESSAGES, error?.code) ? error.code : 'RENDER_ERROR'
  const resource = String(context.resource || '').split(/[?#]/)[0]
  return { code, stage: ['read', 'parse', 'render'].includes(error?.stage) ? error.stage : 'render',
    job_id: context.job_id, package_id: context.package_id, request_key: context.request_key,
    safe_resource: /^\/api\/viewer\/product\//.test(resource) ? resource : 'STL preview resource',
    http_status: Number.isInteger(error?.http_status) ? error.http_status : null,
    message: PREVIEW_MESSAGES[code], occurred_at: new Date().toISOString() }
}

export async function readProductStlBytes(response, { signal, maxBytes = PRODUCT_STL_MAX_BYTES } = {}) {
  if (!response.ok) throw productStlError('HTTP_ERROR', 'read', { http_status: response.status })
  if (Number(response.headers?.get('Content-Length')) > maxBytes) {
    await response.body?.cancel()
    throw productStlError('STL_TOO_LARGE', 'read')
  }
  if (!response.body?.getReader) throw productStlError('STL_STREAM_UNAVAILABLE', 'read')
  const reader = response.body.getReader()
  let chunks = [], total = 0, complete = false
  try {
    while (true) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      const { done, value } = await reader.read()
      if (done) { complete = true; break }
      total += value.byteLength
      if (total > maxBytes) throw productStlError('STL_TOO_LARGE', 'read')
      chunks.push(value)
    }
    const joined = new Uint8Array(total)
    let offset = 0
    for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.byteLength }
    return joined.buffer
  } finally {
    if (!complete) { try { await reader.cancel() } catch { /* retain the original read error */ } }
    reader.releaseLock()
    chunks = []
  }
}

export function inspectProductStl(buffer) {
  const bytes = buffer.byteLength
  if (bytes > PRODUCT_STL_MAX_BYTES) throw productStlError('STL_TOO_LARGE')
  const prefix = new TextDecoder().decode(new Uint8Array(buffer, 0, Math.min(bytes, 80)))
  if (bytes >= 84) {
    const triangles = new DataView(buffer).getUint32(80, true)
    const expected = 84 + 50 * triangles
    if (Number.isSafeInteger(expected) && expected === bytes) {
      if (triangles > PRODUCT_STL_MAX_TRIANGLES) throw productStlError('STL_TRIANGLE_LIMIT')
      if (!triangles) throw productStlError('EMPTY_GEOMETRY')
      return { format: 'binary', bytes, triangles }
    }
    if (!/^\s*solid[^\r\n]*[\r\n]/.test(prefix)) {
      if (triangles > PRODUCT_STL_MAX_TRIANGLES) throw productStlError('STL_TRIANGLE_LIMIT')
      throw productStlError('STL_LENGTH_INVALID')
    }
  }
  let text
  try { text = new TextDecoder('utf-8', { fatal: true }).decode(buffer) } catch { throw productStlError('STL_FORMAT_INVALID') }
  const opening = /^\s*solid[^\r\n]*[\r\n]/.exec(text)
  if (!opening) throw productStlError('STL_FORMAT_INVALID')
  const number = '[+-]?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][+-]?\\d+)?'
  const vector = `\\s+${number}\\s+${number}\\s+${number}`
  const facet = new RegExp(`\\s*facet\\s+normal${vector}\\s+outer\\s+loop(?:\\s+vertex${vector}){3}\\s+endloop\\s+endfacet`, 'y')
  let offset = opening[0].length, triangles = 0
  while (true) {
    facet.lastIndex = offset
    if (!facet.test(text)) break
    offset = facet.lastIndex
    triangles += 1
    if (triangles > PRODUCT_STL_MAX_TRIANGLES) throw productStlError('STL_TRIANGLE_LIMIT')
  }
  if (!/^\s*endsolid[^\r\n]*\s*$/.test(text.slice(offset))) throw productStlError('STL_FORMAT_INVALID')
  if (!triangles) throw productStlError('EMPTY_GEOMETRY')
  return { format: 'ascii', bytes, triangles }
}

export function validateProductGeometry(geometry) {
  const position = geometry?.attributes?.position
  const normal = geometry?.attributes?.normal
  if (!position?.array?.length || position.array.length % 9) throw productStlError('EMPTY_GEOMETRY')
  const triangles = position.array.length / 9
  if (triangles > PRODUCT_STL_MAX_TRIANGLES) throw productStlError('STL_TRIANGLE_LIMIT')
  if (!normal || position.itemSize !== 3 || normal.itemSize !== 3 || normal.array.length !== position.array.length) throw productStlError('STL_FORMAT_INVALID')
  let allocatedBytes = 0
  for (const attribute of Object.values(geometry.attributes)) {
    if (!attribute.array || attribute.count !== position.count) throw productStlError('STL_FORMAT_INVALID')
    allocatedBytes += attribute.array.byteLength
    if (allocatedBytes > PRODUCT_STL_MAX_GEOMETRY_BYTES) throw productStlError('STL_RESOURCE_LIMIT')
    // Position finiteness, world bounds and fixed-view projection share the traversal below.
    if (attribute !== position) for (const value of attribute.array) if (!Number.isFinite(value)) throw productStlError('NONFINITE_GEOMETRY')
  }
  const bounds = productGeometryBounds(position.array)
  return { bounds, triangles, allocatedBytes }
}
