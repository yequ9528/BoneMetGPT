import assert from 'node:assert/strict'
import test from 'node:test'

import {
  fullProductApiUrl,
  fetchProductJob,
  productJobFileUrl,
  productJobUrl
} from '../src/api/productViewerApi.js'


const jobId = 'job-1234567890ABCDEF12345678'

test('preserves safe typed errors and distinguishes network and non-JSON failures', async () => {
  const previous = globalThis.fetch
  try {
    for (const code of ['JOB_NOT_FOUND', 'JOB_CONTRACT_INVALID', 'PACKAGE_CONTRACT_INVALID', 'FILE_MISSING', 'FILE_IDENTITY_MISMATCH', 'PRODUCT_VIEWER_INTERNAL_ERROR']) {
      globalThis.fetch = async () => ({ ok: false, json: async () => ({ status: 'error', code, job_id: jobId, message: 'secret C:/private/path' }) })
      await assert.rejects(fetchProductJob(jobId), e => e.code === code && e.jobId === jobId && !e.message.includes('secret'))
    }
    globalThis.fetch = async () => { throw new Error('network detail') }
    await assert.rejects(fetchProductJob(jobId), e => e.code === 'VIEWER_UNREACHABLE')
    globalThis.fetch = async () => ({ ok: false, json: async () => { throw new Error('html') } })
    await assert.rejects(fetchProductJob(jobId), e => e.code === 'INVALID_VIEWER_RESPONSE')
  } finally { globalThis.fetch = previous }
})

test('constructs only exact product job and file URLs', () => {
  assert.equal(productJobUrl(jobId), `/api/viewer/product/jobs/${jobId}`)
  for (const key of ['unit_png', 'unit_stl', 'conformal_inp']) {
    const expected = `/api/viewer/product/jobs/${jobId}/deliver/${key}`
    assert.equal(productJobFileUrl(jobId, key), expected)
    assert.equal(new URL(fullProductApiUrl(expected)).pathname, expected)
  }
})

test('rejects latest, traversal, unknown file keys and arbitrary absolute URLs', () => {
  for (const invalid of ['latest', '../latest', 'job-lowercase123456789012']) {
    assert.throws(() => productJobUrl(invalid), /Invalid product job_id/)
  }
  for (const retired of ['unknown', 'png', 'stl', 'b31', 'source_mesh', 'metadata', 'run_summary', 'package_metadata', 'geometry_ledger', 'source_element_map', 'view', 'poster', 'triangles']) {
    assert.throws(() => productJobFileUrl(jobId, retired), /Invalid product file_key/)
  }
  assert.throws(() => fullProductApiUrl('http://secret/path'), /relative product API/)
})

test('a successful response for a different task fails identity validation', async () => {
  const previous = globalThis.fetch
  try {
    globalThis.fetch = async () => ({ok:true,json:async()=>({status:'success',data:{job_id:'job-AAAAAAAAAAAAAAAAAAAAAAAA'}})})
    await assert.rejects(fetchProductJob(jobId), error => error.code === 'JOB_CONTRACT_INVALID')
  } finally { globalThis.fetch = previous }
})
