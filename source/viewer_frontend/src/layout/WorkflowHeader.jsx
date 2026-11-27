/* eslint-disable react/prop-types */
import { createContext, useContext } from 'react'
import { normalizeDualSpaceStatus } from '../utils/productDualSpace'

const ViewerEvidenceContext = createContext(null)

export function ViewerEvidenceProvider({ value, children }) {
  return <ViewerEvidenceContext.Provider value={value}>{children}</ViewerEvidenceContext.Provider>
}

export default function WorkflowHeader() {
  const payload = useContext(ViewerEvidenceContext)
  const status = normalizeDualSpaceStatus(payload)
  const spaces = [
    ['Mechanical Space', status.mechanicalSpace.status],
    ['Geometric Space', status.geometricSpace.status],
    ['Fusion Bridge', status.fusionBridge.status],
    ['Output and Visualization', status.outputAndVisualization.status]
  ]
  return (
    <header className="workflow-header">
      <div className="workflow-title-block">
        <span className="module-eyebrow">V page · readonly product evidence</span>
        <h1>BoneMetGPT V1.0</h1>
        <p>LLM-assisted dual-space generative design for bone implants</p>
      </div>
      <div className="evidence-space-grid" aria-label="dual-space evidence status">
        {spaces.map(([label, value]) => (
          <div className="evidence-space-card" key={label}>
            <div className="evidence-space-topline"><span>{label}</span><strong>{value}</strong></div>
          </div>
        ))}
      </div>
    </header>
  )
}
