/* eslint-disable react/no-unknown-property, react/prop-types */
import { OrbitControls } from '@react-three/drei'
import { useMemo } from 'react'
import * as THREE from 'three'

function buildGeometry(candidate) {
  const nodes = candidate?.nodes || []
  if (!nodes.length) return null
  const center = nodes.reduce(
    (total, node) => total.map((value, index) => value + node[index]),
    [0, 0, 0]
  ).map((value) => value / nodes.length)
  const positions = []
  for (const [startIndex, endIndex] of candidate.connections || []) {
    const start = nodes[startIndex]
    const end = nodes[endIndex]
    if (!start || !end) continue
    positions.push(
      start[0] - center[0], start[1] - center[1], start[2] - center[2],
      end[0] - center[0], end[1] - center[1], end[2] - center[2]
    )
  }
  if (!positions.length) return null
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
  return geometry
}

export default function CandidateCellViewer({ candidate }) {
  const geometry = useMemo(() => buildGeometry(candidate), [candidate])
  if (!geometry) return null
  return (
    <>
      <ambientLight intensity={1.1} />
      <directionalLight position={[3, 4, 5]} intensity={0.8} />
      <lineSegments geometry={geometry}>
        <lineBasicMaterial color={candidate.selected ? '#b7791f' : '#248f86'} />
      </lineSegments>
      <axesHelper args={[0.8]} />
      <OrbitControls makeDefault />
    </>
  )
}
