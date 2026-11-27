/* eslint-disable react/no-unknown-property, react/prop-types */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useThree } from '@react-three/fiber'
import { OrbitControls } from '@react-three/drei'
import * as THREE from 'three'
import { validateProductGeometry } from '../utils/productJobView.js'
import { captureCanvas, readScene, createCaptureGuard, sceneVersion } from '../utils/productResultExports.js'

function boundsOfReference(reference) {
  const box = reference?.status === 'available' && reference.bbox
  return box ? new THREE.Box3(new THREE.Vector3(...box.min),new THREE.Vector3(...box.max)) : null
}

function boxCorners(box) {
  const corners = []
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) {
    for (const z of [box.min.z, box.max.z]) corners.push(new THREE.Vector3(x, y, z))
  }
  return corners
}

function Camera({ bounds, reference, view, token, locked }) {
  const {camera,size}=useThree(), controls=useRef(), initialized=useRef(null)
  useLayoutEffect(()=>{
    if(!bounds || !controls.current || !(size.width>0&&size.height>0))return
    const previous = initialized.current
    const restore = !previous || previous.bounds !== bounds || previous.token !== token
    const extra = boundsOfReference(reference)
    const source = reference?.source_sha256
    const referenceChanged = extra && (!previous?.extra || previous.source !== source || !extra.equals(previous.extra))
    const aspect = size.width / size.height
    const halfHeight = (camera.top - camera.bottom) / 2
    camera.left = -halfHeight * aspect
    camera.right = halfHeight * aspect
    camera.updateProjectionMatrix()
    initialized.current = { bounds, token, extra, source }
    if (!restore && !referenceChanged) return

    const box = bounds.clone()
    if (extra) box.union(extra)
    const corners = boxCorners(box)
    camera.updateMatrixWorld()
    if (!restore && corners.every(corner => {
      const { x, y, z } = corner.clone().project(camera)
      return Math.max(Math.abs(x), Math.abs(y), Math.abs(z)) <= 1 + 1e-8
    })) return

    const center = box.getCenter(new THREE.Vector3())
    const diagonal = box.getSize(new THREE.Vector3()).length() || 1
    const direction = restore
      ? new THREE.Vector3(...({ front:[0,0,1], side:[1,0,0], top:[0,1,0] }[view] || [1,1,1])).normalize()
      : camera.getWorldDirection(new THREE.Vector3()).negate()
    camera.position.copy(center).addScaledVector(direction, diagonal * 2)
    if (restore) camera.up.set(...(view === 'top' ? [0,0,-1] : [0,1,0]))
    camera.lookAt(center)
    camera.updateMatrixWorld()
    const right = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0)
    const up = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 1)
    let halfW = 0, halfH = 0
    for (const corner of corners) {
      const point = corner.sub(center)
      halfW = Math.max(halfW, Math.abs(point.dot(right)))
      halfH = Math.max(halfH, Math.abs(point.dot(up)))
    }
    const half = Math.max(halfH, halfW / aspect, diagonal * .01) * 1.1
    camera.top = half
    camera.bottom = -half
    camera.left = -half * aspect
    camera.right = half * aspect
    camera.zoom = 1
    camera.near = diagonal * .001
    camera.far = diagonal * 5
    controls.current.target.copy(center)
    controls.current.update()
    camera.updateProjectionMatrix()
  },[bounds,reference,view,token,camera,size.width,size.height])
  return <OrbitControls ref={controls} enabled={!locked} enableDamping={false} />
}

