import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import * as THREE from 'three'


const productModuleUrl = new URL('../src/utils/productJobView.js', import.meta.url)

test('preview only becomes ready after parsed geometry and a frame for the current request', async () => {
  const v = await loadProductModule()
  let state = v.productPreviewState('a')
  assert.equal(state.status, 'loading')
  assert.equal(v.reduceProductPreview(state, { type: 'frame', request_key: 'a' }).status, 'loading')
  state = v.reduceProductPreview(state, { type: 'parsed', request_key: 'a' })
  assert.equal(v.reduceProductPreview(state, { type: 'frame', request_key: 'old' }), state)
  assert.equal(v.reduceProductPreview(state, { type: 'frame', request_key: 'a' }).status, 'ready')
  assert.equal(v.reduceProductPreview(state, { type: 'abort', request_key: 'a' }), state)
  assert.equal(v.reduceProductPreview(state, { type: 'fallback', request_key: 'a' }), state)
  for (const stage of ['read', 'parse', 'render']) {
    const failed = v.reduceProductPreview(state, { type: 'error', request_key: 'a', diagnostic: { stage } })
    assert.equal(failed.status, stage === 'read' ? 'load_error' : `${stage}_error`)
    assert.equal(v.reduceProductPreview(failed, { type: 'frame', request_key: 'a' }), failed)
  }
  assert.equal(v.productPreviewState(null).status, 'missing')
})

test('STL precheck recognizes solid-prefixed binary and rejects length, facet and format limits', async () => {
  const v = await loadProductModule()
  const binary = new ArrayBuffer(134)
  new Uint8Array(binary).set(new TextEncoder().encode('solid binary'))
  new DataView(binary).setUint32(80, 1, true)
  assert.deepEqual(v.inspectProductStl(binary), { format: 'binary', bytes: 134, triangles: 1 })
  const truncated = binary.slice(0, 133)
  assert.throws(() => v.inspectProductStl(truncated), e => e.code === 'STL_LENGTH_INVALID')
  const ascii = new TextEncoder().encode('solid a\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid a\n').buffer
  assert.equal(v.inspectProductStl(ascii).triangles, 1)
  for (const text of ['solid a\nendsolid a', 'solid a\nfacet normal 0 0 1\nvertex 0 0 0\nendsolid a', 'garbage']) {
    assert.throws(() => v.inspectProductStl(new TextEncoder().encode(text).buffer))
  }
  const tooMany = new ArrayBuffer(84)
  new DataView(tooMany).setUint32(80, 6000001, true)
  assert.throws(() => v.inspectProductStl(tooMany), e => e.code === 'STL_TRIANGLE_LIMIT')
})

test('bounded stream checks actual bytes even with a false or absent Content-Length and releases reader', async () => {
  const v = await loadProductModule()
  let cancelled = 0, released = 0
  const response = chunks => ({ ok: true, status: 200, headers: { get: () => '1' }, body: { getReader: () => ({
    async read() { return chunks.length ? { value: chunks.shift(), done: false } : { done: true } },
    async cancel() { cancelled += 1 }, releaseLock() { released += 1 }
  }) } })
  const bytes = await v.readProductStlBytes(response([new Uint8Array([1, 2]), new Uint8Array([3])]), { maxBytes: 3 })
  assert.deepEqual([...new Uint8Array(bytes)], [1, 2, 3])
  await assert.rejects(v.readProductStlBytes(response([new Uint8Array(4)]), { maxBytes: 3 }), e => e.code === 'STL_TOO_LARGE')
  assert.equal(cancelled, 1)
  assert.equal(released, 2)
  await assert.rejects(v.readProductStlBytes({ ok: false, status: 404 }), e => e.http_status === 404)
  await assert.rejects(v.readProductStlBytes({ ok: true, headers: { get: () => null }, body: null }), e => e.code === 'STL_STREAM_UNAVAILABLE')
})

test('parsed geometry rejects empty, nonfinite and mismatched arrays but accepts planar triangles', async () => {
  const v = await loadProductModule()
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute([0, 0, 0, 1, 0, 0, 0, 1, 0], 3))
  geometry.setAttribute('normal', new THREE.Float32BufferAttribute([0, 0, 1, 0, 0, 1, 0, 0, 1], 3))
  assert.equal(v.validateProductGeometry(geometry).triangles, 1)
  geometry.attributes.normal.array[0] = Infinity
  assert.throws(() => v.validateProductGeometry(geometry), e => e.code === 'NONFINITE_GEOMETRY')
  geometry.dispose()
  assert.throws(() => v.validateProductGeometry(new THREE.BufferGeometry()), e => e.code === 'EMPTY_GEOMETRY')
})

test('preview diagnostics expose safe resource and fixed messages without backend paths', async () => {
  const v = await loadProductModule()
  const diagnostic = v.productPreviewDiagnostic({ code: 'STL_PARSE_FAILED', stage: 'parse', message: 'secret C:/private' }, { request_key: 'k', job_id: 'j', package_id: 'p', resource: '/api/viewer/product/jobs/j/files/stl?secret=x' })
  assert.equal(diagnostic.safe_resource, '/api/viewer/product/jobs/j/files/stl')
  assert.doesNotMatch(diagnostic.message, /secret|private/)
  assert.equal(diagnostic.request_key, 'k')
})

