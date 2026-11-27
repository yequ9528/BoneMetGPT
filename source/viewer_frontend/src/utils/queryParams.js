import { isSafePackageId } from './urlGuards.js'
import { isSafeProductJobId } from './productJobView.js'

export function readInitialViewerQuery(search) {
  const params = new URLSearchParams(search)
  const mode = params.get('mode')
  const structureId = params.get('structure_id')
  const taskId = params.get('task_id')
  const candidateIndex = params.get('candidate_index') || '0'
  const packageId = params.get('package_id') || ''
  const jobId = params.get('job_id') || ''

  if (mode === 'prediction' && taskId) {
    return { mode, taskId, candidateIndex }
  }
  if ((mode === 'database' || mode === 'database_v2') && structureId) {
    return { mode, structureId }
  }
  if (mode === 'conformal') {
    return {
      mode,
      packageId,
      error: packageId && !isSafePackageId(packageId)
        ? 'Invalid conformal package_id'
        : ''
    }
  }
  if (mode === 'product') {
    return {
      mode,
      jobId,
      error: jobId && !isSafeProductJobId(jobId)
        ? 'Invalid product job_id'
        : ''
    }
  }
  return { mode: 'database', structureId: '1' }
}
