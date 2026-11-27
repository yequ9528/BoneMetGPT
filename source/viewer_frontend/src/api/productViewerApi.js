const DEFAULT_PRODUCT_API_BASE = 'http://127.0.0.1:8001'
const PRODUCT_JOB_ID_RE = /^job-[0-9A-F]{24}$/
const PRODUCT_FILE_KEYS = new Set(['unit_png', 'unit_stl', 'conformal_inp'])

function apiBase() {
  const configured = import.meta.env?.VITE_LATTICEGPT_VIEWER_API_BASE || (import.meta.env?.PROD && '/')
  return String(configured || DEFAULT_PRODUCT_API_BASE).replace(/\/+$/, '')
}

function requireJobId(jobId) {
  if (typeof jobId !== 'string' || !PRODUCT_JOB_ID_RE.test(jobId)) {
    throw new Error('Invalid product job_id')
  }
  return jobId
}

export function productJobUrl(jobId) {
  return `/api/viewer/product/jobs/${encodeURIComponent(requireJobId(jobId))}`
}

export function productJobFileUrl(jobId, fileKey) {
  if (!PRODUCT_FILE_KEYS.has(fileKey)) throw new Error('Invalid product file_key')
  return `${productJobUrl(jobId)}/deliver/${encodeURIComponent(fileKey)}`
}

export function fullProductApiUrl(relativeUrl) {
  if (typeof relativeUrl !== 'string' || !relativeUrl.startsWith('/api/viewer/product/jobs/')) {
    throw new Error('Expected a relative product API URL')
  }
  return `${apiBase()}${relativeUrl}`
}

const SAFE_ERRORS = {
  JOB_NOT_FOUND: 'Task not found. Check the complete task ID.',
  JOB_CONTRACT_INVALID: 'Task or event records failed consistency checks. Preserve the current files.',
  PACKAGE_CONTRACT_INVALID: 'The result package failed consistency checks. Preserve the current files.',
  FILE_MISSING: 'The requested result file is missing.',
  FILE_IDENTITY_MISMATCH: 'The result file size or hash differs from its record. Preserve the current files.',
  PRODUCT_VIEWER_INTERNAL_ERROR: 'The result could not be read. Check the service logs.',
  INVALID_VIEWER_RESPONSE: 'The Viewer returned an invalid response.',
  VIEWER_UNREACHABLE: 'The Viewer is unreachable. Check the service status.'
}

function viewerError(code, jobId) {
  const safeCode = Object.hasOwn(SAFE_ERRORS, code) ? code : 'INVALID_VIEWER_RESPONSE'
  return Object.assign(new Error(SAFE_ERRORS[safeCode]), { code: safeCode, jobId })
}

export async function fetchProductJob(jobId) {
  const url = fullProductApiUrl(productJobUrl(jobId))
  let response
  try { response = await fetch(url) }
  catch { throw viewerError('VIEWER_UNREACHABLE', jobId) }
  let body
  try { body = await response.json() }
  catch { throw viewerError('INVALID_VIEWER_RESPONSE', jobId) }
  if (!response.ok || body?.status !== 'success' || !body?.data) {
    throw viewerError(body?.code, jobId)
  }
  if (body.data.job_id !== jobId) throw viewerError('JOB_CONTRACT_INVALID', jobId)
  return body.data
}

export async function fetchOriginalMesh(jobId, {signal}={}) {
  let response
  try { response = await fetch(fullProductApiUrl(productJobUrl(jobId))+'?include=original_mesh',{signal}) }
  catch (error) { if (error.name === 'AbortError') throw error; throw viewerError('VIEWER_UNREACHABLE',jobId) }
  let body
  try { body = await response.json() } catch { throw viewerError('INVALID_VIEWER_RESPONSE',jobId) }
  if (!response.ok || body?.status !== 'success') throw viewerError(body?.code || 'INVALID_VIEWER_RESPONSE',jobId)
  if (body.data?.job_id !== jobId) throw viewerError('JOB_CONTRACT_INVALID',jobId)
  return body.data
}


export function candidatePreviewUrl(payload,candidate) {
  const preview=candidate?.preview, sha=payload?.candidate_set_sha256
  if(!/^[A-Fa-f0-9]{64}$/.test(sha||'') || preview?.candidate_set_sha256!==sha
     || preview?.renderer_version!=='owner-cell-png-v1' || !candidate?.candidate_id)throw new Error('Candidate preview identity is unavailable')
  const expected=`${productJobUrl(payload.job_id)}/candidates/${encodeURIComponent(candidate.candidate_id)}/preview.png?candidate_set_sha256=${sha}&renderer_version=${preview.renderer_version}`
  if(preview.url!==expected)throw new Error('Candidate preview does not belong to this Job')
  return expected
}
