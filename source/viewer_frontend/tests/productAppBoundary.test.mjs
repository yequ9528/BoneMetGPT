import assert from 'node:assert/strict'
import { readFileSync, mkdirSync, unlinkSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { createHash, webcrypto } from 'node:crypto'
import { basename, resolve } from 'node:path'
import Reconciler from 'react-reconciler'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { build } from 'esbuild'


function source(path) {
  return readFileSync(new URL(path, import.meta.url), 'utf8')
}

const jobId = 'job-1234567890ABCDEF12345678'
const base = `/api/viewer/product/jobs/${jobId}`
const snapshot = 'AB'.repeat(32)
const delivery = role => `${base}/deliver/${role}?snapshot=${snapshot}`
const scene = role => `${base}/scene/${role}?snapshot=${snapshot}`
const names = {unit_png:'selected-unit-cell.png', unit_stl:'selected-unit-cell.stl', conformal_inp:'conformal-lattice-b31.inp'}
const titles = {unit_png:'Unit-cell preview (PNG)', unit_stl:'Unit-cell surface (STL, normalized coordinates)', conformal_inp:'Conformal lattice beam mesh (INP)'}
function records(...keys) {
  return {
    downloads: Object.fromEntries(keys.map(key=>[key,delivery(key)])),
    download_identities: Object.fromEntries(keys.map(key=>[key,{filename:names[key],size_bytes:4,sha256:'a'.repeat(64)}]))
  }
}
function assertActions(html,enabled) {
  const row = html.match(/<div[^>]*aria-label="Downloads"[^>]*>([\s\S]*?)<\/div>/)?.[1]
  assert.ok(row, 'download toolbar missing')
  const buttons = [...row.matchAll(/<button\b([^>]*)>([^<]*)<\/button>/g)]
  assert.deepEqual(buttons.map(([,attrs,label]) => [label,/\bdisabled\b/.test(attrs)]),
    Object.entries(titles).map(([role,title]) => enabled.includes(role)?[title,false]:[title+' · unavailable',true]))
  assert.doesNotMatch(html, /<a\b[^>]*href="[^"]*\/(?:files|scene|deliver)\//)
}
function bundleDirectory() {
  return process.env.BMG_VIEWER_TEST_TMP
    ? pathToFileURL(resolve(process.env.BMG_VIEWER_TEST_TMP) + '/')
    : new URL('../.test-temp/', import.meta.url)
}
let panelModule
async function renderPanelModule() {
  if (!panelModule) panelModule = (async () => {
    const temp = bundleDirectory()
    mkdirSync(temp, { recursive: true })
    const output = new URL(`panel-${process.pid}.mjs`, temp)
    await build({ entryPoints: [fileURLToPath(new URL('../src/panels/ProductJobPanel.jsx', import.meta.url))],
      outfile: fileURLToPath(output), bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic', loader: {'.css':'empty'}, logLevel: 'silent',
      plugins: [{ name: 'node-three-module-extensions', setup(builder) {
        builder.onResolve({ filter: /^three\/examples\/jsm\// }, args => ({ path: args.path.endsWith('.js') ? args.path : `${args.path}.js`, external: true }))
      } }] })
    try { return await import(output.href) } finally { unlinkSync(output) }
  })()
  return panelModule
}

// Mount the real result workspace without a browser. The reconciler runs effects,
// including the guarded fetch, WebCrypto verification and object URL cleanup.
const noop = () => {}
const renderer = Reconciler({
  supportsMutation:true, isPrimaryRenderer:false, supportsMicrotasks:true, scheduleMicrotask:queueMicrotask,
  getRootHostContext:()=>({}), getChildHostContext:parent=>parent, getPublicInstance:instance=>instance,
  prepareForCommit:()=>null, resetAfterCommit:noop, shouldSetTextContent:()=>false,
  createInstance:()=>({}), createTextInstance:text=>text, appendInitialChild:noop, finalizeInitialChildren:()=>false,
  appendChild:noop, appendChildToContainer:noop, insertBefore:noop, insertInContainerBefore:noop,
  removeChild:noop, removeChildFromContainer:noop, clearContainer:noop, prepareUpdate:()=>true, commitUpdate:noop,
  commitTextUpdate:noop, detachDeletedInstance:noop, getCurrentEventPriority:()=>16,
  scheduleTimeout:setTimeout, cancelTimeout:clearTimeout, noTimeout:-1
})
function hostElements(root, type) {
  const result = []
  function visit(node) {
    if (!node) return
    if (node.type===type) result.push(node.memoizedProps)
    visit(node.child)
    visit(node.sibling)
  }
  visit(root.current.child)
  return result
}
const posterBytes = Buffer.from('UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA','base64')
async function withPreview(panel, payload, props, check) {
  const originalFetch=globalThis.fetch, originalDocument=globalThis.document
  const requests=[]
  if(!globalThis.crypto)globalThis.crypto=webcrypto
  const root=renderer.createContainer({},0,null,false,null,'',error=>{throw error},null)
  globalThis.document={addEventListener:noop,removeEventListener:noop}
  globalThis.fetch=async(url,options)=>{
    requests.push({url,options})
    return new Response(posterBytes,{headers:{'content-type':'image/webp','content-length':String(posterBytes.length),
      etag:'"'+createHash('sha256').update(posterBytes).digest('hex')+'"'}})
  }
  try {
    renderer.updateContainer(React.createElement(panel.default,{payload,layoutMode:'product-page',...props}),root,null)
    renderer.flushPassiveEffects()
    await check({root, requests, fetchBlob:originalFetch})
  } finally {
    renderer.updateContainer(null,root,null)
    renderer.flushPassiveEffects()
    globalThis.fetch=originalFetch
    if(originalDocument===undefined)delete globalThis.document
    else globalThis.document=originalDocument
  }
}
async function checkPoster({root, requests, fetchBlob}) {
  const deadline=Date.now()+3000
  while (!hostElements(root,'img').length && Date.now()<deadline) await new Promise(resolve=>setTimeout(resolve,5))
  const [image]=hostElements(root,'img')
  assert.equal(image?.alt,'Original preview saved for this result')
  assert.match(image.src,/^blob:/)
  assert.equal(requests.length,1)
  assert.ok(requests[0].url.endsWith(scene('poster')))
  assert.equal(requests[0].options.redirect,'error')
  assert.deepEqual(Buffer.from(await (await fetchBlob(image.src)).arrayBuffer()),posterBytes)
  assert.deepEqual(hostElements(root,'canvas'),[])
  return image.src
}
function previewPayload() {
  const payload=embeddedPayload('result')
  payload.candidates=[]
  payload.result.scene={poster:scene('poster')}
  return payload
}

test('unavailable interactive preview shows a verified internal WebP poster and releases its object URL', async () => {
  const panel=await renderPanelModule()
  const payload=previewPayload()
  const html=renderToStaticMarkup(React.createElement(panel.default,{payload,layoutMode:'product-page'}))
  assert.match(html,/3D preview is unavailable/)
  assert.match(html,/The fixed result image is shown when available/)
  let objectUrl
  await withPreview(panel,payload,{},async context=>{objectUrl=await checkPoster(context)})
  await assert.rejects(fetch(objectUrl))
})

test('stale, disabled, foreign and incorrectly bound posters cannot load through the failure fallback', async () => {
  const panel=await renderPanelModule()
  for (const [url,props] of [
    [scene('poster'),{stale:true}],[scene('poster'),{downloadsEnabled:false}],[null,{}],
    ['https://untrusted.example/image.webp',{}],[scene('poster')+'&extra=x',{}],
    [scene('poster').replace(jobId,'job-AAAAAAAAAAAAAAAAAAAAAAAA'),{}],
    [scene('triangles'),{}],[delivery('unit_png'),{}]
  ]) {
    const payload=previewPayload()
    payload.result.scene.poster=url
    await withPreview(panel,payload,props,async ({root,requests})=>{
      await new Promise(resolve=>setImmediate(resolve))
      assert.equal(requests.length,0)
      assert.deepEqual(hostElements(root,'img'),[])
    })
  }
})

test('WebGL unavailable display uses the guarded scene poster without a canvas', async () => {
  const panel=await renderEmbeddedPanelModule()
  const payload=previewPayload()
  payload.result.scene.triangles=scene('triangles')
  await withPreview(panel,payload,{},checkPoster)
  await withPreview(panel,payload,{stale:true},async ({root,requests})=>{
    assert.equal(requests.length,0)
    assert.deepEqual(hostElements(root,'img'),[])
  })
})

test('unready preview retains the verified STL download and a stale result disables every file resource', async () => {
  const panel = await renderPanelModule()
  const payload=previewPayload()
  Object.assign(payload.result,records('unit_stl'))
  const render=props=>renderToStaticMarkup(React.createElement(panel.default,{payload,layoutMode:'product-page',...props}))
  const html=render({})
  assertActions(html,['unit_stl'])
  assert.match(html,/disabled=""[^>]*>Reset View/)
  const stale=render({stale:true,downloadsEnabled:false})
  assertActions(stale,[])
  assert.doesNotMatch(stale,/<img|<canvas/)
})

test('product entry has no mixed app, query mode, legacy API or action controls', () => {
  const combined = [
    source('../src/product-main.jsx'),
    source('../src/ProductApp.jsx'),
    source('../src/panels/ProductJobPanel.jsx')
  ].join('\n')
  for (const forbidden of [
    "./App.jsx",
    'queryParams',
    "api/viewerApi",
    "../api/viewerApi",
    'database_v2',
    'prediction',
    'upload',
    'authorize',
    'retry'
  ]) {
    assert.equal(combined.includes(forbidden), false, forbidden)
  }
  assert.match(combined, /productViewerApi/)
  assert.match(combined, /ProductJobPanel/)
})

test('product reading area names interrupted and failed runs separately without echoing raw diagnostics', async () => {
  const panel = await renderPanelModule()
  const payload = { view_state: 'failed', candidates: [], progress: {
    job_status: 'HOLD_RUNTIME_INTERRUPTED', current_stage: 'HOLD_RUNTIME_INTERRUPTED',
    failure: { stage: 'CONFORMAL_RUNNING', message: '保留的运行诊断' }
  } }
  const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'product-page' }))
  assert.match(html, /Run interrupted/)
  assert.doesNotMatch(html, /此任务已失败，尚无可用生成结果/)
  assert.match(html, /No automatic retry/)
  assert.doesNotMatch(html, /保留的运行诊断/)
  assert.doesNotMatch(visibleBeforeExpansion(html), /保留的运行诊断/)
  assert.doesNotMatch(html, /Technical details|<pre|HOLD_RUNTIME_INTERRUPTED/)
  const failed = renderToStaticMarkup(React.createElement(panel.default, {
    payload: { ...payload, progress: { ...payload.progress, job_status: 'FAILED' } }, layoutMode: 'product-page'
  }))
  assert.match(failed, /Geometry generation failed/)
  assert.doesNotMatch(failed, /保留的运行诊断/)
  assert.doesNotMatch(visibleBeforeExpansion(failed), /保留的运行诊断/)
})

