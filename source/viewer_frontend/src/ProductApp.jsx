import { useEffect, useMemo, useState } from 'react'
import './App.css'
import './index.css'
import { fetchProductJob } from './api/productViewerApi'
import ProductJobPanel from './panels/ProductJobPanel'
import { createProductJobReader, getProductPageState, isSafeProductJobId, PRODUCT_JOB_POLL_MS, productReadFailure, shouldPollProductJob } from './utils/productJobView'

// Only this entry owns the viewport; shared Viewer styles and components stay unchanged.
const PRODUCT_PAGE_STYLES = `
html.latticegpt-product-page-host,
html.latticegpt-product-page-host body,
html.latticegpt-product-page-host #root { height: 100%; min-height: 0; overflow: hidden; }
html.latticegpt-product-page-host body { display: block; min-width: 0; }
.viewer-shell.product-viewer-page {
  display: block; width: 100%; height: 100vh; height: 100dvh; min-height: 0;
  padding: 14px 20px; box-sizing: border-box; overflow-y: auto; overflow-x: hidden;
  background: #eef1f4; color: #203843; font-size: 16px; line-height: 1.5;
}
.product-viewer-page :is(h1,h2,h3,p,dl,dd,ul) { margin: 0; }
.product-viewer-page :is(h1,h2,h3) { color: #183641; line-height: 1.5; }
.product-viewer-page h1 { font-size: 24px; }
.product-viewer-page :is(h2,h3) { font-size: 18px; }
.product-viewer-page :is(p,span,strong,a,button,summary,dt,dd,li,th,td):not(.result-workspace *) { font-size: inherit; }
.product-viewer-page .product-secondary { font-size: 14px; color: #405965; }
.product-viewer-page :is(code,pre,.mono-value) { font-size: 13px; user-select: text; }
.product-viewer-page * { min-width: 0; overflow-wrap: anywhere; box-sizing: border-box; }
.product-viewer-page :is(pre,code) { white-space: pre-wrap; }
.product-viewer-page :is(.product-job-panel,.section-card,.section-card-body,.section-card-header,.product-result-models) {
  height: auto; max-height: none; min-height: 0; overflow: visible;
}
.product-viewer-page :is(.section-card-header,.section-card-body) { padding: 10px 12px; }
.product-viewer-page .product-heading { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px 18px; }
.product-viewer-page .product-heading p { font-size: 14px; }
.product-viewer-page .product-status { margin: 8px 0; color: #15594f; }
.product-viewer-page .product-scope { max-width: 105ch; font-size: 14px; margin: 12px 0; color: #405965; }
.product-viewer-page .product-input-summary { margin-top: 16px; display: flex; flex-wrap: wrap; column-gap: 24px; row-gap: 8px; }
.product-viewer-page .product-input-summary > div { flex: 1 1 340px; }
.product-viewer-page .product-target-desktop { margin: 4px 0 8px; }
.product-viewer-page .product-target-values { display: flex; flex-wrap: wrap; column-gap: 16px; row-gap: 2px; }
.product-viewer-page .product-target-values > div { display: flex; gap: 5px; }
.product-viewer-page .product-target-mobile { display: none; }
.product-viewer-page .product-result-models { display: grid; grid-template-columns: minmax(0,2fr) minmax(0,1fr); gap: 12px; margin-top: 8px; align-items: start; }
.product-viewer-page .product-canvas { width: 100%; overflow: hidden; position: relative; }
.product-viewer-page .product-stl-canvas { height: 380px; }
.product-viewer-page .product-canvas canvas { display: block; }
.product-viewer-page .product-preview-status { padding: 16px; color: #405965; }
.product-viewer-page .product-model-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 12px; }
.product-viewer-page button { color: #15594f; background: #fff; border: 1px solid #43887f; border-radius: 6px; padding: 5px 12px; }
.product-viewer-page button:disabled { color: #69757b; border-color: #abb7bd; cursor: default; }
.product-viewer-page .product-files { margin-top: 12px; display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 12px; }
.product-viewer-page .product-files .section-card { padding: 12px; }
.product-viewer-page .product-files a { display: inline-block; text-decoration: underline; }
.product-viewer-page summary { cursor: pointer; color: #15594f; }
.product-viewer-page .product-read-error { padding: 12px; margin: 8px 0; border: 2px solid #995b18; background: #fff6e8; }
.product-viewer-page :is(button,summary,a):focus-visible { outline: 3px solid #248f86; outline-offset: 3px; }
.product-viewer-page:focus-visible { outline: 2px solid #248f86; outline-offset: -2px; }
@media (max-width: 840px) {
  .viewer-shell.product-viewer-page { padding: 12px; }
  .product-viewer-page h1 { font-size: 22px; }
  .product-viewer-page :is(.product-result-models,.product-files) { grid-template-columns: minmax(0,1fr); }
  .product-viewer-page .product-input-summary { display: block; }
  .product-viewer-page .product-target-desktop { display: none; }
  .product-viewer-page .product-target-mobile { display: block; margin: 6px 0; }
  .product-viewer-page .product-target-mobile table { width: 100%; border-collapse: collapse; text-align: left; }
  .product-viewer-page .product-target-mobile :is(td,th) { padding: 3px 6px; }
  .product-viewer-page .product-stl-canvas { height: 320px; }
}
`