async function loadProductModule() {
  assert.equal(
    existsSync(fileURLToPath(productModuleUrl)),
    true,
    'productJobView.js is missing'
  )
  return import(productModuleUrl)
}

test('current large binary preview and the new facet boundary pass without weakening byte validation', async () => {
  const v = await loadProductModule()
  for (const [triangles, bytes] of [[1272192, 63609684], [2738944, 136947284], [6000000, 300000084]]) {
    const buffer = new ArrayBuffer(bytes)
    new DataView(buffer).setUint32(80, triangles, true)
    assert.deepEqual(v.inspectProductStl(buffer), { format: 'binary', bytes, triangles })
  }
  const tooMany = new ArrayBuffer(300000134)
  new DataView(tooMany).setUint32(80, 6000001, true)
  assert.throws(() => v.inspectProductStl(tooMany), e => e.code === 'STL_TRIANGLE_LIMIT')
  assert.throws(() => v.inspectProductStl(new ArrayBuffer(314572801)), e => e.code === 'STL_TOO_LARGE' && /300 MiB/.test(e.message))
})

test('default bounded read accepts exactly 300 MiB and rejects both advertised and actual excess', async () => {
  const v = await loadProductModule()
  let cancelled = 0, released = 0
  const response = (length, chunk) => ({ ok: true, headers: { get: () => length }, body: {
    async cancel() { cancelled++ },
    getReader() { let done = false; return {
      async read() { if (done) return { done: true }; done = true; return { done: false, value: chunk } },
      async cancel() { cancelled++ }, releaseLock() { released++ }
    } }
  } })
  const bytes = await v.readProductStlBytes(response(null, new Uint8Array(314572800)))
  assert.equal(bytes.byteLength, 314572800)
  await assert.rejects(v.readProductStlBytes(response('314572801', null)), e => e.code === 'STL_TOO_LARGE')
  await assert.rejects(v.readProductStlBytes(response('1', new Uint8Array(314572801))), e => e.code === 'STL_TOO_LARGE')
  assert.equal(cancelled, 2)
  assert.equal(released, 2)
})

test('6 million parsed faces retain the independent 432000000-byte attribute budget', async () => {
  const v = await loadProductModule()
  const geometry = new THREE.BufferGeometry()
  const values = new Float32Array(54000000)
  values[0] = 1; values[4] = 1
  geometry.setAttribute('position', new THREE.BufferAttribute(values, 3))
  geometry.setAttribute('normal', new THREE.BufferAttribute(values, 3))
  assert.equal(v.validateProductGeometry(geometry).allocatedBytes, 432000000)
  geometry.setAttribute('color', new THREE.BufferAttribute(new Uint8Array(18000000), 1))
  assert.throws(() => v.validateProductGeometry(geometry), e => e.code === 'STL_RESOURCE_LIMIT')
  geometry.dispose()
})


test('candidate snapshot resolves to candidate state', async () => {
  const { resolveProductViewState } = await loadProductModule()
  assert.equal(resolveProductViewState({
    status: 'CANDIDATES_READY',
    mesh: null,
    progress: { attempt_count: 0 }
  }), 'candidate')
})

test('mesh binding before attempt is mesh state', async () => {
  const { resolveProductViewState } = await loadProductModule()
  assert.equal(resolveProductViewState({
    status: 'AWAITING_CONFORMAL_AUTHORIZATION',
    mesh: { status: 'available' },
    progress: { attempt_count: 0 }
  }), 'mesh')
})

test('started attempt is running state', async () => {
  const { resolveProductViewState } = await loadProductModule()
  assert.equal(resolveProductViewState({
    status: 'ADAPTER_RUNNING',
    mesh: { status: 'available' },
    progress: { attempt_count: 1 }
  }), 'running')
})

test('verified package is result state', async () => {
  const { resolveProductViewState } = await loadProductModule()
  assert.equal(resolveProductViewState({
    status: 'VIEWER_READY',
    result: { package_id: 'job-1234567890ABCDEF12345678-attempt-0001' }
  }), 'result')
})

test('failure is terminal failed state', async () => {
  const { resolveProductViewState } = await loadProductModule()
  assert.equal(resolveProductViewState({
    status: 'FAILED',
    progress: { failure: { stage: 'CONFORMAL_RUNNING' } }
  }), 'failed')
})

test('polling stops only for result and failed states', async () => {
  const { shouldPollProductJob } = await loadProductModule()
  assert.equal(shouldPollProductJob({ view_state: 'candidate' }), true)
  assert.equal(shouldPollProductJob({ view_state: 'mesh' }), true)
  assert.equal(shouldPollProductJob({ view_state: 'running' }), true)
  assert.equal(shouldPollProductJob({ view_state: 'result' }), false)
  assert.equal(shouldPollProductJob({ view_state: 'failed' }), false)
})

