/* eslint-disable react/prop-types */
import { Component, Suspense, useCallback, useEffect, useState } from 'react'
import { Canvas, useThree } from '@react-three/fiber'
import { Html } from '@react-three/drei'
import LatticeViewer from '../LatticeViewer'
import { fullProductApiUrl, productJobFileUrl, candidatePreviewUrl } from '../api/productViewerApi'
import { displayBasename, inputName } from '../utils/productResultExports.js'
import CandidateCellViewer from '../visualization/CandidateCellViewer'
import HexMeshViewer from '../visualization/HexMeshViewer'
import ProductResultWorkspace from './ProductResultWorkspace'
import { candidateEligibility, candidateMetric, formatProductNumber, getProductPageState, productScopeSummary, productPackageAssessment, productReadyTime, productTargetComponents, productTaskPresentation, productPreviewState, reduceProductPreview, productPreviewDiagnostic, productStlError } from '../utils/productJobView'

const shellStyle = {
  width: '100%', height: '100%', overflow: 'auto', padding: 18,
  boxSizing: 'border-box', background: '#eef1f4'
}

function StandardCellImage({payload,candidate}) {
  const [failed,setFailed]=useState(false)
  let url
  try{url=fullProductApiUrl(candidatePreviewUrl(payload,candidate))}catch{/* missing historical preview identity */}
  return url&&!failed?<img style={{width:'100%',maxHeight:300,objectFit:'contain'}} src={url} alt={`Standard unit cell, rank ${candidate.rank}`} onError={()=>setFailed(true)}/>:<p>Standard unit-cell preview unavailable.</p>
}

function Candidates({ payload, canPreview }) {
  return (
    <div style={shellStyle}>
      <h2>候选晶格（只读）</h2>
      <p>下列候选按记录中的排名排列。</p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(220px, 1fr))', gap: 12 }}>
        {[...payload.candidates].sort((a,b)=>(a.rank??99)-(b.rank??99)).map((candidate) => (
          <section className="section-card" key={candidate.candidate_id}>
            <div className="section-card-header">
              <h3>Rank {candidate.rank}</h3>
              <span className="status-pill">{candidate.confirmed ? 'confirmed' : candidate.recommended ? 'recommended · unconfirmed' : 'candidate'}</span>
            </div>
            {canPreview && <StandardCellImage payload={payload} candidate={candidate}/>}
            <details><summary>Auxiliary interactive 3D</summary><div style={{height:240}}><Canvas orthographic camera={{position:[3,3,3],zoom:120}}><CandidateCellViewer candidate={candidate}/></Canvas></div></details>
            <div className="section-card-body">Normalized diameter: {formatProductNumber(candidate.diameter)} (dimensionless)<br/>{candidateMetric(candidate,payload.ranking)}<br/>Relative density (dimensionless): {formatProductNumber(candidate.relative_density)}<br/>{candidateEligibility(candidate)}</div>
          </section>
        ))}
      </div>
      <ProductScopeNotice payload={payload} />
    </div>
  )
}

function Mesh({ payload, viewMode }) {
  const mesh = payload.mesh
  return (
    <div style={shellStyle}>
      <h2>输入网格（只读）</h2>
      <div className="key-value-grid">
        <span>{inputName(payload)} · {mesh?.element_type_original} · Units: {mesh?.units||MISSING_DETAIL}</span>
        <span>{mesh?.node_count} nodes · {mesh?.element_count} elements · precheck {mesh?.precheck_status}</span>
      </div>
      <div className={'section-card'} style={{ height: 'calc(100% - 130px)', minHeight: 360 }}>
        {mesh?.visualization?.status === 'available' ? (
          <Canvas orthographic camera={{ position: [10, 10, 10], zoom: 80 }}><HexMeshViewer meshPayload={mesh.visualization} viewMode={viewMode} /></Canvas>
        ) : <div className="viewer-empty-state"><strong>网格预览不可用</strong></div>}
      </div>
      <ProductScopeNotice payload={payload} />
    </div>
  )
}

