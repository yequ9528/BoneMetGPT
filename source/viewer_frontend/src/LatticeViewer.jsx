/* eslint-disable react/prop-types, react/no-unknown-property */
import { useLoader, useThree } from '@react-three/fiber'
import { OrbitControls } from '@react-three/drei'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader'
import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { computeProductCameraFit, productCameraState, inspectProductStl, readProductStlBytes, validateProductGeometry, productStlError } from './utils/productJobView'

function CameraController({ geometry, viewMode }) {
  const { camera, size } = useThree()
  const controlsRef = useRef()

  useEffect(() => {
    if (!geometry) return

    geometry.computeBoundingBox()
    const box = geometry.boundingBox

    const center = new THREE.Vector3()
    box.getCenter(center)

    const bboxSize = new THREE.Vector3()
    box.getSize(bboxSize)

    const maxDim = Math.max(bboxSize.x, bboxSize.y, bboxSize.z)
    const distance = maxDim * 2.5 || 5

    let position = [distance, distance, distance]

    if (viewMode === 'front') {
      position = [0, 0, distance]
    } else if (viewMode === 'side') {
      position = [distance, 0, 0]
    } else if (viewMode === 'top') {
      position = [0, distance, 0]
    } else if (viewMode === 'iso' || viewMode === 'reset') {
      position = [distance, distance, distance]
    }

    camera.position.set(position[0], position[1], position[2])
    camera.lookAt(0, 0, 0)

    // 正交相机视体积设置
    const aspect = size.width / size.height
    const frustumSize = maxDim * 2.2 || 10

    camera.left = (-frustumSize * aspect) / 2
    camera.right = (frustumSize * aspect) / 2
    camera.top = frustumSize / 2
    camera.bottom = -frustumSize / 2
    camera.near = 0.01
    camera.far = distance * 20
    camera.zoom = 1.0
    camera.updateProjectionMatrix()

    if (controlsRef.current) {
      controlsRef.current.target.set(0, 0, 0)
      controlsRef.current.update()
    }
  }, [geometry, camera, size, viewMode])

  return <OrbitControls ref={controlsRef} />
}

function ProductFitController({ fitBounds, resetViewToken }) {
  const { camera, size } = useThree()
  const controlsRef = useRef()
  const initialized = useRef(null)
  const fit = useMemo(() => computeProductCameraFit(fitBounds, size.width, size.height), [fitBounds, size.width, size.height])

  useEffect(() => {
    if (!fit || !controlsRef.current) return
    const controls = controlsRef.current
    const restore = !initialized.current || initialized.current.fitBounds !== fitBounds || initialized.current.token !== resetViewToken
    const next = productCameraState(fit, { position: camera.position.toArray(), quaternion: camera.quaternion.toArray(), target: controls.target.toArray(), zoom: camera.zoom }, restore)
    for (const field of ['left', 'right', 'top', 'bottom', 'near', 'far', 'zoom']) camera[field] = next[field]
    // A resize changes only the fit baseline. In particular it never calls lookAt/update on controls.
    if (restore) {
      camera.position.fromArray(next.position)
      controls.target.fromArray(next.target)
      camera.up.set(0, 1, 0)
      camera.lookAt(controls.target)
      controls.update()
    }
    camera.updateProjectionMatrix()
    initialized.current = { fitBounds, token: resetViewToken }
  }, [fit, fitBounds, resetViewToken, camera])
  return <OrbitControls ref={controlsRef} enableDamping={false} />
}