test('product result describes only recorded files and missing numeric values are not labelled with units', async () => {
  const panel = await renderPanelModule()
  const payload = { view_state: 'result', candidates: [], target: { base_material: { name: 'TC4' } },
    progress: { job_status: 'VIEWER_READY' }, result: { package_id: 'p' } }
  const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'product-page' }))
  assert.doesNotMatch(html, /本次生成1份梁模型，附STL预览和PNG图片/)
  assert.doesNotMatch(html, /Not recorded(?:<!-- -->)? GPa|Not recorded(?:<!-- -->)? \(dimensionless\)/)
  assertActions(html,[])
  const complete = renderToStaticMarkup(React.createElement(panel.default, {
    payload: { ...payload, job_id: jobId, result: { ...payload.result, ...records(...Object.keys(names)) } },
    layoutMode: 'product-page'
  }))
  assertActions(visibleBeforeExpansion(complete),Object.keys(names))
})

test('product reading area shows the selected rank and diameter without rendering its original record or mutating it', async () => {
  const panel = await renderPanelModule()
  const candidateId = 'inverse_150w_0002-0123456789ABCDEF0123456789ABCDEF'
  const payload = { view_state: 'result', candidates: [
    { candidate_id: 'inverse_150w_0001', rank: 1, diameter: 0.1 },
    { candidate_id: candidateId, rank: 2, selected: true, diameter: 0.123456789012345 }
  ], selection: { selected_candidate_id: candidateId, confirmation_status: 'CONFIRMED' },
  progress: { job_status: 'VIEWER_READY' }, result: { package_id: 'p' } }
  const original = JSON.stringify(payload)
  const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'product-page' }))
  const readingArea = visibleBeforeExpansion(html)
  assert.match(readingArea, /Rank 2 · Used in this design/)
  assert.doesNotMatch(readingArea, /inverse_150w_|0123456789ABCDEF/)
  assert.match(readingArea, /0\.123457 \(dimensionless\)/)
  assert.doesNotMatch(html, /Technical details|<pre/)
  assert.equal(html.includes(candidateId), false, 'protocol IDs are absent even from closed disclosures')
  assert.equal(html.includes('0.123456789012345'), false)
  assert.equal(JSON.stringify(payload), original)
})