function Progress({ payload }) {
  const task = productTaskPresentation(payload)
  const steps = {precheck:'Mesh check',adapter:'Geometry input',conformal:'Conformal generation',package:'Result package'}
  const statuses = {complete:'Complete',active:'Running',failed:'Failed',pending:'Pending'}
  return <div style={shellStyle}>
    <h2>Design progress</h2><p role="status">{task.title}.</p><p>{task.next}</p>
    {payload.view_state==='running'&&<ol>{(payload.progress?.steps||[]).filter(step=>Object.hasOwn(steps,step.key))
      .map(step=><li key={step.key}>{steps[step.key]} · {statuses[step.status]||'Status unavailable'}</li>)}</ol>}
    <ProductScopeNotice payload={payload} />
  </div>
}

const MISSING_DETAIL = 'Not provided in this record.'
const PREVIEW_FAILURE = 'The 3D preview is unavailable. The result record already read remains available.'

class ProductPreviewBoundary extends Component {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(error, info) {
    console.error('Product STL render failure', { name: error.name, message: error.message, stack: error.stack, componentStack: info.componentStack })
    this.props.onFailure(productStlError(/webgl|context/i.test(error.message) ? 'WEBGL_UNAVAILABLE' : 'RENDER_ERROR', 'render'))
  }
  render() { return this.state.failed ? <p className="product-preview-status">{PREVIEW_FAILURE}</p> : this.props.children }
}

export function PreviewUnavailable({ pngUrl, stale, downloadsEnabled, jobId }) {
  return <ProductPreviewFailure message={PREVIEW_FAILURE} pngUrl={pngUrl} stale={stale} downloadsEnabled={downloadsEnabled} jobId={jobId} />
}

function PreviewCanvasEvents({ onFailure }) {
  const { gl } = useThree()
  useEffect(() => {
    const lost = event => { event.preventDefault(); onFailure(productStlError('CONTEXT_LOST', 'render')) }
    gl.domElement.addEventListener('webglcontextlost', lost)
    return () => gl.domElement.removeEventListener('webglcontextlost', lost)
  }, [gl, onFailure])
  return null
}

let previewSequence = 0

export function ProductPreviewFailure({ message, pngUrl, stale, downloadsEnabled, jobId }) {
  const [imageFailed, setImageFailed] = useState(false)
  let imageUrl = null
  if (!stale && downloadsEnabled) {
    try { if (pngUrl === productJobFileUrl(jobId, 'png')) imageUrl = fullProductApiUrl(pngUrl) } catch { /* retain the original preview diagnostic */ }
  }
  return <>
    <p className="product-preview-status" role="status">{message}</p>
    {imageUrl && <figure style={{ margin: '0 12px 12px', textAlign: 'center' }}>
      <figcaption className="product-secondary">Static preview (PNG) · 3D preview not ready</figcaption>
      {imageFailed ? <p className="product-preview-status">The static preview is unavailable. Recorded result information and file links remain available.</p>
        : <img src={imageUrl} alt="Static PNG preview of the generated beam mesh" onError={() => setImageFailed(true)} style={{ maxWidth: '100%', maxHeight: 300, objectFit: 'contain' }} />}
    </figure>}
  </>
}

