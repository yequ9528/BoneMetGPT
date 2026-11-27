/* eslint-disable react/prop-types */
import SectionCard from '../components/SectionCard'
import { normalizeDualSpaceStatus } from '../utils/productDualSpace'

export default function DualSpaceWorkflowPanel({ structureInfo }) {
  const status = normalizeDualSpaceStatus(structureInfo)
  const rows = [
    { title: 'Mechanical Space', value: status.mechanicalSpace.status, steps: ['target', 'three candidates', 'recommendation/selection'] },
    { title: 'Geometric Space', value: status.geometricSpace.status, steps: ['C3D8/C3D8R', 'B31', 'STL/PNG'] },
    { title: 'Fusion Bridge', value: status.fusionBridge.status, steps: ['selected candidate', 'Adapter', 'conformal package'] },
    { title: 'Output and Visualization', value: status.outputAndVisualization.status, steps: ['exact Job', 'readonly files', 'evidence'] }
  ]
  return (
    <div className="context-stack">
      <SectionCard title="Dual-space Workflow" subtitle={status.mainline} status="readonly evidence">
        <div className="workflow-route-stack">
          {rows.map((row) => (
            <div className="workflow-route" key={row.title}>
              <div className="route-title-row"><strong>{row.title}</strong><span className="status-pill">{row.value}</span></div>
              <div className="route-steps">{row.steps.map((step) => <span key={step}>{step}</span>)}</div>
            </div>
          ))}
        </div>
      </SectionCard>
      <SectionCard title="Claim boundary" status="V1 research demonstration">
        <div className="boundary-list">
          <span>full_fusion_validation={status.fusionBridge.fullFusionValidation}</span>
          <span>NOT_FULL_DUAL_SPACE_FUSION</span>
          <span>V page is readonly and unknown states fail closed.</span>
        </div>
      </SectionCard>
    </div>
  )
}
