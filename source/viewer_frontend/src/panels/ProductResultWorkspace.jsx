/* eslint-disable react/prop-types, react/no-unknown-property */
import { Component, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas } from '@react-three/fiber'
import { fetchOriginalMesh } from '../api/productViewerApi.js'
import { candidateEligibility, candidateMetric, formatProductNumber, productTargetComponents, resultWarnings } from '../utils/productJobView.js'
import { displayBasename, fileNameFor, inputName, materialDiameter, readResultFile, readCandidatePreview, referenceMatches, resultFileUrl, runExportPreparation, readScene, sceneUrl, startBlobDownload } from '../utils/productResultExports.js'
import { ProductResultScene } from '../visualization/ProductResultScene.jsx'
import './product-result.css'

class SceneBoundary extends Component {
  state={failed:false}
  static getDerivedStateFromError(){return {failed:true}}
  componentDidCatch(error){this.props.onFailure(error)}
  render(){return this.state.failed?this.props.fallback:this.props.children}
}

function StaticPreview({payload,enabled,error,onIdentityFailure}) {
  const [failed,setFailed]=useState(false)
  const [url,setUrl]=useState(null)
  useEffect(()=>{
    let active=true,objectUrl
    const controller=new AbortController()
    setUrl(null);setFailed(false)
    if(enabled && payload.result.scene?.poster){
      readScene(payload,'poster',controller.signal).then(bytes=>{
        if(active){objectUrl=URL.createObjectURL(new Blob([bytes],{type:'image/webp'}));setUrl(objectUrl)}
      }).catch(error=>{if(active){setFailed(true);if(error.code==='FILE_IDENTITY_MISMATCH')onIdentityFailure(error.message)}})
    }
    return ()=>{active=false;controller.abort();if(objectUrl)URL.revokeObjectURL(objectUrl)}
  },[enabled,payload,onIdentityFailure])
  const resourceLimited = ['STL_TOO_LARGE','STL_TRIANGLE_LIMIT','STL_RESOURCE_LIMIT'].includes(error?.code)
  return <div className="result-static" role="status"><p>{resourceLimited?'This result exceeds the interactive preview size limit. The saved files are unchanged.':'3D preview is unavailable'}</p><p>The fixed result image is shown when available.</p>
    {enabled&&url&&!failed&&<><img src={url} alt="Original preview saved for this result" onError={()=>setFailed(true)}/><span>Original result preview · fixed view</span></>}
    {failed&&<span>The original preview is unavailable. Verified model downloads remain available.</span>}</div>
}