test('unknown product page state offers the existing read action without exposing result actions', async () => {
  const panel = await renderPanelModule()
  const payload = { view_state: 'invented', candidates: [], progress: { job_status: 'invented' } }
  const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'product-page' }))
  assert.match(html, /Task status needs review/)
  assert.match(html, /Refresh Job status/)
  assert.doesNotMatch(html, /<a\b[^>]*href="[^"]*\/(?:files|scene|deliver)\//)
})

test('product error display shows a typed support code without raw message or full Job', () => {
  const app = source('../src/ProductApp.jsx')
  assert.match(app, /productReadFailure/)
  assert.match(app, /error\.code/)
  assert.doesNotMatch(app, /Full Job ID|error\.reason/)
  assert.match(app, /layoutMode="product-page"/)
  assert.match(app, /tabIndex=\{0\}/)
  assert.doesNotMatch(app, /Product Job evidence is unavailable/)
})

test('result uses the materials-user workspace while non-result task states retain their existing presentation', async () => {
  const panel = await renderPanelModule()
  const cases = [
    ['result', 'VIEWER_READY', 'Conformal Design Results'],
    ['candidate', 'CANDIDATES_READY', 'Candidates generated; confirmation pending'],
    ['mesh', 'MESH_READY', 'Mesh record prepared; launch-status update pending'],
    ['running', 'CONFORMAL_RUNNING', 'Generating conformal geometry'],
    ['failed', 'FAILED', 'Job failed'],
    ['failed', 'HOLD_RUNTIME_INTERRUPTED', 'Run interrupted'],
    ['invented', 'INVENTED', 'Task status needs review']
  ]
  for (const [viewState, jobStatus, expected] of cases) {
    const payload = { view_state: viewState, candidates: [], progress: { job_status: jobStatus },
      non_claims: ['NOT_FEA_VALIDATION', 'NOT_MECHANICAL_PASS'],
      ...(viewState === 'result' ? { result: { package_id: 'p', p13_evidence: { integrity_status: 'NOT_EVALUATED' } } } : {}) }
    const original = JSON.stringify(payload)
    const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'product-page' }))
    assert.ok(html.includes(expected), jobStatus)
    if(viewState!=='result')assert.doesNotMatch(html, /[\p{Script=Han}]/u, jobStatus)
    assert.doesNotMatch(html, /Technical details|Validation scope and full statements|Package assessment and four-layer evidence|Original target and candidates|Original mesh and element lists|Sources and versions|Original stages and events|Preview diagnostics|<pre|NOT_FEA_VALIDATION|No finite-element validation conclusion/)
    if(viewState!=='result')assert.match(html, /Computation scope is not recorded/)
    if (jobStatus === 'HOLD_RUNTIME_INTERRUPTED') assert.doesNotMatch(html, /Calculation failed/)
    if (viewState === 'result') {
      assert.doesNotMatch(html, /P13 not evaluated|does not mean geometry generation failed/)
      assertActions(html,[])
    }
    assert.equal(JSON.stringify(payload), original)
  }
})