test('product query requires a safe job id', async () => {
  const { isSafeProductJobId } = await loadProductModule()
  const { readInitialViewerQuery } = await import(
    new URL('../src/utils/queryParams.js', import.meta.url)
  )

  const jobId = 'job-1234567890ABCDEF12345678'
  assert.equal(isSafeProductJobId(jobId), true)
  assert.equal(isSafeProductJobId('../latest'), false)
  assert.deepEqual(
    readInitialViewerQuery(`?mode=product&job_id=${jobId}`),
    { mode: 'product', jobId, error: '' }
  )
  assert.deepEqual(
    readInitialViewerQuery('?mode=product&job_id=latest'),
    {
      mode: 'product',
      jobId: 'latest',
      error: 'Invalid product job_id'
    }
  )
})

test('product identity summary exposes revision and disposition', async () => {
  const { formatProductJobIdentity } = await loadProductModule()
  assert.equal(
    formatProductJobIdentity({
      job_id: 'job-1234567890ABCDEF12345678',
      revision: 7,
      workflow_disposition: 'ACTIVE',
      updated_at: '2026-08-02T01:02:03+00:00'
    }),
    'job-1234567890ABCDEF12345678 · revision 7 · ACTIVE · updated 2026-08-02T01:02:03+00:00'
  )
})

test('product page state fails closed without changing embedded resolution', async () => {
  const v = await loadProductModule()
  assert.equal(v.getProductPageState({ view_state: 'invented', progress: { job_status: 'VIEWER_READY' }, result: {} }), 'unknown')
  assert.equal(v.getProductPageState({ view_state: 'result', progress: { job_status: 'CONFORMAL_RUNNING' }, result: {} }), 'unknown')
  assert.equal(v.getProductPageState({ view_state: 'running' }), 'running')
  assert.equal(v.getProductPageState({ view_state: 'result', progress: { job_status: 'VIEWER_READY' }, result: { package_id: 'p' } }), 'result')
  assert.equal(v.resolveProductViewState({ status: 'VIEWER_READY', result: {} }), 'result')
})

test('nine independent values retain order, sign and tiny nonzero values without invented zero', async () => {
  const v = await loadProductModule()
  const rows = v.productTargetComponents({ physical_9d_gpa: { C11: 1.23456789, C12: -0.00000000012, C13: 0, C22: NaN, C23: Infinity } })
  assert.deepEqual(rows.map(row => row.name), ['C11', 'C12', 'C13', 'C22', 'C23', 'C33', 'C44', 'C55', 'C66'])
  assert.equal(rows[0].display, '1.23457')
  assert.equal(Number(rows[1].display), -1.2e-10)
  assert.match(rows[1].display, /e-/)
  assert.equal(rows[2].display, '0')
  assert.ok(rows.slice(3).every(row => row.display === 'Not recorded'))
  assert.equal(rows[0].raw, 1.23456789)
  for (const value of [null, undefined, '', '3', NaN, Infinity, -Infinity]) assert.equal(v.formatProductNumber(value), 'Not recorded')
})

test('progress uses recorded evidence and package format controls compatibility text', async () => {
  const v = await loadProductModule()
  assert.equal(v.productProgressSummary({}), 'Target confirmation not recorded → Candidate selection pending → Mesh binding pending → Task status needs review')
  const payload = { view_state: 'result', progress: { job_status: 'VIEWER_READY' }, target: { target_sha256: 'hash' }, selection: { selected_candidate_id: 'c' }, mesh: { mesh_binding_sha256: 'mesh' }, result: { package_id: 'p' } }
  assert.equal(v.productProgressSummary(payload), 'Target confirmed → System recommendation (confirmation pending) → Mesh bound → Result generated')
  payload.selection.confirmation_status = 'CONFIRMED'
  assert.match(v.productProgressSummary(payload), /Candidate explicitly confirmed/)
  payload.selection.confirmation_status = 'LEGACY_NOT_RECORDED'
  assert.match(v.productProgressSummary(payload), /Historical selection \(explicit confirmation not recorded\)/)
  assert.equal(v.productContractText({ compatibility: { package_format: 'PRODUCT_SIX_FILES_V1', producer: { generation: 'v11' }, reader: { generation: 'v12' } } }), 'Saved result read under the product result-package contract.')
  assert.equal(v.productContractText({ compatibility: { package_format: 'LEGACY_SIX_FILES_V1' } }), 'Viewing a registered historical result; the original production record is unchanged.')
  const verifiedP13 = { status: 'verified', integrity_status: 'PASS', package_contract: { package: { package_id: 'p' } } }
  assert.match(v.productContractText({ compatibility: null, p13_evidence: verifiedP13 }), /P13/)
  assert.doesNotMatch(v.productContractText({ compatibility: null, p13_evidence: verifiedP13 }), /NOT_EVALUATED|PASS/)
  for (const evidence of [null, {}, { status: 'not_available_legacy_package', integrity_status: 'NOT_EVALUATED' }, { status: 'unknown', integrity_status: 'PASS' }]) assert.equal(v.productContractText({ compatibility: null, p13_evidence: evidence }), 'Result-reading contract needs review')
  assert.equal(v.productContractText({ compatibility: { package_format: 'unknown' } }), 'Result-reading contract needs review')
})

