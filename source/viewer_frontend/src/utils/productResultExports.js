import { fullProductApiUrl, productJobUrl, productJobFileUrl, candidatePreviewUrl } from '../api/productViewerApi.js'
import { inspectProductStl, readProductStlBytes } from './productJobView.js'

const files = {
  unit_png: ['selected-unit-cell.png', 'image/png'],
  unit_stl: ['selected-unit-cell.stl', 'model/stl'],
  conformal_inp: ['conformal-lattice-b31.inp', 'text/plain']
}
export function displayBasename(value) {
  if (typeof value !== 'string') return ''
  return value.trim().split(/[\\/]/).pop()?.replace(/[\p{C}]/gu, '').slice(0, 180) || ''
}
export function inputName(payload) {
  return displayBasename(payload.mesh?.original_upload?.original_filename) || displayBasename(payload.mesh?.original_filename) || 'Design mesh'
}
export function fileNameFor(payload, kind) {
  if (!Object.hasOwn(files,kind)) throw new Error('Unsupported result type')
  return files[kind][0]
}
function fileIdentityError() {
  return Object.assign(new Error('The file does not match its saved record. Refresh this result.'), {code:'FILE_IDENTITY_MISMATCH'})
}
export async function verifyFileBytes(bytes, entry) {
  if (bytes.byteLength !== entry.size_bytes) throw fileIdentityError()
  if (!globalThis.crypto?.subtle) throw new Error('File verification is unavailable in this browser context.')
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256',bytes))
  if (Array.from(digest,b=>b.toString(16).padStart(2,'0')).join('') !== entry.sha256.toLowerCase()) throw fileIdentityError()
}
export function resultFileUrl(payload, key) {
  if (!payload.result?.package_id) throw new Error('No verified result is available for download')
  const expected = productJobFileUrl(payload.job_id, key)
  const url = payload.result.downloads?.[key]
  if (typeof url !== 'string' || url.split('?')[0] !== expected || !/^snapshot=[A-F0-9]{64}$/.test(url.split('?').slice(1).join('?'))) throw new Error('The file does not belong to this design')
  const entry = payload.result.download_identities?.[key]
  if (entry?.filename !== fileNameFor(payload,key) || !Number.isSafeInteger(entry.size_bytes) || entry.size_bytes < 1 || !/^[0-9a-f]{64}$/i.test(entry.sha256)) throw fileIdentityError()
  return url
}
export function sceneUrl(payload, role) {
  const url=payload.result?.scene?.[role]
  if(!['triangles','poster'].includes(role) || typeof url!=='string')throw fileIdentityError()
  const [path,query,...extra]=url.split('?')
  if(path!==productJobUrl(payload.job_id)+'/scene/'+role || extra.length || !/^snapshot=[A-F0-9]{64}$/.test(query))throw fileIdentityError()
  return fullProductApiUrl(url)
}
export async function readScene(payload,role,signal) {
  const response=await fetch(sceneUrl(payload,role),{signal,redirect:'error',cache:'no-store'})
  await checkFileResponse(response,signal)
  if(response.headers.get('content-type')?.split(';')[0] !== (role==='poster'?'image/webp':'application/octet-stream'))throw fileIdentityError()
  const bytes=await readProductStlBytes(response,{signal})
  await verifyFileBytes(bytes,{size_bytes:Number(response.headers.get('content-length')),sha256:(response.headers.get('etag')||'').replaceAll('"','')})
  return bytes
}
export function referenceMatches(payload, reference) {
  const expected=payload.mesh?.original_upload?.raw_sha256||payload.mesh?.sha256
  return Boolean(reference && reference.job_id === payload.job_id && reference.revision === payload.revision
    && payload.mesh?.mesh_binding_sha256 && reference.binding_sha256 === payload.mesh.mesh_binding_sha256
    && expected && reference.source_sha256===expected)
}
export function materialDiameter(parameters) {
  if (!(Number.isFinite(parameters?.diameter) && parameters.diameter > 0)
      || !['mm','m','µm'].includes(parameters.diameter_unit) || !parameters.diameter_source) return null
  return { value: parameters.diameter, unit: parameters.diameter_unit, source: parameters.diameter_source }
}

export function sceneVersion(reference, visible, opacity, clip) {
  return JSON.stringify([visible ? reference?.source_sha256 || 'loading' : null, opacity, clip.enabled, clip.axis, clip.position])
}

export function createCaptureGuard(canvas) {
  const initial=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio
  let invalid=false
  const current=()=>{
    const rect=canvas.getBoundingClientRect()
    invalid ||= Math.abs(rect.width-initial.width)>.1||Math.abs(rect.height-initial.height)>.1||window.devicePixelRatio!==dpr
    return !invalid
  }
  const resized=()=>{invalid=true}
  const observer=new ResizeObserver(current)
  observer.observe(canvas)
  window.addEventListener('resize',resized);window.addEventListener('fullscreenchange',resized)
  return {current,dispose(){observer.disconnect();window.removeEventListener('resize',resized);window.removeEventListener('fullscreenchange',resized)}}
}

