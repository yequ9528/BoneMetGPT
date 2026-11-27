import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { transformSync } from 'esbuild'
import { OrbitControls } from 'three-stdlib'
import * as THREE from 'three'

// Execute the production Camera and its local helpers. Replace only the React/R3F
// host: retain refs across renders, commit the actual OrbitControls before layout.
const scene = readFileSync(new URL('../src/visualization/ProductResultScene.jsx', import.meta.url), 'utf8')
const region = scene.slice(scene.indexOf('function boundsOfReference('), scene.indexOf('function useCapture('))
const compiled = transformSync(region, { loader: 'jsx', jsxFactory: 'createElement' }).code
const loadCamera = new Function('THREE', 'useThree', 'useRef', 'useLayoutEffect', 'OrbitControls',
  'createElement', compiled + '\nreturn Camera')

const model = new THREE.Box3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(1, 1, 1))
const reference = (min, max, id = 'original') => ({ status: 'available', source_sha256: id, bbox: { min, max } })
const near = reference([0, 0, 0], [1.01, 1.01, 1.01])
const large = reference([0, 0, 0], [100, 100, 100])
function harness() {
  const camera = new THREE.OrthographicCamera()
  const controls = new OrbitControls(camera)
  const size = { width: 800, height: 500 }
  let index, effect, dependencies
  const refs = []
  const Camera = loadCamera(THREE, () => ({ camera, size }),
    value => refs[index++] ??= { current: value },
    (next, deps) => {
      if (!dependencies || deps.some((value, i) => value !== dependencies[i])) effect = next
      dependencies = deps
    }, OrbitControls, (_type, props) => { props.ref.current = controls })
  const render = (ref, token = 0) => {
    index = 0
    effect = null
    Camera({ bounds: model, reference: ref, view: 'front', token, locked: false })
    effect?.()
    camera.updateMatrixWorld()
  }
  render(null)
  return { camera, controls, size, render }
}
function snapshot({ camera, controls }) {
  return [camera.position.toArray(), camera.quaternion.toArray(), controls.target.toArray(),
    camera.zoom, camera.top, camera.bottom, camera.near, camera.far]
}
function direction(camera) {
  return camera.getWorldDirection(new THREE.Vector3())
}
function assertVisible(camera, ref, padding = 0) {
  const bounds = model.clone().union(new THREE.Box3(
    new THREE.Vector3(...ref.bbox.min), new THREE.Vector3(...ref.bbox.max)))
  for (const x of [bounds.min.x, bounds.max.x]) for (const y of [bounds.min.y, bounds.max.y]) {
    for (const z of [bounds.min.z, bounds.max.z]) {
      const projected = new THREE.Vector3(x, y, z).project(camera)
      assert.ok(projected.toArray().every(v => Math.abs(v) <= 1 - padding + 1e-8),
        `corner [${x},${y},${z}] projected to [${projected.toArray()}]`)
    }
  }
}

test('async in-frame reference preserves the current pose and projection', () => {
  const h = harness()
  h.controls.target.addScalar(0.02)
  h.camera.position.add(new THREE.Vector3(1, 0.3, 0.02))
  h.camera.zoom = 0.6
  h.controls.update()
  h.camera.updateProjectionMatrix()
  h.camera.updateMatrixWorld()
  assertVisible(h.camera, near)
  const before = snapshot(h), projection = h.camera.projectionMatrix.clone()
  h.render(near)
  assert.deepEqual(snapshot(h), before)
  assert.ok(projection.equals(h.camera.projectionMatrix))
})

for (const [name, ref] of [
  ['larger', large], ['offset', reference([10, 0, 0], [11, 1, 1])],
  ['depth only', reference([0, 0, -100], [1, 1, -99])]
]) {
  test(`entering ${name} reference fits all eight union corners, including depth and margin`, () => {
    const h = harness()
    if (name === 'larger') {
      h.camera.position.set(2, 1.7, 3)
      h.controls.update()
    }
    const before = direction(h.camera)
    h.render(ref)
    assertVisible(h.camera, ref, 0.02)
    assert.ok(before.distanceTo(direction(h.camera)) < 1e-6)
  })
}

test('closing keeps the view; cached reopening checks the live zoom; Reset uses visible geometry', () => {
  const h = harness()
  h.render(large)
  h.render(large, 1)
  assertVisible(h.camera, large)
  const fitted = snapshot(h)
  h.render(null, 1)
  assert.deepEqual(snapshot(h), fitted)
  h.camera.zoom = 5
  h.camera.updateProjectionMatrix()
  h.render(large, 1)
  assertVisible(h.camera, large)
  h.render(null, 1)
  h.render(null, 2)
  assert.ok(h.controls.target.distanceTo(new THREE.Vector3(0.5, 0.5, 0.5)) < 1e-8)
  assert.ok(h.camera.top < 1)
  h.render(large, 3)
  assert.ok(h.controls.target.distanceTo(new THREE.Vector3(50, 50, 50)) < 1e-8)
  assertVisible(h.camera, large)
})

test('ordinary resize and equivalent reference renders preserve a manually cropped view', () => {
  const h = harness()
  h.render(large, 1)
  h.controls.target.set(12, 10, 15)
  h.camera.position.set(30, 16, 40)
  h.camera.zoom = 6
  h.controls.update()
  h.camera.updateProjectionMatrix()
  const before = snapshot(h)
  h.size.width = 375
  h.size.height = 800
  h.render(large, 1)
  assert.deepEqual(snapshot(h), before)
  assert.equal(h.camera.right / h.camera.top, h.size.width / h.size.height)
  h.render({ ...large, bbox: { ...large.bbox } }, 1)
  assert.deepEqual(snapshot(h), before)
  h.render(reference([200, 0, 0], [201, 1, 1], 'replacement'), 1)
  assertVisible(h.camera, reference([200, 0, 0], [201, 1, 1]))
})