test('target validation failure is not presented as failed inverse computation', async () => {
  const panel = await renderPanelModule()
  const payload = { view_state: 'failed', candidates: [], progress: { job_status: 'FAILED',
    failure: { stage: 'TARGET_CONFIRMED', message: 'Target range validation did not pass.' } } }
  const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'product-page' }))
  assert.match(html, /Target validation did not pass; inverse design not started/)
  assert.doesNotMatch(html, /Calculation failed|Candidate generation failed|Geometry generation failed/)
  assert.doesNotMatch(html, /Technical details|<pre|TARGET_CONFIRMED/)
  assert.match(html, /No automatic retry/)
})

test('held candidate page offers evidence review without implying a selectable candidate or a specific failed check', async () => {
  const panel = await renderPanelModule()
  const payload = { workflow_disposition: 'HOLD', view_state: 'candidate', candidates: [
    { candidate_id: 'candidate-1', rank: 1, eligible: false, diameter: 0.1 }
  ], progress: { job_status: 'CANDIDATES_READY' } }
  const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'product-page' }))
  const readingArea = html
  assert.match(readingArea, /Candidate step on hold/)
  assert.match(readingArea, /recorded eligibility evidence/)
  assert.match(readingArea, /Refresh Job status/)
  assert.doesNotMatch(readingArea, /confirmation pending|confirm your choice|P-?09|precheck failed/i)
  assert.doesNotMatch(html, /Technical details|<pre/)
  assert.doesNotMatch(readingArea, /<button|<a\b[^>]*href="[^"]*\/(?:files|scene|deliver)\//)
})

test('product entry uses only the result-first surface and cleans request timers', () => {
  const app = source('../src/ProductApp.jsx')
  assert.doesNotMatch(app, /<WorkflowHeader|<DualSpaceWorkflowPanel|<ViewerEvidenceProvider/)
  assert.match(app, /Reading saved task records/)
  assert.match(app, /15000/)
  assert.match(app, /clearTimeout\(slowTimer\)/)
  assert.match(app, /retainSnapshot/)
  assert.match(app, /downloadsEnabled=/)
  assert.match(app, /getProductPageState/)
  assert.match(app, /createProductJobReader\(fetchProductJob\)/)
  assert.match(app, /height: 100vh/)
  assert.match(app, /height: 100dvh/)
  assert.match(app, /max-width: 840px/)
  assert.match(app, /font-size: 16px/)
  assert.doesNotMatch(app, /product-evidence/)
})