function ProductStlPreview({ stlUrl, pngUrl, stale, downloadsEnabled, jobId, packageId }) {
  const [resetViewToken, setResetViewToken] = useState(0)
  const [requestKey] = useState(() => stlUrl ? `${stlUrl}|${++previewSequence}` : null)
  const [preview, setPreview] = useState(() => productPreviewState(requestKey))
  const onPreviewEvent = useCallback(event => {
    const diagnostic = event.type === 'error' ? productPreviewDiagnostic(event.error, { job_id: jobId, package_id: packageId, request_key: requestKey, resource: stlUrl }) : null
    setPreview(previous => reduceProductPreview(previous, { ...event, diagnostic }))
    if (diagnostic) console.error('Product STL preview diagnostic', diagnostic, event.timings)
    else if (event.type === 'frame') console.info('Product STL frame submitted', { job_id: jobId, package_id: packageId, request_key: requestKey, ...event.timings })
  }, [jobId, packageId, requestKey, stlUrl])
  const onFailure = useCallback(error => onPreviewEvent({ type: 'error', error, request_key: requestKey }), [onPreviewEvent, requestKey])
  const loaded = preview.status === 'ready'
  const failed = preview.status.endsWith('_error')
  return <>
    <div className="product-canvas product-stl-canvas">
      {!stlUrl ? <p className="product-preview-status">No STL preview is provided for this result.</p>
        : stale && !loaded ? <p className="product-preview-status">This is an earlier snapshot; its geometry preview has not been loaded.</p>
          : failed ? <ProductPreviewFailure key={pngUrl} message={preview.diagnostic.message} pngUrl={pngUrl} stale={stale} downloadsEnabled={downloadsEnabled} jobId={jobId} />
            : <ProductPreviewBoundary onFailure={onFailure}>
              <Suspense fallback={<p className="product-preview-status">Loading geometry preview…</p>}>
                <Canvas orthographic camera={{ manual: true, position: [10, 10, 10], zoom: 1 }} fallback={<PreviewUnavailable pngUrl={pngUrl} stale={stale} downloadsEnabled={downloadsEnabled} jobId={jobId} />}>
                  <Suspense fallback={<Html center>Loading geometry preview…</Html>}>
                    <LatticeViewer stlUrl={fullProductApiUrl(stlUrl)} viewMode="iso" cameraMode="product-fit" resetViewToken={resetViewToken} requestKey={requestKey} onPreviewEvent={onPreviewEvent} />
                    <PreviewCanvasEvents onFailure={onFailure} />
                  </Suspense>
                </Canvas>
              </Suspense>
            </ProductPreviewBoundary>}
    </div>
    <div className="product-model-actions"><span className="product-secondary">{loaded ? 'Drag to rotate · Scroll to zoom' : preview.status === 'loading' && !stale ? 'Loading 3D preview…' : 'Geometry preview not ready'}</span><button type="button" disabled={!loaded} onClick={() => setResetViewToken(value => value + 1)}>Reset view</button></div>
  </>
}

function ProductScopeNotice({ payload }) {
  return <p className="product-scope" aria-label="Result scope">{getProductPageState(payload) === 'result' && 'For geometry inspection and further engineering analysis. '}{productScopeSummary(payload).join(' ')}</p>
}

function ProductAssessmentWarning({ payload }) {
  const assessment = productPackageAssessment(payload)
  return assessment.attention && <section className="product-read-error" role="alert" aria-label="Package assessment warning"><strong>{assessment.state}.</strong><p>{assessment.meaning}</p></section>
}

