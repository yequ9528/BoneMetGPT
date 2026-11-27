import assert from 'node:assert/strict'
import test from 'node:test'
import { createHash, webcrypto } from 'node:crypto'
import { fileNameFor, readResultFile, resultFileUrl, referenceMatches, materialDiameter } from '../src/utils/productResultExports.js'
import * as exportsApi from '../src/utils/productResultExports.js'

const pngBytes = new Uint8Array(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMwaNjwHwAEpAJg4Dr5PAAAAABJRU5ErkJggg==', 'base64'))
if (!globalThis.crypto) globalThis.crypto = webcrypto

const jobId = 'job-1234567890ABCDEF12345678'
const snapshot = 'AC'.repeat(32)
const jobPath = `/api/viewer/product/jobs/${jobId}`
const file = key => `${jobPath}/deliver/${key}?snapshot=${snapshot}`
const scene = role => `${jobPath}/scene/${role}?snapshot=${snapshot}`
const signature = (filename,bytes) => ({filename,size_bytes:bytes.byteLength,sha256:createHash('sha256').update(bytes).digest('hex')})
const stlBytes = new Uint8Array(134)
const facet = new DataView(stlBytes.buffer)
facet.setUint32(80, 1, true)
facet.setFloat32(92, 1, true) // normal Z
facet.setFloat32(108, 1, true) // second vertex X
facet.setFloat32(124, 1, true) // third vertex Y
const roles = {
  unit_png: ['selected-unit-cell.png', 'image/png', pngBytes],
  unit_stl: ['selected-unit-cell.stl', 'model/stl', stlBytes],
  conformal_inp: ['conformal-lattice-b31.inp', 'text/plain', Buffer.from('*NODE\n1,0,0,0\n')]
}
const payload = { job_id: jobId, revision: 11,
  mesh: { original_filename: 'selected_hex.inp', original_upload: { original_filename: 'yaozhui5.inp' }, sha256:'source', mesh_binding_sha256: 'binding' },
  result: { package_id: `${jobId}-attempt-0001`,
    downloads: Object.fromEntries(Object.keys(roles).map(key => [key,file(key)])),
    download_identities: Object.fromEntries(Object.entries(roles).map(([key,[name,,bytes]]) => [key, signature(name,bytes)])),
    scene: {poster:scene('poster'), triangles:scene('triangles')}
  }
}
const decodePng = blob => exportsApi.validatePngBlob(blob, {decode:async()=>({close(){}})})

test('only the three owner-bound snapshot URLs deliver files, internal scene roles stay separate', () => {
  for (const [key, [filename]] of Object.entries(roles)) {
    assert.equal(fileNameFor(payload,key), filename)
    assert.equal(resultFileUrl(payload,key), file(key))
    for (const wrong of [file(key).replace(jobId,'job-AAAAAAAAAAAAAAAAAAAAAAAA'), file(key).split('?')[0],
      file(key).replace(snapshot,snapshot.toLowerCase()), file(key)+'&extra=x', scene('poster'),
      file(key).replace('/deliver/', '/files/'), 'https://foreign.example' + file(key)]) {
      assert.throws(() => resultFileUrl({...payload,result:{...payload.result,downloads:{[key]:wrong}}},key))
    }
  }
  for (const role of ['source_mesh','view','metadata','run_summary','package_metadata','geometry_ledger','source_element_map','stl','png','b31','poster','triangles']) {
    assert.throws(() => fileNameFor(payload,role), /Unsupported result type/)
    assert.throws(() => resultFileUrl(payload,role), /Invalid product file_key/)
  }
  for (const role of ['poster','triangles']) {
    assert.ok(exportsApi.sceneUrl(payload,role).endsWith(scene(role)))
    for (const wrong of [file('unit_png'), scene(role).replace(jobId,'job-AAAAAAAAAAAAAAAAAAAAAAAA'),
      scene(role).replace('/'+role+'?', '/'+(role==='poster'?'triangles':'poster')+'?'), scene(role).split('?')[0], scene(role)+'?other']) {
      assert.throws(() => exportsApi.sceneUrl({...payload,result:{...payload.result,scene:{[role]:wrong}}},role), {code:'FILE_IDENTITY_MISMATCH'})
    }
  }
})

test('each delivered role verifies MIME, filename, byte length and SHA256 before returning original bytes', async () => {
  for (const [key,[filename,type,bytes]] of Object.entries(roles)) {
    const fetcher = async (url, options) => {
      assert.ok(url.endsWith(file(key)))
      assert.equal(options.redirect,'error')
      assert.equal(options.cache,'no-store')
      return new Response(bytes, {headers:{'content-type':type}})
    }
    const blob = await readResultFile(payload,key,{fetcher,decodePng})
    assert.equal(blob.type,type)
    assert.deepEqual(Buffer.from(await blob.arrayBuffer()),Buffer.from(bytes))
    for (const identity of [undefined, { ...signature(filename,bytes),filename:'private-source.json' },
      { ...signature(filename,bytes),size_bytes:bytes.length+1 }, { ...signature(filename,bytes),sha256:'0'.repeat(64) }]) {
      const wrong = {...payload,result:{...payload.result,download_identities:{[key]:identity}}}
      await assert.rejects(readResultFile(wrong,key,{fetcher,decodePng}),{code:'FILE_IDENTITY_MISMATCH'})
    }
    await assert.rejects(readResultFile(payload,key,{fetcher:async()=>new Response(bytes,{headers:{'content-type':'text/html'}})}),/Unexpected file type/)
  }
})

test('PNG is fetched as original bytes; JSON errors never become saved files', async () => {
  const blob = await readResultFile(payload, 'unit_png', { decodePng, fetcher: async () => new Response(pngBytes, { headers: { 'Content-Type': 'image/png' } }) })
  assert.deepEqual(new Uint8Array(await blob.arrayBuffer()), pngBytes)
  for (const response of [new Response('{"error":"bad"}', {status:400}), new Response('{"error":"bad"}', {headers:{'Content-Type':'application/json'}}), new Response('not a png', {headers:{'Content-Type':'image/png'}})]) {
    await assert.rejects(readResultFile(payload, 'unit_png', {fetcher:async()=>response}))
  }
})

test('matching error bytes cannot masquerade as a supported format and package failures suppress raw details', async () => {
  const bytes = Buffer.from('{"status":"error","message":"PRIVATE_RAW_FAILURE"}')
  for (const [key,[name,type]] of Object.entries(roles)) {
    const fixture = {...payload,result:{...payload.result,download_identities:{[key]:signature(name,bytes)}}}
    await assert.rejects(readResultFile(fixture,key,{fetcher:async()=>new Response(bytes,{headers:{'content-type':type}})}))
    await assert.rejects(readResultFile(fixture,key,{fetcher:async()=>new Response('{"code":"PACKAGE_CONTRACT_INVALID","message":"PRIVATE_RAW_FAILURE"}',
      {status:409,headers:{'content-type':'application/json'}})}), error=>error.code==='FILE_IDENTITY_MISMATCH' && !error.message.includes('PRIVATE_RAW'))
  }
})

test('PNG decode validates the original blob and releases the decoded image', async () => {
  let closed = 0
  const blob = new Blob([pngBytes], {type:'image/png'})
  assert.equal(await exportsApi.validatePngBlob(blob, {decode:async()=>({close(){closed++}})}), blob)
  assert.equal(closed,1)
  await assert.rejects(exportsApi.validatePngBlob(blob, {decode:async()=>{throw new Error('decode failed')}}), /PNG/)
})

test('late PNG decode is released after timeout and cannot succeed', async () => {
  let finish, closed = 0
  const blob = new Blob([pngBytes], {type:'image/png'})
  const promise = exportsApi.validatePngBlob(blob, {timeoutMs:5, decode:()=>new Promise(resolve=>{finish=resolve})})
  await assert.rejects(promise, /timed out/)
  finish({close(){closed++}})
  await new Promise(resolve=>setImmediate(resolve))
  assert.equal(closed,1)
})

test('file deadline aborts a stalled request and releases the caller', async () => {
  let signal
  await assert.rejects(readResultFile(payload,'unit_png',{timeoutMs:5,fetcher:async(_url,options)=>{signal=options.signal;return new Promise(()=>{})}}), /timed out/)
  assert.equal(signal.aborted,true)
})

test('rejected response headers cancel the unread body and its request', async()=>{
  for(const headers of [{'content-length':'999','content-type':'image/png'},{'content-type':'application/json'}]){
    let cancelled=false,signal
    const body=new ReadableStream({cancel(){cancelled=true}})
    await assert.rejects(readResultFile(payload,'unit_png',{maxBytes:10,fetcher:async(_url,options)=>{signal=options.signal;return new Response(body,{headers})}}))
    assert.equal(cancelled,true)
    assert.equal(signal.aborted,true)
  }
})

test('a signature-only or truncated PNG never becomes a download', async () => {
  for(const bytes of [pngBytes.slice(0,8),pngBytes.slice(0,40)]){
    const fixture={...payload,result:{...payload.result,download_identities:{unit_png:signature('selected-unit-cell.png',bytes)}}}
    let decodeCalls = 0
    const decodePng = blob => exportsApi.validatePngBlob(blob,{decode:async()=>{decodeCalls++; throw new Error('Incomplete image')}})
    await assert.rejects(readResultFile(fixture,'unit_png',{fetcher:async()=>new Response(bytes,{headers:{'content-type':'image/png'}}),decodePng}), /PNG/)
    assert.equal(decodeCalls,bytes.length<33?0:1)
  }
})

test('capture guard ignores internal bitmap scaling but permanently rejects an external resize', () => {
  const oldWindow=globalThis.window, oldObserver=globalThis.ResizeObserver
  let changed, removed=0, width=200
  globalThis.window={devicePixelRatio:2,addEventListener(){},removeEventListener(){removed++}}
  globalThis.ResizeObserver=class {constructor(callback){changed=callback}observe(){}disconnect(){removed++}}
  try{
    const canvas={width:400,height:400,getBoundingClientRect:()=>({width,height:200})}
    const guard=exportsApi.createCaptureGuard(canvas)
    changed();assert.equal(guard.current(),true)
    canvas.width=1200;canvas.height=1200;assert.equal(guard.current(),true)
    width=201;changed();width=200;assert.equal(guard.current(),false)
    guard.dispose();assert.equal(removed,3)
    const second=exportsApi.createCaptureGuard(canvas)
    globalThis.window.devicePixelRatio=3;assert.equal(second.current(),false)
    second.dispose()
  }finally{globalThis.window=oldWindow;globalThis.ResizeObserver=oldObserver}
})

test('bounded reads and reference identities reject stale or foreign content', async () => {
  await assert.rejects(readResultFile(payload, 'conformal_inp', {maxBytes:3, fetcher:async()=>new Response(roles.conformal_inp[2], {headers:{'Content-Type':'text/plain'}})}), /read limit/)
  const reference = { job_id:jobId, revision:11, binding_sha256:'binding', source_sha256:'source', status:'available' }
  assert.equal(referenceMatches(payload,reference),true)
  assert.equal(referenceMatches({...payload,mesh:{}},{...reference,binding_sha256:undefined}),false)
  assert.equal(referenceMatches({...payload,mesh:{mesh_binding_sha256:'binding'}},reference),false)
  assert.equal(referenceMatches(payload,{...reference,revision:10}),false)
  assert.equal(referenceMatches(payload,{...reference,job_id:'job-AAAAAAAAAAAAAAAAAAAAAAAA'}),false)
})

test('result diameter never acquires millimetres from normalized reference data', () => {
  assert.equal(materialDiameter({diameter:.627,diameter_source:'preview'}),null)
  assert.deepEqual(materialDiameter({diameter:.627,diameter_unit:'mm',diameter_source:'saved record'}),{value:.627,unit:'mm',source:'saved record'})
})

test('saved binary STL download does not inherit the renderer triangle budget',async()=>{
  const count=1500001, bytes=new Uint8Array(84+50*count)
  new DataView(bytes.buffer).setUint32(80,count,true)
  const fixture={...payload,result:{...payload.result,download_identities:{unit_stl:signature('selected-unit-cell.stl',bytes)}}}
  const blob=await readResultFile(fixture,'unit_stl',{fetcher:async()=>new Response(bytes,{headers:{'content-type':'model/stl'}})})
  assert.equal(blob.size,bytes.length)
})

test('internal scene reads bind role, content type, length and ETag and reject a stale snapshot', async t => {
  const mockFetch = t.mock.method(globalThis,'fetch')
  for (const [role,type,bytes] of [
    ['poster','image/webp',Buffer.from('UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA','base64')],
    ['triangles','application/octet-stream',new Uint8Array(new Float32Array([0,0,0,1,0,0,0,1,0]).buffer)]
  ]) {
    const headers={'content-type':type,'content-length':String(bytes.length),etag:'"'+signature('',bytes).sha256+'"'}
    const signal=new AbortController().signal
    mockFetch.mock.mockImplementation(async(url,options)=>{
      assert.ok(url.endsWith(scene(role)))
      assert.equal(options.cache,'no-store')
      assert.equal(options.redirect,'error')
      assert.equal(options.signal,signal)
      return new Response(bytes,{headers})
    })
    assert.deepEqual(Buffer.from(await exportsApi.readScene(payload,role,signal)),Buffer.from(bytes))
    for (const wrong of [{'content-type':'text/plain'},{'content-length':String(bytes.length+1)},{etag:'"'+ '0'.repeat(64)+'"'}]) {
      mockFetch.mock.mockImplementation(async()=>new Response(bytes,{headers:{...headers,...wrong}}))
      await assert.rejects(exportsApi.readScene(payload,role,signal),{code:'FILE_IDENTITY_MISMATCH'})
    }
    mockFetch.mock.mockImplementation(async()=>new Response('{"code":"FILE_IDENTITY_MISMATCH","message":"PRIVATE"}',
      {status:409,headers:{'content-type':'application/json'}}))
    await assert.rejects(exportsApi.readScene(payload,role,signal),error=>error.code==='FILE_IDENTITY_MISMATCH' && !error.message.includes('PRIVATE'))
  }
})