test('product preview fits explicitly and contains failures without changing shared defaults', () => {
  const panel = source('../src/panels/ProductJobPanel.jsx')
  const viewer = source('../src/LatticeViewer.jsx')
  assert.match(panel, /cameraMode="product-fit"/)
  assert.match(panel, /ProductPreviewBoundary/)
  assert.match(panel, /<Suspense/)
  assert.match(panel, /Reset view/)
  assert.match(panel, /disabled=\{!loaded\}/)
  assert.match(panel, /camera=\{\{ manual: true/)
  assert.match(panel, /View nine target components \(GPa\)/)
  assert.match(panel, /<details/)
  assert.doesNotMatch(panel, /Technical details|Sources and versions|function ProductEvidence|function RawEvidence|function ProductPreviewDiagnostics/)
  assert.match(viewer, /cameraMode = 'legacy'/)
  assert.match(viewer, /computeProductCameraFit/)
  assert.match(viewer, /productCameraState/)
  assert.match(viewer, /enableDamping=\{false\}/)
  assert.match(panel, /layoutMode = 'embedded'/)
})

test('fallback is pure display and product fetch owns cancellation, geometry and frame reporting', () => {
  const panel = source('../src/panels/ProductJobPanel.jsx')
  const viewer = source('../src/LatticeViewer.jsx')
  const fallback = panel.slice(panel.indexOf('function PreviewUnavailable('), panel.indexOf('function PreviewCanvasEvents('))
  assert.doesNotMatch(fallback, /useEffect|onFailure|setState/)
  assert.match(panel, /fallback=\{<PreviewUnavailable /)
  assert.match(panel, /componentDidCatch\(error, info\)/)
  assert.match(panel, /webglcontextlost/)
  assert.match(panel, /removeEventListener/)
  assert.match(panel, /reduceProductPreview/)
  const product = viewer.slice(viewer.indexOf('function ProductLatticeViewer('))
  assert.match(product, /AbortController/)
  assert.match(product, /readProductStlBytes/)
  assert.match(product, /inspectProductStl/)
  assert.match(product, /validateProductGeometry/)
  assert.match(product, /geometry\?\.dispose\(\)/)
  assert.match(product, /onAfterRender=\{model.afterRender\}/)
  assert.match(product, /first_frame_submitted/)
  assert.doesNotMatch(product, /useLoader\(/)
  assert.match(viewer, /useLoader\(STLLoader, stlUrl\)/)
})

function detailSections(html) {
  const stack = [], sections = []
  for (const match of html.matchAll(/<\/?details\b[^>]*>/g)) {
    if (match[0].startsWith('</')) {
      const entry = stack.pop()
      sections.push({ ...entry, end: match.index + match[0].length,
        html: html.slice(entry.start, match.index + match[0].length) })
    } else stack.push({ start: match.index, depth: stack.length + 1, tag: match[0] })
  }
  assert.equal(stack.length, 0)
  return sections
}

function visibleBeforeExpansion(html) {
  let cursor = 0, visible = ''
  for (const section of detailSections(html).filter(row => row.depth === 1).sort((a, b) => a.start - b.start)) {
    visible += html.slice(cursor, section.start) + section.html.match(/<summary>[\s\S]*?<\/summary>/)[0]
    cursor = section.end
  }
  return visible + html.slice(cursor)
}

test('ordinary result markup contains key geometry parameters and file links without technical groups or original records', async () => {
  const panel = await renderPanelModule()
  const ids = Array.from({ length: 300 }, (_, index) => index + 1000)
  const payload = { job_id: 'job-1234567890ABCDEF12345678', revision: 0, schema_version: 'viewer_v1',
    updated_at: '2099-01-01', view_state: 'result', candidates: [
      { candidate_id: 'PRIVATE_SELECTED_CANDIDATE', rank: 1, selected: true, diameter: 0.076933 }
    ], target: { source: { sample_id: 0 }, base_material: { name: 'TC4', elastic_modulus_gpa: 110 },
      physical_9d_gpa: { C11: 2.3273867572530444 } },
    mesh: { original_filename: 'selected.inp', node_count: 0, element_count: 0, units: 'mm',
      design_domain: { excluded_element_count: 0, selected_element_ids: ids } },
    progress: { job_status: 'VIEWER_READY', events: [{ event: 'ORIGINAL_EVENT_ONLY' }] },
    dual_space_status: { mainline: 'ORIGINAL_FOUR_LAYER_ONLY' },
    non_claims: ['SELECTED_DOMAIN_ONLY', 'NOT_FULL_BONE_COVERAGE', 'NOT_EXCLUDED_ELEMENT_MAPPING',
      'NOT_MECHANICAL_VALIDATION', 'NOT_FEA_VALIDATION', 'NOT_MANUFACTURING_VALIDATION', 'NOT_CLINICAL_VALIDATION', 'NOT_FULL_DUAL_SPACE_FUSION'],
    result: { package_id: 'p', p13_evidence: { status: 'not_available_legacy_package', integrity_status: 'NOT_EVALUATED' },
      compatibility: { producer: { generation: 'v18' }, reader: { generation: 'v28' } },
      core_output: { node_count: 5223, beam_count: 19878 },
      ...records(...Object.keys(names)),
      file_identities: { b31: { sha256: 'UNSHORTENED_HASH_ONLY_IN_RAW_RECORDS' } } } }
  const before = JSON.stringify(payload)
  const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'product-page' }))
  const visible = visibleBeforeExpansion(html)
  assert.match(visible, /Conformal Design Results/)
  assert.match(visible, /Conformal lattice viewport/)
  assert.match(visible, /Selected Unit Cell/)
  assert.match(visible, /Rank 1 · Used in this design/)
  assert.match(visible, /0\.076933 \(dimensionless\)/)
  assert.doesNotMatch(visible, /PRIVATE_SELECTED_CANDIDATE|UNSHORTENED_HASH/)
  assert.doesNotMatch(html, /<pre|ORIGINAL_EVENT_ONLY|ORIGINAL_FOUR_LAYER_ONLY|UNSHORTENED_HASH|2\.3273867572530444/)
  assert.doesNotMatch(html, /PRIVATE_SELECTED_CANDIDATE/)
  assert.match(html, /0 hexahedra · 0 nodes/)
  assert.match(html, /Elastic modulus 110 GPa/)
  assert.match(html, /Mesh units: mm/)
  assert.match(html, /5223 nodes · 19878 B31 beams/)
  assert.match(html, /C11<\/th><td>2\.32739/)
  assert.doesNotMatch(html, /do not describe the performance of the whole conformal structure/)
  assert.doesNotMatch(html, /<a\b[^>]*href="[^"]*\/(?:files|scene|deliver)\//)
  assertActions(html,Object.keys(names))
  const sections = detailSections(html)
  assert.equal(sections.length, 5)
  assert.ok(sections.every(section=>section.depth===1&&!/\bopen(?:=|\s|>)/.test(section.tag)))
  assert.doesNotMatch(visible, /完整身份|1299|PRIVATE_SELECTED_CANDIDATE/)
  assert.deepEqual(payload.mesh.design_domain.selected_element_ids, ids)
  assert.equal(JSON.stringify(payload), before)
})

test('preview diagnostics stay in logging while plain preview failures remain visible without a raw-record drawer', async () => {
  const panel = await renderPanelModule()
  assert.equal(panel.ProductPreviewDiagnostics, undefined)
  const production = source('../src/panels/ProductJobPanel.jsx')
  assert.match(production, /console\.error\('Product STL preview diagnostic', diagnostic, event\.timings\)/)
  assert.match(production, /console\.info\('Product STL frame submitted'/)
  assert.match(production, /console\.error\('Product STL render failure'/)
  const jobId = 'job-1234567890ABCDEF12345678'
  const view = await import(new URL('../src/utils/productJobView.js', import.meta.url))
  const diagnostic = view.productPreviewDiagnostic(view.productStlError('HTTP_ERROR', 'read'), {
    job_id: jobId, request_key: 'preview-1', resource: `/api/viewer/product/jobs/${jobId}/files/stl`
  })
  assert.equal(diagnostic.code, 'HTTP_ERROR')
  assert.equal(diagnostic.stage, 'read')
  assert.equal(diagnostic.safe_resource, `/api/viewer/product/jobs/${jobId}/files/stl`)
  assert.match(diagnostic.message, /The STL preview file could not be read\. The saved result information and file links remain available\./)
  for (const message of [diagnostic.message, 'The STL preview could not be parsed.', 'The 3D preview is unavailable.']) {
    const html = renderToStaticMarkup(React.createElement(panel.ProductPreviewFailure, {
      jobId, message, stale: false, downloadsEnabled: true
    }))
    assert.match(html, /role="status"/)
    assert.ok(html.includes(message))
    assert.doesNotMatch(html, /Technical details|Preview diagnostics|<pre|<details/)
  }
})

test('important package assessment warnings stay visible while unevaluated evidence does not imply generation failure or alter file guards', async () => {
  const panel = await renderPanelModule()
  const jobId = 'job-1234567890ABCDEF12345678'
  for (const [evidence, attention] of [
    [{ integrity_status: 'NOT_EVALUATED', status: 'not_available_legacy_package' }, false],
    [{ integrity_status: 'PASS', status: 'verified', package_contract: {} }, false],
    [{ integrity_status: 'FAIL', status: 'failed' }, true],
    [{ integrity_status: 'PASS' }, true],
    [{ integrity_status: 'NEW_STATE' }, true]
  ]) {
    const payload = { job_id: jobId, view_state: 'result', candidates: [], progress: { job_status: 'VIEWER_READY' },
      result: { package_id: 'p', p13_evidence: evidence, ...records('unit_stl') } }
    const before = JSON.stringify(payload)
    const render = props => renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'product-page', ...props }))
    const html = render({})
    const visible = visibleBeforeExpansion(html)
    assert.doesNotMatch(html, /Technical details|Preview diagnostics|<pre/)
    assert.equal(visible.includes('aria-label="Package assessment warning"'), attention)
    assert.match(visible, /Generated result recorded/)
    assertActions(visible,['unit_stl'])
    assertActions(visibleBeforeExpansion(render({stale:true,downloadsEnabled:false})),[])
    assert.equal(JSON.stringify(payload), before)
  }
})

