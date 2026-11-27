const SAFE_PACKAGE_ID_RE = /^[A-Za-z0-9_.-]+$/

export function isSafePackageId(value) {
  return Boolean(
    value &&
    value !== '.' &&
    value !== '..' &&
    !value.includes('..') &&
    !value.includes('/') &&
    !value.includes('\\') &&
    !value.includes(':') &&
    SAFE_PACKAGE_ID_RE.test(value)
  )
}

export function isSafeApiRelativeUrl(value) {
  return Boolean(
    typeof value === 'string' &&
    (value.startsWith('/api/') || value.startsWith('/artifacts/')) &&
    !value.includes('..') &&
    !value.includes('\\') &&
    !/^[A-Za-z]:/.test(value)
  )
}