export function runExportPreparation(prepare, {signal, timeoutMs=60000}={}) {
  return new Promise((resolve,reject)=>{
    const controller=new AbortController()
    let settled=false
    const finish=(callback,value)=>{
      if(settled)return
      settled=true;clearTimeout(timer);signal?.removeEventListener('abort',abort)
      callback(value)
    }
    const abort=()=>{const error=signal?.reason instanceof Error?signal.reason:new DOMException('File preparation cancelled','AbortError');controller.abort(error);finish(reject,error)}
    const timer=setTimeout(()=>{const error=new Error('Result preparation timed out. Please try again.');controller.abort(error);finish(reject,error)},timeoutMs)
    signal?.addEventListener('abort',abort,{once:true})
    if(signal?.aborted){abort();return}
    Promise.resolve().then(()=>{if(controller.signal.aborted)throw controller.signal.reason;return prepare(controller.signal)}).then(value=>finish(resolve,value),error=>{controller.abort(error);finish(reject,error)})
  })
}

function decodeImage(blob,signal) {
  return new Promise((resolve,reject)=>{
    if(typeof Image==='undefined'){reject(new Error('PNG decoding is unavailable'));return}
    const image=new Image(),url=URL.createObjectURL(blob)
    const cleanup=()=>{image.onload=null;image.onerror=null;signal.removeEventListener('abort',abort);URL.revokeObjectURL(url)}
    const abort=()=>{cleanup();image.src='';reject(signal.reason)}
    image.onload=()=>{const valid=image.naturalWidth>0&&image.naturalHeight>0;cleanup();valid?resolve():reject(new Error('Invalid PNG image'))}
    image.onerror=()=>{cleanup();reject(new Error('Invalid PNG image'))}
    signal.addEventListener('abort',abort,{once:true})
    if(signal.aborted){abort();return}
    image.src=url
  })
}

export function validatePngBlob(blob,{signal,timeoutMs=10000,decode=globalThis.createImageBitmap?.bind(globalThis)}={}) {
  return runExportPreparation(async current=>{
    const signature=new Uint8Array(await blob.slice(0,8).arrayBuffer())
    if(blob.size<33||![137,80,78,71,13,10,26,10].every((v,i)=>signature[i]===v))throw new Error('Invalid PNG image; download was not started')
    if(decode){
      try{const image=await decode(blob);image?.close?.()}
      catch{throw new Error('Invalid PNG image; download was not started')}
    }else await decodeImage(blob,current)
    return blob
  },{signal,timeoutMs})
}

async function readBounded(response, limit, signal) {
  const advertised = Number(response.headers.get('content-length'))
  if (advertised > limit) {response.body?.cancel().catch(()=>{});throw new Error('The file exceeds the current read limit')}
  if (!response.body?.getReader) {
    const buffer = await response.arrayBuffer()
    if (!buffer.byteLength || buffer.byteLength > limit) throw new Error('The file is empty or exceeds the current read limit')
    return new Uint8Array(buffer)
  }
  const reader = response.body.getReader(), chunks = []
  let total = 0
  const abort=()=>{reader.cancel(signal.reason).catch(()=>{})}
  signal?.addEventListener('abort',abort,{once:true})
  try {
    while (true) {
      const {done,value} = await reader.read()
      if (done) break
      total += value.byteLength
      if (signal?.aborted) throw signal.reason
      if (total > limit) throw new Error('The file exceeds the current read limit')
      chunks.push(value)
    }
  } finally { signal?.removeEventListener('abort',abort);reader.cancel().catch(()=>{});reader.releaseLock() }
  if (!total) throw new Error('The file is empty')
  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk,offset); offset += chunk.byteLength }
  return bytes
}

export async function checkFileResponse(response, signal) {
  if (response.ok) return
  let code
  if ((response.headers.get('content-type') || '').split(';')[0] === 'application/json') {
    try { code = JSON.parse(new TextDecoder().decode(await readBounded(response,8192,signal))).code }
    catch { /* A malformed error envelope still fails closed. */ }
  } else response.body?.cancel().catch(()=>{})
  if (['JOB_CONTRACT_INVALID','PACKAGE_CONTRACT_INVALID','FILE_IDENTITY_MISMATCH'].includes(code)) throw fileIdentityError()
  if (code === 'FILE_MISSING') throw new Error('The requested file is missing. Refresh this result.')
  throw new Error(`File request failed (${response.status}). Refresh the result before downloading.`)
}

