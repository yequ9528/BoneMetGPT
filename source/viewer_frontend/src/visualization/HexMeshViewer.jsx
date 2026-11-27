/* eslint-disable react/no-unknown-property, react/prop-types */
import { OrbitControls } from '@react-three/drei'
import { useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'

function HexMeshCameraController({ bbox, viewMode }) {
  const { camera, size } = useThree()
  const controlsRef = useRef()

  useEffect(() => {
    if (!bbox) return

    const bboxSize = new THREE.Vector3(
      bbox.size?.[0] || 1,
      bbox.size?.[1] || 1,
      bbox.size?.[2] || 1
    )
    const maxDim = Math.max(bboxSize.x, bboxSize.y, bboxSize.z) || 1
    const distance = maxDim * 2.8

    let position = [distance, distance, distance]
    if (viewMode === 'front') {
      position = [0, 0, distance]
    } else if (viewMode === 'side') {
      position = [distance, 0, 0]
    } else if (viewMode === 'top') {
      position = [0, distance, 0]
    }

    camera.position.set(position[0], position[1], position[2])
    camera.lookAt(0, 0, 0)

    const aspect = size.width / size.height
    const frustumSize = maxDim * 2.3
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
  }, [bbox, camera, size, viewMode])

  return <OrbitControls ref={controlsRef} />
}

function buildCenteredEdgePositions(meshPayload) {
  const nodeById = new Map()
  for (const node of meshPayload.nodes || []) {
    nodeById.set(node.id, node.xyz)
  }

  const bbox = meshPayload.bbox || {}
  const min = bbox.min || [0, 0, 0]
  const max = bbox.max || [1, 1, 1]
  const center = [
    (min[0] + max[0]) / 2,
    (min[1] + max[1]) / 2,
    (min[2] + max[2]) / 2,
  ]

  const positions = []
  for (const [startId, endId] of meshPayload.edges || []) {
    const start = nodeById.get(startId)
    const end = nodeById.get(endId)
    if (!start || !end) continue

    positions.push(
      start[0] - center[0],
      start[1] - center[1],
      start[2] - center[2],
      end[0] - center[0],
      end[1] - center[1],
      end[2] - center[2]
    )
  }

  return new Float32Array(positions)
}

export default function HexMeshViewer({ meshPayload, viewMode }) {
  const geometry = useMemo(() => {
    if (meshPayload?.status !== 'available') return null

    const positions = buildCenteredEdgePositions(meshPayload)
    if (!positions.length) return null

    const nextGeometry = new THREE.BufferGeometry()
    nextGeometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    nextGeometry.computeBoundingBox()
    return nextGeometry
  }, [meshPayload])

  if (!geometry) return null

  return (
    <>
      <ambientLight intensity={1.0} />
      <directionalLight position={[4, 5, 6]} intensity={0.8} />
      <lineSegments geometry={geometry}>
        <lineBasicMaterial color="#1d77d4" linewidth={1} />
      </lineSegments>
      <axesHelper args={[1.8]} />
      <gridHelper args={[10, 10]} />
      <HexMeshCameraController bbox={meshPayload.bbox} viewMode={viewMode} />
    </>
  )
}
