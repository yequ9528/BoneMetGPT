import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  DUAL_SPACE_MAINLINE,
  normalizeDualSpaceStatus
} from '../src/utils/productDualSpace.js'


test('normalizes all four layers and keeps full fusion unvalidated', () => {
  const normalized = normalizeDualSpaceStatus({
    view_state: 'result',
    result: { package_id: 'job-1234567890ABCDEF12345678-attempt-0001' },
    dual_space_status: {
      mainline: DUAL_SPACE_MAINLINE,
      mechanical_space: { status: 'CANDIDATES_RECORDED' },
      geometric_space: { status: 'GEOMETRIC_OUTPUT_RECORDED' },
      fusion_bridge: {
        status: 'TRACEABLE_BRIDGE_RECORDED',
        full_fusion_validation: 'PASS'
      },
      output_and_visualization: { status: 'READONLY_RESULT_AVAILABLE' }
    }
  })
  assert.equal(normalized.fusionBridge.status, 'TRACEABLE_BRIDGE_RECORDED')
  assert.equal(normalized.fusionBridge.fullFusionValidation, 'NOT_VALIDATED')
  assert.equal(normalized.outputAndVisualization.status, 'READONLY_RESULT_AVAILABLE')
  assert.deepEqual(normalized.nonClaims, ['NOT_FULL_DUAL_SPACE_FUSION'])
})

test('unknown states and traceable claim without verified result fail closed', () => {
  const normalized = normalizeDualSpaceStatus({
    view_state: 'running',
    dual_space_status: {
      mechanical_space: { status: 'FORGED' },
      fusion_bridge: { status: 'TRACEABLE_BRIDGE_RECORDED' }
    }
  })
  assert.equal(normalized.mechanicalSpace.status, 'NOT_YET_RECORDED')
  assert.equal(normalized.fusionBridge.status, 'NOT_YET_RECORDED')
})

test('ordinary product pages omit raw four-layer records while the normalization contract stays separate', () => {
  const panel = readFileSync(new URL('../src/panels/ProductJobPanel.jsx', import.meta.url), 'utf8')
  const app = readFileSync(new URL('../src/ProductApp.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(app, /productProgressSummary\(payload\)/)
  assert.doesNotMatch(panel, /ProductEvidence|RawEvidence|Technical details|Original stages and events/)
  assert.doesNotMatch(panel, /payload\.dual_space_status|Four evidence layers|Validation scope and full statements/)
  assert.doesNotMatch(app, /<WorkflowHeader|<DualSpaceWorkflowPanel/)
})