export async function readResultFile(payload, key, {fetcher=fetch, signal, maxBytes,decodePng=validatePngBlob,timeoutMs=60000}={}) {
  const url = resultFileUrl(payload,key)
  const entry = payload.result.download_identities[key]
  return runExportPreparation(async current=>{
  const response = await fetcher(fullProductApiUrl(url),{signal:current,redirect:'error',cache:'no-store'})
  await checkFileResponse(response,current)
  const type = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase()
  if (type !== files[key][1]) {response.body?.cancel().catch(()=>{});throw new Error('Unexpected file type. Refresh the result before downloading.')}
  const bytes = await readBounded(response,Math.min(maxBytes ?? entry.size_bytes,entry.size_bytes),current)
  await verifyFileBytes(bytes,entry)
  if (key === 'unit_png' && ![137,80,78,71,13,10,26,10].every((v,i)=>bytes[i]===v)) throw new Error('Invalid PNG image')
  if (key === 'unit_stl') {
    const triangles=bytes.byteLength>=84?new DataView(bytes.buffer).getUint32(80,true):0
    // A valid saved binary STL remains downloadable beyond the renderer's
    // triangle budget; preview limits do not change the model file role.
    if(!triangles||84+50*triangles!==bytes.byteLength)inspectProductStl(bytes.buffer)
  }
  if (key === 'conformal_inp' && new TextDecoder().decode(bytes.slice(0,1000)).trimStart()[0] !== '*') throw new Error('Invalid INP file')
  const blob=new Blob([bytes],{type})
  return key==='unit_png'?decodePng(blob,{signal:current}):blob
  },{signal,timeoutMs})
}

export function startBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob), link = document.createElement('a')
  link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove()
  window.setTimeout(()=>URL.revokeObjectURL(url),30000)
}

export function canvasPng(canvas) {
  return new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?.size ? resolve(blob) : reject(new Error('Image creation failed; download was not started')),'image/png'))
}

export async function captureCanvas(canvas, labels, {background,legend,footer=false,stillCurrent=()=>true}={}) {
  if (!(canvas?.width > 0 && canvas.height > 0) || !stillCurrent()) throw new Error('The model is not ready for capture')
  const scale=Math.max(1,canvas.width/900), margin=14*scale, line=23*scale
  const image = document.createElement('canvas'); image.width=canvas.width; image.height=canvas.height+(footer?line*labels.length+margin*2:0)
  const ctx=image.getContext('2d')
  if (!ctx) throw new Error('Image creation is unavailable')
  ctx.fillStyle=background || getComputedStyle(canvas).backgroundColor || '#ffffff'; ctx.fillRect(0,0,image.width,image.height)
  ctx.drawImage(canvas,0,0)
  if(legend){
    const box=legend.getBoundingClientRect(), frame=canvas.getBoundingClientRect(), style=getComputedStyle(legend)
    const scale=image.width/frame.width, x=(box.left-frame.left)*scale, y=(box.top-frame.top)*scale
    ctx.fillStyle=style.backgroundColor;ctx.fillRect(x,y,box.width*scale,box.height*scale)
    for(const span of legend.querySelectorAll('span')){
      const rect=span.getBoundingClientRect(), textStyle=getComputedStyle(span), px=parseFloat(textStyle.fontSize)*scale
      ctx.font=`${textStyle.fontWeight} ${px}px ${textStyle.fontFamily}`;ctx.fillStyle=textStyle.color;ctx.textBaseline='top'
      let text='', row=0
      const maxWidth=rect.width*scale, lineHeight=parseFloat(textStyle.lineHeight)*scale||px*1.5
      for(const char of span.textContent){
        if(text&&ctx.measureText(text+char).width>maxWidth){ctx.fillText(text,(rect.left-frame.left)*scale,(rect.top-frame.top)*scale+row*lineHeight);row++;text=''}
        text+=char
      }
      ctx.fillText(text,(rect.left-frame.left)*scale,(rect.top-frame.top)*scale+row*lineHeight)
    }
  }else{
  ctx.font=`${14*scale}px Arial, Helvetica, sans-serif`
  const boxWidth=Math.min(image.width-margin*2,Math.max(...labels.map(label=>ctx.measureText(label).width))+margin*2)
  ctx.fillStyle='rgba(255,255,255,0.92)'; ctx.fillRect(margin,footer?canvas.height:image.height-margin-line*labels.length-margin,boxWidth,line*labels.length+margin)
  ctx.fillStyle='#233e50'
  labels.forEach((label,i)=>ctx.fillText(label,margin*1.6,image.height-margin-line*(labels.length-i-1)-margin*.3))
  }
  const blob=await canvasPng(image)
  if (!stillCurrent()) throw new Error('The design or view changed; download was not started')
  return blob
}


export async function readCandidatePreview(payload,candidate,{signal,fetcher=fetch,decodePng=validatePngBlob}={}) {
  const response=await fetcher(fullProductApiUrl(candidatePreviewUrl(payload,candidate)),{signal,redirect:'error',cache:'no-store'})
  await checkFileResponse(response,signal)
  if(!(response.headers.get('content-type')||'').toLowerCase().startsWith('image/png'))throw new Error('The standard unit-cell preview could not be verified. Refresh the Job.')
  const bytes=await readBounded(response,20*1024*1024,signal)
  return decodePng(new Blob([bytes],{type:'image/png'}),{signal})
}
