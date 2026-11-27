export const DUAL_SPACE_MAINLINE = 'LLM-assisted dual-space generative design for bone implants'

const ALLOWED = {
  mechanical: new Set(['NOT_YET_RECORDED', 'TARGET_RECORDED', 'CANDIDATES_RECORDED']),
  geometric: new Set(['NOT_YET_RECORDED', 'SOURCE_MESH_RECORDED', 'GEOMETRIC_OUTPUT_RECORDED']),
  fusion: new Set(['NOT_YET_RECORDED', 'AWAITING_EXPLICIT_AUTHORIZATION', 'TRACEABLE_BRIDGE_RECORDED']),
  output: new Set(['NOT_YET_RECORDED', 'READONLY_JOB_STATE_AVAILABLE', 'READONLY_RESULT_AVAILABLE'])
}

function accepted(value, allowed) {
  return allowed.has(value) ? value : 'NOT_YET_RECORDED'
}

export function normalizeDualSpaceStatus(payload) {
  const source = payload?.dual_space_status || {}
  let fusion = accepted(source.fusion_bridge?.status, ALLOWED.fusion)
  const verifiedResult = payload?.view_state === 'result' && Boolean(payload?.result?.package_id)
  if (fusion === 'TRACEABLE_BRIDGE_RECORDED' && !verifiedResult) fusion = 'NOT_YET_RECORDED'
  return {
    mainline: DUAL_SPACE_MAINLINE,
    mechanicalSpace: { status: accepted(source.mechanical_space?.status, ALLOWED.mechanical) },
    geometricSpace: { status: accepted(source.geometric_space?.status, ALLOWED.geometric) },
    fusionBridge: { status: fusion, fullFusionValidation: 'NOT_VALIDATED' },
    outputAndVisualization: { status: accepted(source.output_and_visualization?.status, ALLOWED.output) },
    nonClaims: ['NOT_FULL_DUAL_SPACE_FUSION']
  }
}