let embeddedPanelModule
async function renderEmbeddedPanelModule() {
  if (!embeddedPanelModule) embeddedPanelModule = (async () => {
    const entry = fileURLToPath(new URL('../src/panels/ProductJobPanel.jsx', import.meta.url))
    const temp = bundleDirectory()
    mkdirSync(temp, { recursive: true })
    const output = new URL(`embedded-panel-${process.pid}.mjs`, temp)
    const boundaries = new Map([
      ['@react-three/fiber', `import { createElement } from 'react';
        export function Canvas({ children, camera }) {
          return createElement('div', { 'data-display-boundary': 'canvas',
            'data-camera-position': camera?.position?.join(','), 'data-camera-zoom': camera?.zoom }, children);
        }
        export function useThree() { throw new Error('Canvas hooks must stay outside embedded SSR'); }`],
      ['@react-three/drei', `import { createElement } from 'react';
        export function Html({ children }) { return createElement('span', null, children); }`],
      ['../LatticeViewer', `import { createElement } from 'react';
        export default function LatticeViewer({ stlUrl, viewMode }) {
          return createElement('span', { 'data-display-boundary': 'stl', 'data-stl-url': stlUrl, 'data-view-mode': viewMode });
        }`],
      ['../visualization/CandidateCellViewer', `import { createElement } from 'react';
        export default function CandidateCellViewer({ candidate }) {
          return createElement('span', { 'data-display-boundary': 'candidate-cell', 'data-candidate-id': candidate.candidate_id });
        }`],
      ['../visualization/HexMeshViewer', `import { createElement } from 'react';
        export default function HexMeshViewer({ meshPayload, viewMode }) {
          return createElement('span', { 'data-display-boundary': 'mesh', 'data-mesh-status': meshPayload.status, 'data-view-mode': viewMode });
        }`]
    ])
    try {
      await build({ entryPoints: [entry], outfile: fileURLToPath(output), bundle: true,
        platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic', loader: {'.css':'empty'}, logLevel: 'silent',
        plugins: [{ name: 'embedded-display-boundaries', setup(builder) {
          builder.onResolve({filter:/^@react-three\/fiber$/},args=>
            basename(args.importer) === 'ProductResultWorkspace.jsx'
              ? {path:args.path,namespace:'webgl-unavailable'} : null)
          builder.onLoad({filter:/.*/,namespace:'webgl-unavailable'},()=>({contents:'export function Canvas({fallback}) {return fallback}'}))
          builder.onResolve({ filter: /^(?:@react-three\/(?:fiber|drei)|\.\.\/(?:LatticeViewer|visualization\/(?:CandidateCellViewer|HexMeshViewer)))$/ }, args => {
            if (args.importer.replaceAll('\\', '/') !== entry.replaceAll('\\', '/')) return null
            return { path: args.path, namespace: 'embedded-display-boundary' }
          })
          builder.onLoad({ filter: /.*/, namespace: 'embedded-display-boundary' }, args => ({ contents: boundaries.get(args.path), loader: 'js', resolveDir: fileURLToPath(new URL('../', import.meta.url)) }))
        } }] })
      return await import(output.href)
    } finally {
      try { unlinkSync(output) } catch (error) { if (error.code !== 'ENOENT') throw error }
    }
  })()
  return embeddedPanelModule
}