test('task presentation distinguishes recorded waiting, running, interruption, failure and result states', async () => {
  const v = await loadProductModule()
  const present = (status, overrides = {}) => v.productTaskPresentation({ progress: { job_status: status }, ...overrides })
  assert.equal(present('TARGET_PROPOSED').kind, 'waiting')
  assert.match(present('TARGET_PROPOSED').next, /Confirm target and generate 3 candidates/)
  assert.equal(present('TARGET_CONFIRMED').kind, 'waiting')
  assert.match(present('TARGET_CONFIRMED').title, /confirmed/)
  assert.equal(present('CANDIDATES_READY').kind, 'waiting')
  assert.match(present('CANDIDATES_READY').next, /confirm/)
  assert.match(present('CANDIDATES_READY', { selection: { confirmation_status: 'CONFIRMED' } }).next, /upload/)
  for (const status of ['MESH_READY', 'AWAITING_CONFORMAL_AUTHORIZATION']) assert.equal(present(status).kind, 'waiting')
  assert.match(present('AWAITING_CONFORMAL_AUTHORIZATION').next, /Start conformal generation/)
  for (const status of ['INVERSE_RUNNING', 'ADAPTER_RUNNING', 'CONFORMAL_RUNNING', 'PACKAGE_VERIFYING']) {
    assert.equal(present(status).kind, 'running', status)
    assert.match(present(status).next, /Refresh Job status/)
  }
  assert.equal(present('FAILED').kind, 'failed')
  assert.match(present('FAILED').title, /Job failed/)
  const interrupted = present('HOLD_RUNTIME_INTERRUPTED')
  assert.equal(interrupted.kind, 'interrupted')
  assert.match(interrupted.title, /interrupted/)
  assert.doesNotMatch(interrupted.title, /failed|succeeded/)
  assert.match(interrupted.next, /No automatic retry/)
  assert.equal(present('VIEWER_READY', { view_state: 'result', result: { package_id: 'p' } }).kind, 'result')
  for (const status of [undefined, '', 'INVENTED']) assert.equal(present(status).kind, 'unknown')
  assert.equal(present('VIEWER_READY').kind, 'unknown')
})

test('unknown or absent task states cannot turn a target hash into confirmed target evidence', async () => {
  const v = await loadProductModule()
  for (const status of [undefined, '', 'INVENTED']) {
    const summary = v.productProgressSummary({ target: { target_sha256: 'hash' }, progress: { job_status: status } })
    assert.doesNotMatch(summary, /Target confirmed/)
    assert.match(summary, /needs review/)
  }
  assert.match(v.productProgressSummary({ target: { target_sha256: 'hash' }, progress: { job_status: 'TARGET_CONFIRMED' } }), /Target confirmed/)
})

test('explicit identity refusal takes priority over previous successful snapshot', async () => {
  const v = await loadProductModule()
  const previous = { result: { package_id: 'p' } }
  for (const code of ['JOB_NOT_FOUND', 'FILE_MISSING', 'FILE_IDENTITY_MISMATCH', 'JOB_CONTRACT_INVALID', 'PACKAGE_CONTRACT_INVALID', 'PRODUCT_VIEWER_INTERNAL_ERROR']) {
    const failure = v.productReadFailure({ code, message: '安全原因' }, previous)
    assert.equal(failure.retainSnapshot, false, code)
    assert.equal(failure.downloadsEnabled, false)
  }
  for (const code of ['VIEWER_UNREACHABLE', 'INVALID_VIEWER_RESPONSE']) {
    assert.equal(v.productReadFailure({ code }, previous).retainSnapshot, true)
    assert.equal(v.productReadFailure({ code }, null).retainSnapshot, false)
  }
  assert.match(v.productReadFailure({ code: 'VIEWER_UNREACHABLE' }, null).message, /refresh this page/)
})

test('read failures have English reading summaries while preserving the recorded reason as a diagnostic', async () => {
  const v = await loadProductModule()
  for (const code of ['FILE_MISSING', 'FILE_IDENTITY_MISMATCH', 'JOB_CONTRACT_INVALID', 'PACKAGE_CONTRACT_INVALID']) {
    const error = v.productReadFailure({ code, message: '保留的原始读取诊断' }, null)
    assert.doesNotMatch(error.message, /[\p{Script=Han}]/u)
    assert.equal(error.reason, '保留的原始读取诊断')
    assert.doesNotMatch(error.message, /calculation failed/i)
    assert.equal(error.downloadsEnabled, false)
  }
})

