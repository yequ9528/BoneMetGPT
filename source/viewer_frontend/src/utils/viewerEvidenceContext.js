const PRODUCT_SCHEMA = 'latticegpt_product_viewer_state_v1'
const PRODUCT_STATES = new Set(['candidate', 'mesh', 'running', 'result', 'failed'])

function availability(overrides = {}) {
  return {
    sourceMesh: false,
    result: false,
    b31: false,
    stl: false,
    png: false,
    downloads: false,
    ...overrides
  }
}

function emptyContext() {
  return {
    payloadKind: 'empty',
    viewState: 'unknown',
    identity: {},
    target: null,
    candidates: [],
    selection: {},
    mesh: null,
    progress: {},
    result: null,
    mechanical: null,
    traceability: null,
    downloads: {},
    nonClaims: [],
    availability: availability()
  }
}

function hasSourceMesh(mesh) {
  return Boolean(
    mesh
    && (mesh.status === 'available' || mesh.visualization?.status === 'available')
  )
}

function resultAvailability(result, downloads, sourceMeshAvailable) {
  const b31 = Boolean(result?.core_output && (result.core_output.path || downloads.b31))
  const stl = Boolean(result?.stl_url || result?.visualization?.stl_url || downloads.stl)
  const png = Boolean(result?.preview_url || result?.visualization?.png_url || downloads.png)
  return availability({
    sourceMesh: sourceMeshAvailable,
    result: true,
    b31,
    stl,
    png,
    downloads: Object.values(downloads).some(Boolean)
  })
}

function projectProductContext(structureInfo) {
  const viewState = PRODUCT_STATES.has(structureInfo.view_state)
    ? structureInfo.view_state
    : 'unknown'
  const exposesWorkflowEvidence = ['candidate', 'mesh', 'running', 'result'].includes(viewState)
  const exposesMesh = ['mesh', 'running', 'result'].includes(viewState)
  const sourceMeshAvailable = exposesMesh && hasSourceMesh(structureInfo.mesh)
  const result = viewState === 'result'
    && structureInfo.result
    && structureInfo.result.package_id
    ? structureInfo.result
    : null
  const downloads = result?.downloads || {}

  return {
    payloadKind: 'product',
    viewState,
    identity: {
      jobId: structureInfo.job_id,
      packageId: result?.package_id,
      revision: structureInfo.revision,
      schemaVersion: structureInfo.schema_version,
      workflowDisposition: structureInfo.workflow_disposition
    },
    target: exposesWorkflowEvidence ? structureInfo.target || null : null,
    candidates: exposesWorkflowEvidence && Array.isArray(structureInfo.candidates)
      ? structureInfo.candidates
      : [],
    selection: exposesWorkflowEvidence ? structureInfo.selection || {} : {},
    mesh: exposesMesh ? structureInfo.mesh || null : null,
    progress: structureInfo.progress || {},
    result,
    mechanical: result?.mechanical_space || null,
    traceability: result?.traceability || null,
    downloads,
    nonClaims: Array.isArray(structureInfo.non_claims) ? structureInfo.non_claims : [],
    availability: result
      ? resultAvailability(result, downloads, sourceMeshAvailable)
      : availability({ sourceMesh: sourceMeshAvailable })
  }
}

function projectLegacyContext(structureInfo) {
  const sourceVisualization = structureInfo.geometric_space?.source_mesh_visualization
  const mesh = structureInfo.input_mesh || sourceVisualization
    ? {
        ...(structureInfo.input_mesh || structureInfo.geometric_space?.source_mesh || {}),
        visualization: sourceVisualization || null
      }
    : null
  const downloads = structureInfo.downloads || {}
  const sourceMeshAvailable = Boolean(sourceVisualization?.status === 'available')
  const result = structureInfo.mode === 'conformal' ? structureInfo : null

  return {
    payloadKind: structureInfo.mode === 'conformal' ? 'conformal' : 'legacy',
    viewState: 'legacy',
    identity: {
      packageId: structureInfo.package_id,
      schemaVersion: structureInfo.schema_version
    },
    target: structureInfo.mechanical_space || null,
    candidates: [],
    selection: {},
    mesh,
    progress: {},
    result,
    mechanical: structureInfo.mechanical_space || null,
    traceability: structureInfo.traceability || null,
    downloads,
    nonClaims: Array.isArray(structureInfo.non_claims)
      ? structureInfo.non_claims
      : Array.isArray(structureInfo.warnings) ? structureInfo.warnings : [],
    availability: result
      ? resultAvailability(result, downloads, sourceMeshAvailable)
      : availability({ sourceMesh: sourceMeshAvailable })
  }
}

export function projectViewerEvidenceContext(structureInfo) {
  if (!structureInfo || typeof structureInfo !== 'object') return emptyContext()
  if (structureInfo.schema_version === PRODUCT_SCHEMA) {
    return projectProductContext(structureInfo)
  }
  return projectLegacyContext(structureInfo)
}