for (const layoutMode of ['embedded', 'product-page']) {
  test(`conformal result omits duplicate cell commentary and auxiliary snapshot in ${layoutMode}`, async () => {
    const panel = await renderPanelModule()
    const payload = embeddedPayload('result')
    payload.result.warnings = ['explicit diameter used', 'not real bone defect validation',
      'not spatially-varying generative lattice design', 'not dual-space fusion completion',
      'interface node set was not provided and was not inferred']
    const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode }))
    assert.doesNotMatch(html, /Auxiliary interactive 3D|Enlarge Auxiliary View|same saved geometry with a different camera/)
    assert.doesNotMatch(html, /provide no mechanical|no validation against a real bone defect|Full dual-space fusion|spatially varying lattice design|model uses the specified diameter/)
    assert.match(html, /Predicted unit-cell stiffness \(GPa\)/)
    assert.match(html, /No interface node set was supplied or inferred/)
    for (const label of ['Conformal lattice viewport', 'Reset View', 'Show Input Bone Mesh', 'Enable Clipping']) {
      assert.ok(html.includes(label), label)
    }
  })
}

test('result only displays a recorded ranking metric, including zero', async () => {
  const panel = await renderPanelModule()
  for (const layoutMode of ['product-page', 'embedded']) {
    for (const [metric, score, nmse, expected] of [
      ['score', null, 0.00033, null], ['score', 0, 0.2, 'P-10 score (lower is better): 0'],
      ['nmse', 0.5, null, null], ['nmse', 0.5, 0.0123, 'NMSE (dimensionless, lower is better): 0.0123']
    ]) {
      const payload = embeddedPayload('result')
      payload.ranking = { metric }
      Object.assign(payload.candidates[0], {score, nmse})
      const html = renderToStaticMarkup(React.createElement(panel.default, {payload, layoutMode}))
      const section = html.split('<summary>Material and Structure</summary>')[1].split('</details>')[0]
      if (expected) assert.ok(section.includes(expected))
      else assert.doesNotMatch(section, /P-10 score|NMSE/)
    }
  }
})

function embeddedPayload(viewState) {
  const jobId = 'job-1234567890ABCDEF12345678'
  return {
    job_id: jobId, revision: 10, workflow_disposition: 'ACTIVE', view_state: viewState,
    candidates: [1, 2, 3].map(rank => ({ candidate_id: `inverse_150w_000${rank}`, rank,
      selected: rank === 1, recommended: rank === 1, diameter: 0.1 * rank })),
    mesh: { original_filename: 'embedded.inp', element_type_original: 'C3D8R',
      element_type_effective: 'C3D8_GEOMETRY_COMPATIBLE', node_count: 8, element_count: 1,
      precheck_status: 'PASS', sha256: 'mesh-sha', visualization: { status: 'available' } },
    progress: { job_status: {running:'CONFORMAL_RUNNING',failed:'FAILED',candidate:'CANDIDATES_READY',mesh:'MESH_READY',result:'VIEWER_READY'}[viewState], current_stage: 'CONFORMAL_RUNNING', attempt_count: 1,
      steps: [{ key: 'conformal', label: '共形阶段', status: 'RUNNING' }],
      events: [{ revision: 8, event: 'conformal_started', status: 'RUNNING' }],
      failure: { stage: 'CONFORMAL_RUNNING', message: '保留的失败诊断' } },
    result: { package_id: `${jobId}-attempt-0001`,
      core_output: { type: 'B31', path: 'core/conformal_b31.inp' }, ...records(...Object.keys(names)) },
    non_claims: ['NOT_FEA_VALIDATION']
  }
}

for (const state of ['candidate', 'mesh', 'running']) {
  test(`fusion scope notice in embedded ${state}`, async () => {
    const panel = await renderEmbeddedPanelModule()
    const { productScopeSummary } = await import('../src/utils/productJobView.js')
    const message = 'Full dual-space fusion has not been validated.'
    const code = 'NOT_FULL_DUAL_SPACE_FUSION'
    const baseline = ['NOT_MECHANICAL_PASS', 'NOT_FEA_VALIDATION', 'NOT_MANUFACTURING_VALIDATION', 'NOT_CLINICAL_VALIDATION']
    for (const occurrences of [0, 1, 2]) {
      const payload = embeddedPayload(state)
      payload.non_claims = [...baseline, 'PRIVATE_SYNTHETIC_DIAGNOSTIC']
      if (occurrences) payload.non_claims.push(code)
      if (occurrences === 2) payload.mesh.non_claims = [code]
      const html = renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'embedded' }))
      const notice = html.match(/<p[^>]*aria-label="Result scope"[^>]*>([^<]+)<\/p>/)?.[1]
      assert.ok(notice, 'missing rendered scope notice')
      for (const text of [productScopeSummary(payload).join(' '), notice]) {
        assert.equal(text.split(message).length - 1, Number(occurrences > 0))
        assert.match(text, /a mechanical pass, finite-element validation, manufacturing validation, clinical validation/)
        for (const hidden of [code, ...baseline, 'PRIVATE_SYNTHETIC_DIAGNOSTIC']) assert.ok(!text.includes(hidden))
      }
    }
  })
}