function useCapture({labels,legend,onCapture,sourceKey,footer=false,exportScale=1}) {
  const {gl,scene,camera}=useThree(), active=useRef(true), epoch=useRef(sourceKey),capturing=useRef(false)
  epoch.current=sourceKey
  useEffect(()=>{active.current=true;return()=>{active.current=false}},[])
  const capture=useCallback(async({signal}={})=>{
    if(capturing.current)throw new Error('An image is already being prepared')
    const token={};capturing.current=token
    const key=epoch.current,cameraCopy=camera.clone(),guard=createCaptureGuard(gl.domElement),size=gl.getSize(new THREE.Vector2()).clone()
    const current=()=>active.current&&key===epoch.current&&!signal?.aborted&&!gl.getContext().isContextLost()&&guard.current()&&gl.getSize(new THREE.Vector2()).equals(size)
    const cancelled=()=>{guard.dispose();if(capturing.current===token)capturing.current=null}
    signal?.addEventListener('abort',cancelled,{once:true})
    try{
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))
      if(!current())throw new Error('The source or viewport changed. Please save again.')
      const ratio=gl.getPixelRatio()
      let encoded
      try{
        if(exportScale!==1)gl.setPixelRatio(ratio*exportScale)
        gl.render(scene,cameraCopy)
        // captureCanvas copies to an independent 2D canvas before its first await.
        encoded=captureCanvas(gl.domElement,labels,{background:'#f8fbfd',legend:legend?.current,footer,stillCurrent:current})
      }finally{
        if(exportScale!==1){gl.setPixelRatio(ratio);gl.render(scene,camera)}
      }
      return await encoded
    }finally{
      signal?.removeEventListener('abort',cancelled);guard.dispose();if(capturing.current===token)capturing.current=null
    }
  },[gl,scene,camera,labels,legend,footer,exportScale])
  useEffect(()=>{onCapture(capture);return()=>onCapture(null)},[capture,onCapture])
}

function ReferenceLines({reference,opacity,clippingPlanes}) {
  const geometries=useMemo(()=>{
    if(reference?.status!=='available')return []
    const byId=new Map(reference.nodes.map(node=>[node.id,node.xyz]))
    return Object.entries(reference.groups).map(([key,group])=>{
      const positions=[]
      for(const [a,b] of group.edges){if(byId.has(a)&&byId.has(b))positions.push(...byId.get(a),...byId.get(b))}
      const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(positions,3))
      return {key,geometry}
    })
  },[reference])
  useEffect(()=>()=>geometries.forEach(item=>item.geometry.dispose()),[geometries])
  return <group>{geometries.map(({key,geometry})=><lineSegments key={key} geometry={geometry}>
    <lineBasicMaterial color={key==='excluded_solid'?'#b6554b':key==='excluded_other'?'#737981':'#708796'}
      transparent opacity={opacity} depthWrite={false} clippingPlanes={clippingPlanes} />
  </lineSegments>)}</group>
}