test('failure wording distinguishes input validation, preflight and recorded computation stages', async () => {
  const v = await loadProductModule()
  const title = stage => v.productTaskPresentation({ progress: { job_status: 'FAILED', failure: { stage } } }).title
  assert.equal(title('TARGET_CONFIRMED'), 'Target validation did not pass; inverse design not started')
  assert.equal(title('FORMAL_BUNDLE_PREFLIGHT'), 'Preflight did not pass')
  assert.equal(title('INVERSE_RUNNING'), 'Candidate generation failed')
  assert.equal(title('ADAPTER_RUNNING'), 'Geometry-input preparation failed')
  assert.equal(title('CONFORMAL_RUNNING'), 'Geometry generation failed')
  assert.equal(title('PACKAGE_VERIFYING'), 'Generated-file verification failed')
  for (const stage of [undefined, '', 'INVENTED']) {
    assert.match(title(stage), /Job failed/)
    assert.doesNotMatch(title(stage), /Calculation failed|generation failed|result generated/)
  }
})

test('a held candidate step directs review rather than candidate confirmation', async () => {
  const v = await loadProductModule()
  const payload = { workflow_disposition: 'HOLD', view_state: 'candidate',
    progress: { job_status: 'CANDIDATES_READY' },
    selection: { selected_candidate_id: 'candidate-1' }, candidates: [{ candidate_id: 'candidate-1', eligible: false }] }
  const original = JSON.stringify(payload)
  const task = v.productTaskPresentation(payload)
  assert.equal(task.title, 'Candidate step on hold')
  assert.match(task.next, /recorded eligibility evidence/)
  assert.match(task.next, /Refresh Job status/)
  assert.doesNotMatch(task.title + task.next, /confirmation pending|confirm your choice|P-?09|precheck failed/i)
  assert.doesNotMatch(v.productProgressSummary(payload), /confirmation pending|selection pending/i)
  assert.equal(JSON.stringify(payload), original)
})

test('pending exact-Job reads are shared across effect reentry and are released on success or failure', async () => {
  const v = await loadProductModule()
  let count = 0
  let complete
  const read = v.createProductJobReader(() => { count += 1; return new Promise(resolve => { complete = resolve }) })
  const first = read('job-A')
  assert.equal(read('job-A'), first)
  assert.equal(count, 1)
  complete({ revision: 1 })
  await first
  const second = read('job-A')
  assert.notEqual(second, first)
  assert.equal(count, 2)
  complete({ revision: 2 })
  await second
  const fails = v.createProductJobReader(() => Promise.reject(new Error('network')))
  await assert.rejects(fails('job-A'))
  await assert.rejects(fails('job-A'))
})

test('ready time comes only from the unique consistent terminal event', async () => {
  const v = await loadProductModule()
  const p = { revision: 10, updated_at: '2099-01-01', progress: { job_status: 'VIEWER_READY', attempt_count: 1, events: [{ event: 'VIEWER_READY', status: 'VIEWER_READY', revision: 10, recorded_at: '2026-09-17T01:00:32+00:00' }] } }
  assert.equal(v.productReadyTime(p), '2026-09-17 09:00:32 (Beijing time, UTC+08:00)')
  assert.equal(v.productReadyTime({ ...p, progress: { ...p.progress, attempt_count: 2 } }), null)
  assert.equal(v.productReadyTime({ ...p, revision: 11 }), null)
  assert.equal(v.productReadyTime({ ...p, progress: { ...p.progress, events: [] } }), null)
  assert.equal(v.productReadyTime({ ...p, progress: { ...p.progress, events: [...p.progress.events, { ...p.progress.events[0], recorded_at: '2026-09-17T02:00:00Z' }] } }), null)
})

test('all original nonclaim arrays retain origins and unknown explanations', async () => {
  const v = await loadProductModule()
  const claims = v.productNonClaims({ non_claims: ['NOT_FEA_VALIDATION'], result: { p13_evidence: { non_claims: ['NOT_FEA_VALIDATION', 'UNKNOWN_CLAIM'] } } })
  assert.equal(claims.find(row => row.code === 'NOT_FEA_VALIDATION').sources.length, 2)
  assert.equal(claims.find(row => row.code === 'UNKNOWN_CLAIM').explanation, 'No explanation is recorded for this statement.')
})

test('domain limitations explain the six known codes without losing unknown codes, origins or input data', async () => {
  const v = await loadProductModule()
  const codes = ['SELECTED_DOMAIN_ONLY', 'EXCLUDED_REGIONS_NOT_COMPUTED', 'NOT_FULL_BONE_COVERAGE',
    'NOT_MECHANICAL_VALIDATION', 'NOT_EXCLUDED_ELEMENT_MAPPING', 'NOT_SHELL_MECHANICAL_VALIDATION']
  const payload = { mesh: { design_domain: { excluded_element_count: 0, non_claims: codes } },
    non_claims: ['NOT_EXCLUDED_ELEMENT_MAPPING', 'UNRECOGNIZED_LIMIT'] }
  const before = JSON.stringify(payload)
  const claims = v.productNonClaims(payload)
  const explanations = claims.map(row => row.explanation).join(' ')
  for (const phrase of ['selected C3D8/C3D8R domain', 'Excluded regions do not participate',
    'full bone model', 'mechanical validation of the selected domain',
    "excluded elements is outside this result's scope", 'mechanical validation of shell regions']) {
    assert.ok(explanations.includes(phrase), phrase)
  }
  assert.deepEqual(claims.find(row => row.code === 'NOT_EXCLUDED_ELEMENT_MAPPING').sources,
    ['mesh.design_domain.non_claims', 'non_claims'])
  assert.equal(claims.find(row => row.code === 'UNRECOGNIZED_LIMIT').explanation, 'No explanation is recorded for this statement.')
  assert.doesNotMatch(explanations, /mapping file.*missing/i)
  assert.equal(JSON.stringify(payload), before)
})