export default function ProductApp() {
  const jobId = useMemo(() => new URLSearchParams(window.location.search).get('job_id') || '', [])
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [slow, setSlow] = useState(false)
  const [readAt, setReadAt] = useState(null)
  const [readJob] = useState(() => createProductJobReader(fetchProductJob))

  useEffect(() => {
    const host = document.documentElement
    const hostClass = 'latticegpt-product-page-host'
    const added = !host.classList.contains(hostClass)
    if (added) host.classList.add(hostClass)
    return () => { if (added) host.classList.remove(hostClass) }
  }, [])

  useEffect(() => {
    setPayload(null)
    setError(null)
    setReadAt(null)
    setSlow(false)
    if (!isSafeProductJobId(jobId)) { setLoading(false); return undefined }
    let active = true
    let timer
    let slowTimer
    let successfulPayload = null
    let requestVersion = 0
    const load = async () => {
      const version = ++requestVersion
      setLoading(true)
      setSlow(false)
      slowTimer = window.setTimeout(() => { if (active && version === requestVersion) setSlow(true) }, 15000)
      try {
        const next = await readJob(jobId)
        if (!active || version !== requestVersion) return
        successfulPayload = next
        setPayload(next)
        setError(null)
        setReadAt(new Date().toLocaleString('en-GB', { timeZone: 'Asia/Shanghai' }))
        if (getProductPageState(next) !== 'unknown' && shouldPollProductJob(next)) timer = window.setTimeout(load, PRODUCT_JOB_POLL_MS)
      } catch (failure) {
        if (!active || version !== requestVersion) return
        const nextError = productReadFailure(failure, successfulPayload)
        setError(nextError)
        if (!nextError.retainSnapshot) { successfulPayload = null; setPayload(null) }
      } finally {
        window.clearTimeout(slowTimer)
        if (active && version === requestVersion) { setLoading(false); setSlow(false) }
      }
    }
    load()
    return () => {
      active = false
      requestVersion += 1
      window.clearTimeout(timer)
      window.clearTimeout(slowTimer)
    }
  }, [jobId, readJob])

  return <>
    <style>{PRODUCT_PAGE_STYLES}</style>
    <main className="viewer-shell product-viewer-page" tabIndex={0} aria-label="Read-only conformal design results">
      {getProductPageState(payload) !== 'result' && <header className="product-heading"><h1>Conformal design results</h1><p>Read-only saved results.</p></header>}
      {!isSafeProductJobId(jobId) && <p role="status">A valid full Job ID is required. Open this page from a result link.</p>}
      {loading && !payload && <p role="status">Reading saved task records…</p>}
      {slow && <p role="status">The read is taking longer than expected. Still waiting for a response; avoid duplicate actions.</p>}
      {error && <section className="product-read-error" role="alert">
        <strong>{error.message}</strong>
        <details><summary>Read status</summary><p>Support code: <code>{error.code}</code></p><p>Last successful read: {readAt ? `${readAt} (Beijing time, UTC+08:00)` : 'No successful read'}</p></details>
      </section>}
      {payload && <ProductJobPanel payload={payload} viewMode="iso" layoutMode="product-page" downloadsEnabled={!error} stale={Boolean(error?.retainSnapshot)} />}
    </main>
  </>
}