const embeddedCases = [
  ['candidate', html => {
    assert.match(html, /候选晶格（只读）/)
    assert.equal((html.match(/data-display-boundary="candidate-cell"/g) || []).length, 3)
    for (const rank of [1, 2, 3]) assert.match(html, new RegExp(`data-candidate-id="inverse_150w_000${rank}"`))
    assert.match(html, /class="status-pill">recommended · unconfirmed/)
    assert.match(html, /grid-template-columns:repeat\(3, minmax\(220px, 1fr\)\);gap:12px/)
    assert.match(html, /data-camera-position="3,3,3" data-camera-zoom="120"/)
  }],
  ['mesh', html => {
    assert.match(html, /输入网格（只读）/)
    assert.match(html, /embedded\.inp/)
    assert.match(html, /C3D8R · Units:/)
    assert.match(html, /8 nodes · 1 elements · precheck PASS/)
    assert.match(html, /height:calc\(100% - 130px\);min-height:360px/)
    assert.match(html, /data-display-boundary="mesh" data-mesh-status="available" data-view-mode="front"/)
    assert.match(html, /data-camera-position="10,10,10" data-camera-zoom="80"/)
  }],
  ['running', html => {
    assert.match(html, /Generating conformal geometry/)
    assert.doesNotMatch(html, /CONFORMAL_RUNNING|conformal_started/)
  }],
  ['failed', html => {
    assert.match(html, /Geometry generation failed/)
    assert.doesNotMatch(html, /保留的失败诊断|CONFORMAL_RUNNING/)
  }],
  ['result', (html, payload) => {
    assert.match(html, /Conformal Design Results/)
    assert.equal(html.includes(payload.result.package_id),false)
    assertActions(html,Object.keys(names))
    assert.doesNotMatch(html, /core\/conformal_b31|<a\b[^>]*\/files\//)
  }]
]

for (const [state, check] of embeddedCases) {
  test(`embedded ${state} renders the production route and keeps its default layout`, async () => {
    const panel = await renderEmbeddedPanelModule()
    const payload = embeddedPayload(state)
    const props = { payload, viewMode: 'front' }
    const html = renderToStaticMarkup(React.createElement(panel.default, props))
    assert.equal(html, renderToStaticMarkup(React.createElement(panel.default, { ...props, layoutMode: 'embedded' })))
    assert.match(html, /height:100%;overflow:auto;/)
    assert.doesNotMatch(html, /NOT_FEA_VALIDATION/)
    assert.doesNotMatch(html, /Technical details|Preview diagnostics|<pre/)
    assert.doesNotMatch(html, /class="product-job-panel"|product-candidate-grid|product-result-models|product-stl-card|product-cell-card/)
    if (state !== 'result') assert.doesNotMatch(html, /<a\b[^>]*href="[^"]*\/(?:files|scene|deliver)\//)
    check(html, payload)
  })
}

test('embedded unknown state fails closed even when candidate and result evidence is present', async () => {
  const panel = await renderEmbeddedPanelModule()
  for (const state of [undefined, null, 'unrecognized-state']) {
    const payload = embeddedPayload(state)
    const html = renderToStaticMarkup(React.createElement(panel.default, { payload }))
    assert.equal(html, '<div class="viewer-empty-state"><strong>未知 Product Job 状态</strong></div>')
    assert.doesNotMatch(html, /Technical details|Preview diagnostics|<pre/)
    assert.equal(html, renderToStaticMarkup(React.createElement(panel.default, { payload, layoutMode: 'embedded' })))
  }
})

test('both layouts verify role bindings and disable all saved-file actions for stale and inconsistent states', async () => {
  const panel=await renderPanelModule()
  const owner=embeddedPayload('result')
  const keys=Object.keys(names)
  owner.ranking={metric:'nmse'}
  owner.mesh.original_filename='C:/private/server/visible.inp'
  Object.assign(owner.candidates[0],{nmse:0.0123,relative_density:0.15,eligible:true})
  owner.result.warnings=['interface node set was not provided and was not inferred','PRIVATE_WARNING']
  owner.result.parameters={diameter:0.32,diameter_unit:'mm',diameter_source:'Normalized unit-cell diameter scaled by the mesh reference length'}
  for (const layoutMode of ['product-page','embedded']) {
    const render=(payload,props={})=>renderToStaticMarkup(React.createElement(panel.default,{payload,layoutMode,...props}))
    const good=render(owner)
    assert.doesNotMatch(good,/private\/server|PRIVATE_WARNING/)
    assert.match(good,/NMSE \(dimensionless, lower is better\): 0\.0123/)
    assert.match(good,/Result model strut diameter: 0\.32 mm/)
    assert.match(good,/No interface node set was supplied or inferred/)
    assertActions(good,keys)
    assertActions(render(owner,{stale:true}),[])
    assertActions(render(owner,{downloadsEnabled:false}),[])
    assert.doesNotMatch(render({...owner,progress:{job_status:'FAILED'}}),/aria-label="Downloads"|<canvas|<img/)
    const bad={...owner,result:{...owner.result,downloads:{...owner.result.downloads,
      unit_png:owner.result.downloads.unit_png.replace(owner.job_id,'job-AAAAAAAAAAAAAAAAAAAAAAAA')},
      download_identities:{...owner.result.download_identities,unit_stl:null}}}
    assertActions(render(bad),['conformal_inp'])
  }
})