test('scope summary keeps the recorded computation boundary short and leaves full domain limitations in evidence', async () => {
  const v = await loadProductModule()
  const payload = { non_claims: ['SELECTED_DOMAIN_ONLY', 'EXCLUDED_REGIONS_NOT_COMPUTED',
    'NOT_FULL_BONE_COVERAGE', 'NOT_EXCLUDED_ELEMENT_MAPPING', 'NOT_MECHANICAL_VALIDATION',
    'NOT_SHELL_MECHANICAL_VALIDATION', 'NOT_MECHANICAL_PASS', 'NOT_OOD_PASS', 'NOT_FEA_VALIDATION',
    'NOT_MANUFACTURING_VALIDATION', 'NOT_CLINICAL_VALIDATION', 'NOT_FULL_DUAL_SPACE_FUSION',
    'REPRODUCIBILITY_NOT_VERIFIED', 'UNKNOWN_BOUNDARY'] }
  const before = JSON.stringify(payload)
  const summary = v.productScopeSummary(payload)
  assert.ok(summary.length >= 2 && summary.length <= 3)
  const text = summary.join(' ')
  for (const phrase of ['selected C3D8/C3D8R', 'Excluded regions do not participate',
    'Reproducibility', 'unrecognized']) assert.ok(text.includes(phrase), phrase)
  assert.equal(text.split('Full dual-space fusion has not been validated.').length - 1, 1)
  assert.doesNotMatch(text, /This record does not establish|NOT_FULL_DUAL_SPACE_FUSION/)
  const full = v.productNonClaims(payload).map(claim => claim.explanation).join(' ')
  for (const phrase of ['full bone model', "excluded elements is outside this result's scope", 'mechanical validation of shell regions',
    'finite-element', 'manufacturing', 'clinical', 'out-of-distribution', 'Full dual-space fusion has not been validated']) {
    assert.ok(full.includes(phrase), phrase)
  }
  assert.match(v.productScopeSummary({}).join(' '), /not recorded/)
  assert.equal(JSON.stringify(payload), before)
})

test('scope summary does not invent computation or validation for failed, missing or unfamiliar records', async () => {
  const v = await loadProductModule()
  for (const payload of [{}, { view_state: 'failed', progress: { job_status: 'FAILED' } },
    { view_state: 'invented', non_claims: ['UNRECOGNIZED_LIMIT'] }]) {
    const before = JSON.stringify(payload)
    const text = v.productScopeSummary(payload).join(' ')
    assert.match(text, /Computation scope is not recorded/)
    assert.match(text, /Scientific validation status is not recorded/)
    assert.doesNotMatch(text, /selected C3D8|Result generated|checks passed|validation passed|computation completed/i)
    if (payload.non_claims) assert.match(text, /unrecognized/)
    assert.equal(JSON.stringify(payload), before)
  }
})

test('P13 package assessment distinguishes unevaluated, passed, failed, absent and inapplicable evidence', async () => {
  const v = await loadProductModule()
  for (const [evidence, expectedState, expectedMeaning, attention] of [
    [{ status: 'not_available_legacy_package', integrity_status: 'NOT_EVALUATED' }, /not evaluated/, /does not mean geometry generation failed/, false],
    [{ status: 'verified', integrity_status: 'PASS', package_contract: {} }, /package checks passed/, /does not establish.*validation/, false],
    [{ status: 'failed', integrity_status: 'FAIL' }, /package checks failed/, /not a geometry-generation conclusion/, true],
    [{ integrity_status: 'NOT_APPLICABLE' }, /not applicable/, /explicitly records/, false],
    [undefined, /not recorded/, /No.*status is recorded/, false],
    [{ integrity_status: 'PASS' }, /needs review/, /do not establish/, true],
    [{ status: 'verified', integrity_status: 'NOT_EVALUATED' }, /needs review/, /do not establish/, true],
    [{ status: 'verified', integrity_status: 'FAIL' }, /needs review/, /do not establish/, true],
    [{ integrity_status: 'NEW_STATE' }, /needs review/, /do not establish/, true]
  ]) {
    const payload = { result: { p13_evidence: evidence } }
    const before = JSON.stringify(payload)
    const assessment = v.productPackageAssessment(payload)
    assert.match(assessment.state, expectedState)
    assert.match(assessment.meaning, expectedMeaning)
    assert.equal(assessment.attention, attention)
    assert.equal(JSON.stringify(payload), before)
  }
})

