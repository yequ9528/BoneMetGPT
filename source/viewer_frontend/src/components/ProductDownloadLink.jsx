/* eslint-disable react/prop-types */
import { fullProductApiUrl } from '../api/productViewerApi'

export default function ProductDownloadLink({ href, children, className }) {
  if (typeof href !== 'string' || !href.startsWith('/api/viewer/product/jobs/')) return null
  return (
    <a className={['download-link', className].filter(Boolean).join(' ')} href={fullProductApiUrl(href)} download>
      {children}
    </a>
  )
}