export function ProductResultScene({url,payload,sourceKey,reference,referenceVisible,opacity,clip,view,viewToken,locked,
                                    onFrame,onFailure}) {
  const [model,setModel]=useState(null), delivered=useRef(null), {gl}=useThree()
  const version=sceneVersion(reference,referenceVisible,opacity,clip)
  useEffect(()=>{
    const controller=new AbortController();let active=true,geometry=null
    const timer=window.setTimeout(()=>controller.abort(),30000)
    setModel(null)
    ;(async()=>{
      try{
        const buffer=await readScene(payload,'triangles',controller.signal)
        if(!active)return
        if(!buffer.byteLength || buffer.byteLength%36 || buffer.byteLength/36>6000000)throw new Error('Invalid scene geometry')
        geometry=new THREE.BufferGeometry()
        geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(buffer),3))
        geometry.computeVertexNormals()
        validateProductGeometry(geometry);geometry.computeBoundingBox()
        if(active)setModel({geometry,bounds:geometry.boundingBox.clone()})
      }catch(error){if(active)onFailure(error)}finally{window.clearTimeout(timer)}
    })()
    return()=>{active=false;controller.abort();window.clearTimeout(timer);geometry?.dispose()}
  },[url,payload,sourceKey,onFailure])
  useEffect(()=>{
    gl.localClippingEnabled=true
    const lost=event=>{event.preventDefault();onFailure(new Error('3D display is unavailable. Use the original result preview.'))}
    gl.domElement.addEventListener('webglcontextlost',lost)
    return()=>gl.domElement.removeEventListener('webglcontextlost',lost)
  },[gl,onFailure])
  const planes=useMemo(()=>{
    if(!model || !clip.enabled)return []
    const axis={X:0,Y:1,Z:2}[clip.axis], b=model.bounds
    const lower=b.min.getComponent(axis),upper=b.max.getComponent(axis),normal=new THREE.Vector3().setComponent(axis,1)
    return [new THREE.Plane(normal,-(lower+(upper-lower)*clip.position/100))]
  },[model,clip])
  const afterFrame=()=>{if(delivered.current!==version){delivered.current=version;onFrame(version)}}
  if(!model)return null
  return <>
    <ambientLight intensity={1.1}/><directionalLight position={[5,7,9]} intensity={1.2}/><directionalLight position={[-5,-3,-4]} intensity={.5}/>
    <mesh geometry={model.geometry} onAfterRender={afterFrame}><meshStandardMaterial color="#5b9cae" roughness={.65} metalness={.08} clippingPlanes={planes}/></mesh>
    {referenceVisible&&<ReferenceLines reference={reference} opacity={opacity} clippingPlanes={planes}/>}
    <Camera bounds={model.bounds} reference={referenceVisible?reference:null} view={view} token={viewToken} locked={locked}/>
  </>
}

export function SolidCellScene({candidate,onCapture,onFrame,onFailure}) {
  const {gl}=useThree()
  useEffect(()=>{
    const lost=event=>{event.preventDefault();onFailure(new Error('The unit cell display was interrupted. Refresh before saving.'))}
    gl.domElement.addEventListener('webglcontextlost',lost)
    return()=>gl.domElement.removeEventListener('webglcontextlost',lost)
  },[gl,onFailure])
  const {mesh,bounds}=useMemo(()=>{
    const geometry=new THREE.CylinderGeometry(1,1,1,16), material=new THREE.MeshStandardMaterial({color:'#4d8fb9',roughness:.65})
    const mesh=new THREE.InstancedMesh(geometry,material,candidate.connections.length), matrix=new THREE.Matrix4(), quaternion=new THREE.Quaternion(), y=new THREE.Vector3(0,1,0)
    for(const [i,[a,b]] of candidate.connections.entries()){
      const start=new THREE.Vector3(...candidate.nodes[a]),end=new THREE.Vector3(...candidate.nodes[b]),direction=end.clone().sub(start),length=direction.length()
      quaternion.setFromUnitVectors(y,direction.normalize());matrix.compose(start.add(end).multiplyScalar(.5),quaternion,new THREE.Vector3(candidate.diameter/2,length,candidate.diameter/2));mesh.setMatrixAt(i,matrix)
    }
    mesh.computeBoundingSphere()
    const bounds=new THREE.Box3().setFromPoints(candidate.nodes.map(p=>new THREE.Vector3(...p))).expandByScalar(candidate.diameter/2)
    return {mesh,bounds}
  },[candidate])
  const done=useRef(false), labels=useMemo(()=>['Selected reference unit cell',`Normalized diameter: ${candidate.diameter} (dimensionless)`],[candidate.diameter])
  useEffect(()=>()=>{mesh.geometry.dispose();mesh.material.dispose()},[mesh])
  useCapture({labels,onCapture,sourceKey:candidate.candidate_id,footer:true,exportScale:3})
  return <>
    <ambientLight intensity={1.2}/><directionalLight position={[4,6,8]} intensity={1.2}/>
    <primitive object={mesh} dispose={null} onAfterRender={()=>{if(!done.current){done.current=true;onFrame()}}}/>
    <Camera bounds={bounds} view="iso" token={0} locked/>
  </>
}