export default function ProductResultWorkspace({payload,downloadsEnabled,stale,initialView='iso'}) {
  const selected=payload.candidates?.find(c=>c.selected), result=payload.result
  const identity=[payload.job_id,payload.revision,result.package_id,result.scene?.triangles||''].join('|')
  const currentIdentity=useRef(identity), alive=useRef(true), operation=useRef(null)
  currentIdentity.current=identity
  const [overlay,setOverlay]=useState(false),[reference,setReference]=useState(null),[referenceLoading,setReferenceLoading]=useState(false),[referenceError,setReferenceError]=useState('')
  const [opacity,setOpacity]=useState(.24),[clip,setClip]=useState({enabled:false,axis:'X',position:0}),[view,setView]=useState(initialView),[viewToken,setViewToken]=useState(0)
  const [info,setInfo]=useState(true),[work,setWork]=useState(null),[message,setMessage]=useState('')
  const [touchModel,setTouchModel]=useState(false)
  const [mainError,setMainError]=useState(null),[accessError,setAccessError]=useState(''),[frame,setFrame]=useState(null),[zoomImage,setZoomImage]=useState(null),[full,setFull]=useState(false)
  const [standardCell,setStandardCell]=useState(null),[standardError,setStandardError]=useState('')
  const viewport=useRef(null),dialog=useRef(null),zoomButton=useRef(null),fullButton=useRef(null),legend=useRef(null)
  const valid=downloadsEnabled&&!stale&&!accessError
  const authorized=useRef(valid)
  authorized.current=valid
  useEffect(()=>{if(!valid){operation.current?.controller.abort();setZoomImage(null)}},[valid])
  const permitted=key=>{try{return valid&&Boolean(resultFileUrl(payload,key))}catch{return false}}
  const stlUrl=valid&&result.scene?.triangles?sceneUrl(payload,'triangles'):null
  const labels=useMemo(()=>[
    'Generated conformal lattice',
    `Coordinate units: ${payload.mesh?.units||'Not recorded'}${overlay?' · pale overlay: input mesh':''}`,
    ...(overlay?[`Input: blue-grey = selected; red = excluded solids; grey = excluded shells/other`, 'Excluded elements are reference only; they were not used for generation']:[]),
    ...(clip.enabled?[`Clipping: ${clip.axis} direction ${clip.position}% · model files remain complete`]:[])
  ],[payload,overlay,clip])
  const onFrame=useCallback(version=>setFrame(version),[])
  const onFailure=useCallback(error=>{setMainError(error);setFrame(null);if(error.code==='FILE_IDENTITY_MISMATCH')setAccessError(error.message)},[])

  useEffect(()=>{
    setStandardCell(null);setStandardError('')
    if(!valid||!selected)return
    if(payload.candidate_set_sha256==null && selected.preview==null){
      setStandardError('Standard unit-cell preview unavailable: this record has no saved preview identity.')
      return
    }
    let active=true,url
    const controller=new AbortController()
    readCandidatePreview(payload,selected,{signal:controller.signal}).then(blob=>{
      if(!active)return
      url=URL.createObjectURL(blob);setStandardCell({blob,url,identity})
    }).catch(error=>{if(active&&error.name!=='AbortError'){
      if(error.code==='FILE_IDENTITY_MISMATCH')setAccessError(error.message)
      setStandardError('The selected unit-cell preview could not be verified. Refresh this result.')
    }})
    return()=>{active=false;controller.abort();if(url)URL.revokeObjectURL(url)}
  },[identity,valid,payload,selected])
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;operation.current?.controller.abort()}},[])
  useEffect(()=>{
    if(!overlay || reference || !valid)return
    const controller=new AbortController(), issued=identity
    let active=true
    setReferenceLoading(true);setReferenceError('')
    fetchOriginalMesh(payload.job_id,{signal:controller.signal}).then(next=>{
      if(!active||currentIdentity.current!==issued)return
      const ref=next.mesh?.original_visualization
      if(!referenceMatches(payload,ref)||next.result?.package_id!==result.package_id)throw Object.assign(new Error('The input reference does not match this result. Reopen the result.'),{code:'FILE_IDENTITY_MISMATCH'})
      setReference(ref)
      if(ref.status!=='available')setReferenceError('The input mesh overlay is unavailable.')
    }).catch(error=>{if(active&&error.name!=='AbortError'){
      setReferenceError(['FILE_IDENTITY_MISMATCH','PACKAGE_CONTRACT_INVALID','JOB_CONTRACT_INVALID'].includes(error.code)?error.message:'The input mesh overlay could not be loaded. Refresh to try again.')
      if(['FILE_IDENTITY_MISMATCH','PACKAGE_CONTRACT_INVALID','JOB_CONTRACT_INVALID'].includes(error.code))setAccessError(error.message)
    }}).finally(()=>{if(active)setReferenceLoading(false)})
    return()=>{active=false;controller.abort()}
  },[overlay,reference,valid,identity,payload,result.package_id])
  useEffect(()=>{
    const update=()=>{const isFull=document.fullscreenElement===viewport.current;setFull(isFull);if(!isFull)fullButton.current?.focus()}
    document.addEventListener('fullscreenchange',update);return()=>document.removeEventListener('fullscreenchange',update)
  },[])
  useEffect(()=>{
    if(zoomImage)dialog.current?.showModal()
    return()=>{if(zoomImage)URL.revokeObjectURL(zoomImage)}
  },[zoomImage])

  async function save(kind,{enlarge=false}={}){
    if(operation.current||!authorized.current)return
    const issued=identity, controller=new AbortController(), token={controller}
    operation.current=token
    setWork(kind);setMessage('Preparing the selected result…')
    try{
      const blob=await runExportPreparation(async signal=>{
      if(enlarge && kind==='cell'){
        if(!valid||standardCell?.identity!==identity)throw new Error('The standard unit-cell preview is not ready')
        return standardCell.blob
      }else return readResultFile(payload,kind,{signal})
      },{signal:controller.signal})
      if(!authorized.current||!alive.current||issued!==currentIdentity.current||operation.current!==token)throw new Error('This result changed. Reopen it before downloading.')
      if(enlarge){
        setZoomImage(URL.createObjectURL(blob))
        setMessage('Standard unit-cell preview ready.')
      }
      else{startBlobDownload(blob,fileNameFor(payload,kind));setMessage('Download started. Check your browser downloads.')}
    }catch(error){if(alive.current&&operation.current===token){
      if(error.code==='FILE_IDENTITY_MISMATCH')setAccessError(error.message)
      setMessage(error.name==='AbortError'?'File preparation cancelled.':error.message)
    }}
    finally{
      controller.abort()
      if(operation.current===token){operation.current=null;if(alive.current){setWork(null)}}
    }
  }
  function showCell(){return save('cell',{enlarge:true})}
  function closeZoom(){dialog.current?.close();setZoomImage(null);zoomButton.current?.focus()}
  async function returnToChat(){
    setMessage('Returning does not switch the chat task. To keep another task pending, reopen this result in a new chat.')
    try{if(window.opener&&!window.opener.closed){window.opener.focus();return}}catch{/* preserve all existing tabs */}
    const url=payload.navigation?.chat_url
    try{const parsed=new URL(url);if(!['http:','https:'].includes(parsed.protocol))throw new Error();window.open(parsed.href,'_blank','noopener')}catch{setMessage('Return to your chat tab. The chat task will not switch automatically.')}
  }
  async function copyResume(){try{await navigator.clipboard.writeText(`/resume ${payload.job_id}`);setMessage('Reopen command copied. Use a new chat to preserve another pending task.')}catch{setMessage('Copy is unavailable. Reopen this result from its saved chat.')}}
  const diameter=materialDiameter(result.parameters),target=productTargetComponents(payload.target), mesh=payload.mesh||{}, domain=mesh.design_domain||{},quality=mesh.quality_summary||{}
  const metric=candidateMetric(selected,payload.ranking,{recordedOnly:true})
  const warnings=resultWarnings(result.warnings)
  const ledger=result.p13_evidence?.geometry_ledger
  const size=ledger?.output_geometry?.bbox?.size, coordinateUnit=mesh.units
  return <section className={`result-workspace ${info?'':'result-info-hidden'}`} aria-label="Conformal Design Results">
    <header className="result-heading"><div><h1>Conformal Design Results</h1><p>{inputName(payload)} · {stale?'Earlier saved result snapshot':'Generated result recorded'}</p></div><button type="button" onClick={returnToChat}>Back to Chat</button></header>
    {accessError&&<p role="alert" className="result-display-note">{accessError} Model and file access is disabled. Refresh to verify the result.</p>}
    <div className="result-action-row" aria-label="Downloads">
      {[['unit_png','Unit-cell preview (PNG)'],['unit_stl','Unit-cell surface (STL, normalized coordinates)'],['conformal_inp','Conformal lattice beam mesh (INP)']].map(([role,title])=>
        <button type="button" key={role} disabled={!permitted(role)||Boolean(work)} onClick={()=>save(role)}>{title}{!permitted(role)?' · unavailable':''}</button>)}
    </div>
    {(clip.enabled||overlay)&&<p className="result-display-note">The conformal INP contains the complete generated beam mesh. The input reference and display clipping are excluded.</p>}
    <div className="result-body">
      <section className={`result-viewport ${touchModel?'result-touch-model':'result-touch-scroll'}`} ref={viewport} aria-label="Conformal lattice viewport">
        <div className="result-toolbar"><label><input type="checkbox" checked={overlay} disabled={!valid||Boolean(work)} onChange={e=>setOverlay(e.target.checked)}/>Show Input Bone Mesh</label>
          {overlay&&<label>Opacity <input type="range" aria-label="Input mesh opacity" min="10" max="80" value={Math.round(opacity*100)} disabled={Boolean(work)} onChange={e=>setOpacity(Number(e.target.value)/100)}/><span>{Math.round(opacity*100)}%</span></label>}
          <button type="button" disabled={Boolean(work)} onClick={()=>setInfo(!info)} aria-expanded={info} aria-controls="result-information">{info?'Hide Information':'Show Information'}</button>
          <button type="button" disabled={Boolean(work)} ref={fullButton} onClick={async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await viewport.current.requestFullscreen()}catch{setMessage('Fullscreen is unavailable in this browser')}}}>{full?'Exit Fullscreen':'Fullscreen'}</button>
          <button type="button" className="result-touch-toggle" aria-pressed={touchModel} disabled={Boolean(work)} onClick={()=>setTouchModel(!touchModel)}>{touchModel?'Scroll Page':'Interact with Model'}</button>
        </div>
        <p className="result-touch-hint">{touchModel?'Model mode: drag to rotate; pinch to zoom. Choose Scroll Page to scroll.':'Scroll the page, or choose Interact with Model to rotate and zoom.'}</p>
        <div className="result-scene-wrap">
          <div className="result-canvas">
            {valid&&stlUrl&&!mainError?<SceneBoundary onFailure={onFailure} fallback={<StaticPreview payload={payload} enabled={valid} error={mainError} onIdentityFailure={setAccessError}/>}>
              <Canvas aria-label="Solid conformal lattice 3D model" orthographic camera={{manual:true,position:[10,10,10],zoom:1}} fallback={<StaticPreview payload={payload} enabled={valid} error={mainError} onIdentityFailure={setAccessError}/>}>
                <color attach="background" args={['#f8fbfd']}/>
                <ProductResultScene url={stlUrl} payload={payload} sourceKey={identity} reference={reference} referenceVisible={overlay} opacity={opacity} clip={clip} view={view} viewToken={viewToken} onFrame={onFrame} onFailure={onFailure}/>
              </Canvas>
            </SceneBoundary>:accessError?<div className="result-static">Result identity must be verified again. Model and file access is disabled.</div>:stale?<div className="result-static">This is an earlier snapshot. Return to chat to reopen the current result.</div>:<StaticPreview payload={payload} enabled={valid} error={mainError} onIdentityFailure={setAccessError}/>}
          </div>
          {!frame&&!mainError&&valid&&stlUrl&&<span className="result-loading" role="status">Loading the 3D structure…</span>}
          {frame&&!mainError&&<div className="result-legend" ref={legend} aria-label="Current view legend">{labels.map(label=><span key={label}>{label}</span>)}</div>}
        </div>
        <div className="result-view-controls"><span>Drag to rotate · scroll to zoom · right-drag to pan</span>{[['iso','Isometric'],['front','Front'],['side','Side'],['top','Top']].map(([key,title])=><button type="button" key={key} disabled={Boolean(work)||!frame||Boolean(mainError)} aria-pressed={view===key} onClick={()=>{setView(key);setViewToken(t=>t+1)}}>{title}</button>)}<button type="button" disabled={Boolean(work)||!frame} onClick={()=>setViewToken(t=>t+1)}>Reset View</button></div>
        <details className="result-clipping"><summary>Inspect Internal Structure</summary><div><label><input type="checkbox" checked={clip.enabled} disabled={Boolean(work)||!frame} onChange={e=>setClip({...clip,enabled:e.target.checked})}/>Enable Clipping</label><label> direction <select aria-label="Clipping axis" value={clip.axis} disabled={!clip.enabled||Boolean(work)} onChange={e=>setClip({...clip,axis:e.target.value})}>{['X','Y','Z'].map(axis=><option key={axis}>{axis}</option>)}</select></label><label>Position <input aria-label="Clipping position" type="range" min="0" max="95" value={clip.position} disabled={!clip.enabled||Boolean(work)} onChange={e=>setClip({...clip,position:Number(e.target.value)})}/>{clip.position}%</label></div></details>
        {overlay&&<p className="result-reference-status" role="status">{referenceLoading?'Loading the original input mesh…':referenceError||`Original input: ${reference?.node_count??'—'} nodes, ${reference?.element_count??'—'} elements. This is an INP reference, not a complete STEP anatomical surface.`}</p>}
        {full&&<p className="result-full-message" role="status" aria-live="polite">{message}</p>}
      </section>
      <aside className={`result-information ${info?'':'result-information-collapsed'}`} id="result-information" aria-hidden={!info} inert={info?undefined:''}>
        <h2>Selected Unit Cell</h2>
        <div className="result-information-scroll" tabIndex={0} aria-label="Selected unit cell and design details">
        {valid&&standardCell?.identity===identity?<button className="result-cell-zoom" type="button" disabled={Boolean(work)} ref={zoomButton} onClick={showCell} aria-label="Enlarge selected unit cell"><img style={{width:'100%'}} src={standardCell.url} alt="Standard selected unit cell; saved normalized geometry"/><span>Enlarge Standard Preview</span></button>:<p role="status">{standardError||(!valid?'Preview unavailable for this snapshot.':!selected?'No unit cell was selected.':'Preparing the standard unit-cell preview…')}</p>}
        <dl><div><dt>Unit cell</dt><dd>{selected?`Rank ${selected.rank??'Not recorded'} · Used in this design`:'Not recorded'}</dd></div><div><dt>Reference unit-cell diameter (normalized)</dt><dd>{selected?.diameter!=null?`${formatProductNumber(selected.diameter)} (dimensionless)`:'Not recorded'}</dd></div><div><dt>Input Bone Mesh</dt><dd>{inputName(payload)}</dd></div></dl>
        <details><summary>Unit-cell Stiffness Reference</summary><div className="result-table-wrap"><table><thead><tr><th>Component</th><th>Target (GPa)</th><th>Predicted unit-cell stiffness (GPa)</th></tr></thead><tbody>{target.map(({name,display})=><tr key={name}><th>{name}</th><td>{display==='Not recorded'?'Not recorded':display}</td><td>{selected?.predicted_stiffness?.[name]!=null?formatProductNumber(selected.predicted_stiffness[name]):'Not recorded'}</td></tr>)}</tbody></table></div></details>
        <details><summary>Material and Structure</summary>{metric&&<p>{metric}</p>}<p>Eligibility: {candidateEligibility(selected)}</p><p>Base material: {payload.target?.base_material?.name||'Not recorded'}{payload.target?.base_material?.elastic_modulus_gpa!=null?` · Elastic modulus ${formatProductNumber(payload.target.base_material.elastic_modulus_gpa)} GPa`:''}</p>{selected?.relative_density!=null&&<p>Reference unit-cell relative density: {formatProductNumber(selected.relative_density)} (dimensionless)</p>}<p>{diameter?`Result model strut diameter: ${formatProductNumber(diameter.value)} ${diameter.unit}`:'The result strut diameter has no complete recorded unit or source.'}</p>{diameter?.source==='Normalized unit-cell diameter scaled by the mesh reference length'&&<p>Diameter source: normalized unit-cell diameter scaled by the mesh reference length.</p>}</details>
        <details><summary>Design Domain and Checks</summary><p>Used for generation: {domain.selected_element_count??mesh.element_count??'Not recorded'} hexahedra · {domain.selected_node_count??mesh.node_count??'Not recorded'} nodes</p><p>Excluded elements: {domain.excluded_element_count??'Not recorded'} (not used for generation).</p><p>Mesh units: {mesh.units||'Not recorded'} · Input precheck: {['PASS','passed'].includes(mesh.precheck_status)?'Passed':mesh.precheck_status||'Not recorded'}</p>{quality.status&&<p>Mesh geometry check: {quality.status==='PASS'?'Passed':quality.status}</p>}{quality.minimum_scaled_jacobian!=null&&<p>Minimum scaled Jacobian: {formatProductNumber(quality.minimum_scaled_jacobian)}</p>}<p>Lattice beam mesh: {result.core_output?.node_count??'Not recorded'} nodes · {result.core_output?.beam_count??'Not recorded'} B31 beams</p>{Array.isArray(size)&&size.length===3&&size.every(Number.isFinite)&&<p>Output bounds (X × Y × Z): {size.map(formatProductNumber).join(' × ')} {coordinateUnit||'(coordinate units not recorded)'}</p>}{quality.maximum_jacobian_condition_2!=null&&<p>Maximum Jacobian condition number: {formatProductNumber(quality.maximum_jacobian_condition_2)}</p>}{warnings.length>0&&<ul aria-label="Recorded design limitations">{warnings.map(warning=><li key={warning}>{warning}</li>)}</ul>}<p>Result files: {valid?'Verified for file access':'Access unavailable'} · 3D display: {mainError?'Unavailable':frame?'Loaded':'Preparing'}</p></details>
        <details><summary>Design Record</summary><p>Target file: {displayBasename(payload.target?.source?.source_name)||'Not recorded'}</p>{payload.target?.source?.record_index!=null&&<p>Original record: {payload.target.source.record_index} of {payload.target.source.record_count}</p>}<p>{selected?.confirmed?'Confirmed by the user':payload.selection?.confirmation_status==='LEGACY_NOT_RECORDED'?'Historical selection; confirmation not recorded':selected?.recommended?'System recommendation':'Selection status not recorded'}</p><p>Selection: {selected ? `Rank ${selected.rank??'not recorded'}` : 'Not recorded'}</p><button type="button" onClick={copyResume}>Copy Reopen Command</button></details>
        </div>
      </aside>
    </div>
    <p className="result-navigation-note">Returning to chat does not switch tasks.<button type="button" onClick={copyResume}>Copy Reopen Command</button></p>
    <p className="result-message" role="status" aria-live="polite" hidden={full}>{message}</p>
    <dialog ref={dialog} className="result-image-dialog" onCancel={e=>{e.preventDefault();closeZoom()}} onClick={e=>{if(e.target===e.currentTarget)closeZoom()}}><button type="button" onClick={closeZoom}>Close Unit-cell Image</button>{zoomImage&&<img src={zoomImage} alt="Enlarged selected unit cell; normalized diameter is dimensionless"/>}</dialog>
  </section>
}