test('record summary keeps six technical fields and separates historical producer from reader with zero and missing values', async () => {
  const v = await loadProductModule()
  const payload = { job_id: 'job-1234567890ABCDEF12345678', revision: 0, schema_version: 'viewer_v1',
    updated_at: '2099-01-01', target: { source: { sample_id: 0 } },
    mesh: { original_filename: 'selected.inp', node_count: 0, element_count: 0 },
    candidates: [{ candidate_id: 'cell-0', rank: 0, selected: true }],
    result: { compatibility: { producer: { generation: 'v18' }, reader: { generation: 'v28' } } } }
  const before = JSON.stringify(payload)
  const fields = Object.fromEntries(v.productRecordSummary(payload))
  assert.deepEqual(Object.keys(fields), ['Job', 'Record revision', 'Viewer record schema',
    'Historical producer generation', 'Current reader generation', 'Record updated time (not result-ready time)'])
  assert.equal(fields.Job, payload.job_id)
  assert.equal(fields['Record revision'], 0)
  assert.equal(fields['Viewer record schema'], 'viewer_v1')
  assert.equal(fields['Historical producer generation'], 'v18')
  assert.equal(fields['Current reader generation'], 'v28')
  assert.equal(fields['Record updated time (not result-ready time)'], '2099-01-01')
  const missing = Object.fromEntries(v.productRecordSummary({}))
  assert.deepEqual(Object.keys(missing), Object.keys(fields))
  assert.ok(Object.values(missing).every(value => value === 'Not recorded'))
  assert.equal(JSON.stringify(payload), before)
})

const diagonalSliver = [0, 0, 0, 1000, 1002, 1000, 1002, 1000, 999]

function applyCameraState(camera, state) {
  for (const field of ['left', 'right', 'top', 'bottom', 'near', 'far']) camera[field] = state[field]
  camera.zoom = state.zoom ?? 1
  camera.position.fromArray(state.position)
  if (state.quaternion) camera.quaternion.fromArray(state.quaternion)
  else camera.lookAt(new THREE.Vector3(...state.target))
  camera.updateProjectionMatrix()
  camera.updateMatrixWorld(true)
}

function assertActualVertexProjection(positions, camera) {
  // This oracle projects source vertices through Three's matrices, not fit metadata or AABB corners.
  const projected = []
  for (let index = 0; index < positions.length; index += 3) {
    const point = new THREE.Vector3(positions[index], positions[index + 1], positions[index + 2]).project(camera)
    for (const axis of ['x', 'y', 'z']) {
      assert.ok(Number.isFinite(point[axis]) && Math.abs(point[axis]) <= 1 + 1e-5, `${axis}: ${point[axis]}`)
    }
    projected.push(point)
  }
  const occupied = []
  for (const axis of ['x', 'y']) {
    const min = Math.min(...projected.map(point => point[axis]))
    const max = Math.max(...projected.map(point => point[axis]))
    assert.ok(Math.abs((min + max) / 2) < 1e-5, `${axis} center: ${(min + max) / 2}`)
    occupied.push((max - min) / 2)
  }
  assert.ok(occupied.some(value => value >= 0.70 && value <= 0.85), `actual vertex occupancy: ${occupied}`)
  assert.ok(Math.abs(Math.max(...occupied) - 0.8) < 1e-5, `80 percent baseline: ${occupied}`)
}

function projectionGeometry(positions) {
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(new Float64Array(positions), 3))
  const normals = new Float64Array(positions.length)
  for (let index = 2; index < normals.length; index += 3) normals[index] = 1
  geometry.setAttribute('normal', new THREE.BufferAttribute(normals, 3))
  return geometry
}

test('product camera fits real nonbox vertices, offset and tiny planar geometry through independent Three projection', async () => {
  const v = await loadProductModule()
  const box = []
  for (const x of [-1, 1]) for (const y of [-1, 1]) for (const z of [-1, 1]) box.push(x, y, z)
  box.push(...box.slice(0, 3))
  const translatedSliver = diagonalSliver.map((value, index) => value + [1e9, -3e9, 2e9][index % 3])
  const fixtures = [box, diagonalSliver, translatedSliver,
    [120, -7, 93, 132, 4, 94, 125, -2, 94],
    [0, 0, 0, 1, 0, 0, 0, 1, 0],
    [0, 0, 0, 1, 0, -1, 2, 0, -2],
    [-1e-12, 0, 0, 1e-12, 2e-12, 0, 0, 0, 1e-12]]
  for (const vertices of fixtures) {
    const geometry = projectionGeometry(vertices)
    const original = Array.from(geometry.attributes.position.array)
    const { bounds } = v.validateProductGeometry(geometry)
    for (const [width, height] of [[806.65625, 380], [841, 720], [840, 720], [390, 844], [12, 900], [2000, 10]]) {
      const fit = v.computeProductCameraFit(bounds, width, height)
      const camera = new THREE.OrthographicCamera()
      applyCameraState(camera, fit)
      assertActualVertexProjection(original, camera)
    }
    assert.deepEqual(Array.from(geometry.attributes.position.array), original, 'fitting must not alter geometry')
    geometry.dispose()
  }
})

