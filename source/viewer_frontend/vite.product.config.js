import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ isPreview }) => {
  const config = {
    plugins: [react()],
    build: { rollupOptions: { input: 'product.html' } }
  }
  if (isPreview) {
    const origin = new URL(process.env.LATTICEGPT_PRODUCT_VIEWER_ORIGIN)
    if (origin.protocol !== 'http:' || origin.href !== origin.origin + '/') {
      throw new Error('A bound HTTP Viewer origin is required')
    }
    config.preview = {
      strictPort: true,
      cors: false,
      proxy: { '/api/viewer/product/': { target: origin.origin } }
    }
  }
  return config
})
