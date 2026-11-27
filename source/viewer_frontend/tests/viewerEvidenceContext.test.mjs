import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test from 'node:test'


const contextModuleUrl = new URL('../src/utils/viewerEvidenceContext.js', import.meta.url)

async function loadContextModule() {
  assert.equal(
    existsSync(fileURLToPath(contextModuleUrl)),
    true,
    'viewerEvidenceContext.js is missing'
  )
  return import(contextModuleUrl)
}

function productPayload(viewState, overrides = {}) {
  return {
    schema_version: 'latticegpt_product_viewer_state_v1',
    job_id: 'job-1234567890ABCDEF12345678',
    revision: 10,
    workflow_disposition: viewState === 'failed' ? 'HOLD' : 'ACTIVE',
    view_state: viewState,
    target: { target_sha256: 'A'.repeat(64) },
    candidates: [{ candidate_id: 'candidate-1', rank: 1, selected: true }],
    selection: { selected_candidate_id: 'candidate-1' },
    mesh: null,
    progress: { current_stage: 'CANDIDATES_READY', failure: null },
    result: null,
    non_claims: ['NOT_FEA_VALIDATION'],
    ...overrides
  }
}

function availableMesh() {
  return {
    status: 'available',
    original_filename: 'implant.inp',
    sha256: 'B'.repeat(64),
    element_type_original: 'C3D8R',
    element_type_effective: 'C3D8_GEOMETRY_COMPATIBLE',
    node_count: 8,
    element_count: 1,
    precheck_status: 'passed',
    visualization: {
      status: 'available',
      representation: 'hex_wireframe',
      element_type: 'C3D8R',
      node_count: 8,
      element_count: 1
    }
  }
}

function verifiedResult() {
  const packageId = 'job-1234567890ABCDEF12345678-attempt-0001'
  return {
    mode: 'conformal',
    package_id: packageId,
    input_mesh: {
      path: 'input/source_c3d8.inp',
      element_type: 'C3D8R',
      node_count: 8,
      element_count: 1
    },
    core_output: {
      type: 'B31',
      path: 'core/conformal_b31.inp',
      node_count: 20,
      beam_count: 30,
      role: 'engineering output'
    },
    mechanical_space: {
      candidate_adapter_status: 'static_validated',
      evidence_status: 'identity_bound'
    },
    traceability: {
      candidate_id: 'candidate-1',
      adapter_status: 'static_validated'
    },
    visualization: {
      stl_url: `/api/conformal/packages/${packageId}/files/stl`,
      png_url: `/api/conformal/packages/${packageId}/files/png`,
      role: 'derived visualization'
    },
    downloads: {
      b31: `/api/conformal/packages/${packageId}/files/b31`,
      stl: `/api/conformal/packages/${packageId}/files/stl`,
      png: `/api/conformal/packages/${packageId}/files/png`,
      metadata: `/api/conformal/packages/${packageId}/files/metadata`
    }
  }
}

test('candidate state does not promote mesh or result evidence', async () => {
  const { projectViewerEvidenceContext } = await loadContextModule()
  const context = projectViewerEvidenceContext(productPayload('candidate'))

  assert.equal(context.payloadKind, 'product')
  assert.equal(context.availability.sourceMesh, false)
  assert.equal(context.availability.result, false)
  assert.deepEqual(context.downloads, {})
})

test('mesh state exposes only the bound source mesh', async () => {
  const { projectViewerEvidenceContext } = await loadContextModule()
  const context = projectViewerEvidenceContext(productPayload('mesh', { mesh: availableMesh() }))

  assert.equal(context.availability.sourceMesh, true)
  assert.equal(context.mesh.node_count, 8)
  assert.equal(context.availability.stl, false)
  assert.equal(context.availability.b31, false)
})

test('running state carries current progress without promoting result artifacts', async () => {
  const { projectViewerEvidenceContext } = await loadContextModule()
  const context = projectViewerEvidenceContext(productPayload('running', {
    mesh: availableMesh(),
    progress: { current_stage: 'CONFORMAL_RUNNING', attempt_count: 1, failure: null }
  }))

  assert.equal(context.progress.current_stage, 'CONFORMAL_RUNNING')
  assert.equal(context.availability.sourceMesh, true)
  assert.equal(context.availability.result, false)
  assert.deepEqual(context.downloads, {})
})

test('result state exposes one identity-bound package and its evidence', async () => {
  const { projectViewerEvidenceContext } = await loadContextModule()
  const context = projectViewerEvidenceContext(productPayload('result', {
    mesh: availableMesh(),
    result: verifiedResult()
  }))

  assert.equal(context.identity.packageId, 'job-1234567890ABCDEF12345678-attempt-0001')
  assert.equal(context.availability.result, true)
  assert.equal(context.availability.b31, true)
  assert.equal(context.availability.stl, true)
  assert.equal(context.availability.png, true)
  assert.equal(context.result.core_output.type, 'B31')
  assert.equal(context.mechanical.candidate_adapter_status, 'static_validated')
  assert.equal(context.traceability.candidate_id, 'candidate-1')
  assert.equal(
    context.downloads.stl,
    '/api/conformal/packages/job-1234567890ABCDEF12345678-attempt-0001/files/stl'
  )
  assert.deepEqual(context.nonClaims, ['NOT_FEA_VALIDATION'])
})

test('failed state suppresses stale result fields and downloads', async () => {
  const { projectViewerEvidenceContext } = await loadContextModule()
  const context = projectViewerEvidenceContext(productPayload('failed', {
    mesh: availableMesh(),
    progress: {
      current_stage: 'CONFORMAL_RUNNING',
      failure: { stage: 'CONFORMAL_RUNNING', message: 'safe failure' }
    },
    result: verifiedResult()
  }))

  assert.equal(context.progress.failure.stage, 'CONFORMAL_RUNNING')
  assert.equal(context.availability.sourceMesh, false)
  assert.equal(context.availability.result, false)
  assert.equal(context.result, null)
  assert.deepEqual(context.downloads, {})
})

test('unknown product state fails closed', async () => {
  const { projectViewerEvidenceContext } = await loadContextModule()
  const context = projectViewerEvidenceContext(productPayload('unexpected', {
    mesh: availableMesh(),
    result: verifiedResult()
  }))

  assert.equal(context.viewState, 'unknown')
  assert.equal(context.availability.sourceMesh, false)
  assert.equal(context.availability.result, false)
})

test('legacy conformal payload maps to the shared evidence contract', async () => {
  const { projectViewerEvidenceContext } = await loadContextModule()
  const legacy = verifiedResult()
  legacy.geometric_space = {
    source_mesh_visualization: availableMesh().visualization,
    b31_visualization: { status: 'future', representation: 'beam_graph' }
  }

  const context = projectViewerEvidenceContext(legacy)

  assert.equal(context.payloadKind, 'conformal')
  assert.equal(context.viewState, 'legacy')
  assert.equal(context.availability.sourceMesh, true)
  assert.equal(context.availability.b31, true)
  assert.equal(context.availability.stl, true)
  assert.equal(context.identity.packageId, legacy.package_id)
})

test('projection does not mutate its input payload', async () => {
  const { projectViewerEvidenceContext } = await loadContextModule()
  const payload = productPayload('result', { mesh: availableMesh(), result: verifiedResult() })
  const snapshot = structuredClone(payload)

  projectViewerEvidenceContext(payload)

  assert.deepEqual(payload, snapshot)
})