test('geometry validation caches a constant-size projection and fitting never reads source vertices again', async () => {
  const v = await loadProductModule()
  let readable = true
  const source = new Proxy(diagonalSliver, { get(target, key) {
    assert.equal(readable, true, 'resize must not read the source vertices')
    return Reflect.get(target, key)
  } })
  const bounds = v.productGeometryBounds(source)
  readable = false
  assert.deepEqual(Object.keys(bounds).sort(), ['max', 'min', 'projection'])
  assert.deepEqual(Object.keys(bounds.projection).sort(), ['max', 'min', 'origin'])
  assert.equal(bounds.min.length + bounds.max.length + bounds.projection.origin.length
    + bounds.projection.min.length + bounds.projection.max.length, 13)
  for (const [width, height] of [[806.65625, 380], [390, 844], [806.65625, 380]]) {
    const camera = new THREE.OrthographicCamera()
    applyCameraState(camera, v.computeProductCameraFit(bounds, width, height))
    assertActualVertexProjection(diagonalSliver, camera)
  }
})

test('camera delays unavailable canvas and rejects nonfinite or completely degenerate projected geometry', async () => {
  const v = await loadProductModule()
  const bounds = v.productGeometryBounds(diagonalSliver)
  for (const [width, height] of [[0, 320], [390, 0], [Infinity, 320], [390, NaN]]) {
    assert.equal(v.computeProductCameraFit(bounds, width, height), null)
  }
  for (const positions of [[], [1, 1, 1, 1, 1, 1, 1, 1, 1], [0, 0, 0, 1, 1, 1, 2, 2, 2]]) {
    assert.throws(() => v.productGeometryBounds(positions), error => error.code === 'EMPTY_GEOMETRY')
  }
  for (const positions of [[0, 0, Infinity, 1, 1, 1, 2, 2, 2],
    [-Number.MAX_VALUE, 0, 0, Number.MAX_VALUE, 1, 0, 0, 0, 1]]) {
    assert.throws(() => v.productGeometryBounds(positions), error => error.code === 'NONFINITE_GEOMETRY')
  }
  for (const invalid of [{ ...bounds, min: [0, NaN, 0] }, { ...bounds, min: [2000, 0, 0] },
    { ...bounds, projection: { ...bounds.projection, max: [Infinity, 1] } },
    { min: bounds.min, max: bounds.max }]) {
    assert.throws(() => v.computeProductCameraFit(invalid, 400, 300), error => error.code === 'NONFINITE_GEOMETRY')
  }
  const geometry = projectionGeometry([0, 0, 0, 1, 1, 1, 2, 2, 2])
  assert.throws(() => v.validateProductGeometry(geometry), error => error.code === 'EMPTY_GEOMETRY')
  geometry.attributes.position.array[0] = NaN
  assert.throws(() => v.validateProductGeometry(geometry), error => error.code === 'NONFINITE_GEOMETRY')
  geometry.dispose()
})

test('resizing preserves a real user camera pose and zoom while restore fits the actual vertices again', async () => {
  const v = await loadProductModule()
  const bounds = v.productGeometryBounds(diagonalSliver)
  const firstFit = v.computeProductCameraFit(bounds, 806.65625, 380)
  const camera = new THREE.OrthographicCamera()
  applyCameraState(camera, firstFit)
  const userTarget = new THREE.Vector3(...firstFit.target).add(new THREE.Vector3(1, -2, 3))
  camera.position.copy(userTarget).add(new THREE.Vector3(15, 25, 35))
  camera.lookAt(userTarget)
  camera.zoom = 2.3
  camera.updateProjectionMatrix()
  camera.updateMatrixWorld(true)
  const pose = { position: camera.position.toArray(), quaternion: camera.quaternion.toArray(), target: userTarget.toArray(), zoom: camera.zoom }
  const fit = v.computeProductCameraFit(bounds, 390, 844)
  const resized = v.productCameraState(fit, pose, false)
  for (const key of ['position', 'quaternion', 'target', 'zoom']) assert.deepEqual(resized[key], pose[key])
  applyCameraState(camera, resized)
  assert.deepEqual(camera.position.toArray(), pose.position)
  assert.deepEqual(camera.quaternion.toArray(), pose.quaternion)
  assert.equal(camera.zoom, pose.zoom)
  const restored = v.productCameraState(fit, resized, true)
  assert.deepEqual(restored.position, fit.position)
  assert.deepEqual(restored.target, fit.target)
  assert.equal(restored.zoom, 1)
  assert.equal(restored.quaternion, null)
  applyCameraState(camera, restored)
  assertActualVertexProjection(diagonalSliver, camera)
})