function ProductInputs({ payload, selected }) {
  const target = payload.target || {}
  const mesh = payload.mesh
  const values = productTargetComponents(target)
  const material = target.base_material
  const source = target.source || {}
  const sourceName = displayBasename(source.source_name) || MISSING_DETAIL
  const modulus = formatProductNumber(material?.elastic_modulus_gpa)
  const selectedName = selected ? Number.isInteger(selected.rank) && selected.rank > 0 ? `Rank ${selected.rank} of ${payload.candidates.length}` : 'Recorded (rank not recorded)' : 'Not selected'
  return <>
    <div className="product-input-summary">
      <div><p>Target source: {sourceName}{source.sample_id != null ? ` · Sample ${source.sample_id}` : ''}</p><p className="product-secondary">Base material: {material?.name || MISSING_DETAIL} · Elastic modulus: {modulus === 'Not recorded' ? modulus : `${modulus} GPa`}</p></div>
      <div><p>{payload.selection?.confirmation_status==='CONFIRMED'?'Confirmed candidate':payload.selection?.confirmation_status==='LEGACY_NOT_RECORDED'?'Historical recorded candidate (confirmation not recorded)':'Recommended candidate (not confirmed)'}: {selectedName}{selected?.recommended && <span className="product-secondary"> · Matches the system recommendation</span>}</p></div>
      <div><p>Input mesh: {mesh ? inputName(payload) : 'Not bound'}</p><p className="product-secondary">{mesh ? `${mesh.element_type_original || 'Not recorded'} · ${mesh.node_count ?? 'Not recorded'} nodes · ${mesh.element_count ?? 'Not recorded'} hexahedral elements · Units: ${mesh.units || 'Not recorded'}` : MISSING_DETAIL}</p></div>
    </div>
    <div className="product-target-desktop"><p className="product-secondary">Input target: nine stiffness components (GPa, selected 9D view)</p><dl className="product-target-values">{values.map(({ name, display }) => <div key={name}><dt>{name}</dt><dd>= {display}</dd></div>)}</dl><p className="product-secondary">These are input targets, not measured properties of the generated structure or a complete stiffness matrix.</p></div>
    <details className="product-target-mobile"><summary>View nine target components (GPa)</summary><p>Input target: nine stiffness components (GPa, selected 9D view)</p><table><thead><tr><th scope="col">Component</th><th scope="col">Target (GPa)</th></tr></thead><tbody>{values.map(({ name, display }) => <tr key={name}><th scope="row">{name}</th><td>{display}</td></tr>)}</tbody></table><p className="product-secondary">These are input targets, not measured properties of the generated structure or a complete stiffness matrix.</p></details>
  </>
}

function ProductPage({ payload, downloadsEnabled, stale }) {
  const state = getProductPageState(payload)
  const task = productTaskPresentation(payload)
  const selected = payload.candidates?.find(candidate => candidate.selected)
  return <div className="product-job-panel">
    {state === 'result' ? null
      : state === 'unknown' ? <p className="product-status" role="status">Task status needs review; result actions are unavailable. Use “Refresh Job status” on the chat page to check the current record.</p>
        : <section className="product-status" role="status"><p>{task.title}.</p><p>{task.next}</p></section>}
    <ProductAssessmentWarning payload={payload} />
    {state === 'result' ? <ProductResultWorkspace key={[payload.job_id,payload.revision,payload.result.package_id,payload.result.scene?.triangles].join('|')} payload={payload} downloadsEnabled={downloadsEnabled} stale={stale} />
      : <><ProductInputs payload={payload} selected={selected} /><ProductScopeNotice payload={payload} /></>}
  </div>
}

export default function ProductJobPanel({ payload, viewMode, layoutMode = 'embedded', downloadsEnabled = true, stale = false }) {
  if (layoutMode === 'product-page') return <ProductPage payload={payload} downloadsEnabled={downloadsEnabled} stale={stale} />
  if (payload.view_state === 'candidate') return <Candidates payload={payload} canPreview={downloadsEnabled&&!stale} />
  if (payload.view_state === 'mesh') return <Mesh payload={payload} viewMode={viewMode} />
  if (payload.view_state === 'running') return <Progress payload={payload} />
  if (payload.view_state === 'failed') return <Progress payload={payload} />
  if (getProductPageState(payload) === 'result') return <div style={shellStyle}><ProductAssessmentWarning payload={payload} /><ProductResultWorkspace key={[payload.job_id,payload.revision,payload.result.package_id,payload.result.scene?.triangles].join('|')} payload={payload} downloadsEnabled={downloadsEnabled} stale={stale} initialView={viewMode} /></div>
  return <div className="viewer-empty-state"><strong>未知 Product Job 状态</strong></div>
}