function LegacyLatticeViewer({ stlUrl, viewMode }) {
  const geometry = useLoader(STLLoader, stlUrl)

  const centeredGeometry = useMemo(() => {
    if (!geometry) return null

    geometry.computeBoundingBox()
    const box = geometry.boundingBox
    const center = new THREE.Vector3()
    box.getCenter(center)

    const cloned = geometry.clone()
    cloned.translate(-center.x, -center.y, -center.z)
    cloned.computeVertexNormals()

    return cloned
  }, [geometry])

  if (!centeredGeometry) return null

  return (
    <>
      <ambientLight intensity={1.2} />
      <directionalLight position={[5, 5, 5]} intensity={1.2} />
      <directionalLight position={[-5, -5, -5]} intensity={0.6} />

      <mesh geometry={centeredGeometry}>
        <meshStandardMaterial color="lightgray" metalness={0.1} roughness={0.6} />
      </mesh>

      <axesHelper args={[2]} />
      <gridHelper args={[10, 10]} />
      <CameraController geometry={centeredGeometry} viewMode={viewMode} />
    </>
  )
}

function ProductLatticeViewer({ stlUrl, requestKey, resetViewToken, onPreviewEvent }) {
  const [model, setModel] = useState(null)
  useEffect(() => {
    const controller = new AbortController()
    let active = true, timedOut = false, geometry = null, buffer = null
    const timings = { read_started: performance.now() }
    const report = event => { if (active) onPreviewEvent({ ...event, request_key: requestKey, timings: { ...timings } }) }
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, 30000)
    setModel(null)
    async function load() {
      try {
        const response = await fetch(stlUrl, { signal: controller.signal })
        buffer = await readProductStlBytes(response, { signal: controller.signal })
        clearTimeout(timer)
        timings.read_ended = performance.now()
        if (!active) return
        // STLLoader.parse is synchronous; the read timeout cannot interrupt parsing.
        timings.validate_started = performance.now()
        const inspected = inspectProductStl(buffer)
        timings.bytes = inspected.bytes
        timings.triangles = inspected.triangles
        timings.validate_ended = performance.now()
        timings.parse_started = performance.now()
        try { geometry = new STLLoader().parse(buffer) } catch { throw productStlError('STL_PARSE_FAILED') }
        timings.parse_ended = performance.now()
        timings.geometry_validate_started = performance.now()
        const validated = validateProductGeometry(geometry)
        if (validated.triangles !== inspected.triangles) throw productStlError('STL_FORMAT_INVALID')
        timings.geometry_validate_ended = performance.now()
        buffer = null
        if (!active) { geometry.dispose(); geometry = null; return }
        report({ type: 'parsed' })
        let displayed = false
        setModel({ geometry, fitBounds: validated.bounds, afterRender() {
          if (!active || displayed) return
          displayed = true
          timings.first_frame_submitted = performance.now()
          report({ type: 'frame' })
        } })
      } catch (error) {
        if (!active || (controller.signal.aborted && !timedOut)) return
        geometry?.dispose()
        geometry = null
        const failure = timedOut ? productStlError('READ_TIMEOUT', 'read') : error?.code ? error : productStlError('NETWORK_ERROR', 'read')
        report({ type: 'error', error: failure })
      } finally { clearTimeout(timer); buffer = null }
    }
    load()
    return () => { active = false; controller.abort(); clearTimeout(timer); geometry?.dispose(); geometry = null; buffer = null }
  }, [stlUrl, requestKey, onPreviewEvent])
  if (!model) return null
  return <>
    <ambientLight intensity={1.2} />
    <directionalLight position={[5, 5, 5]} intensity={1.2} />
    <directionalLight position={[-5, -5, -5]} intensity={0.6} />
    <mesh geometry={model.geometry} onAfterRender={model.afterRender}>
      <meshStandardMaterial color="lightgray" metalness={0.1} roughness={0.6} />
    </mesh>
    <ProductFitController fitBounds={model.fitBounds} resetViewToken={resetViewToken} />
  </>
}

export default function LatticeViewer({ stlUrl, viewMode, cameraMode = 'legacy', resetViewToken = 0, requestKey, onPreviewEvent }) {
  return cameraMode === 'product-fit'
    ? <ProductLatticeViewer stlUrl={stlUrl} requestKey={requestKey} resetViewToken={resetViewToken} onPreviewEvent={onPreviewEvent} />
    : <LegacyLatticeViewer stlUrl={stlUrl} viewMode={viewMode} />
}
