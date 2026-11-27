from xml.dom import ValidationErr
from elementsBody_beam import ElementsBody_beam
from elementsBody_cube import ElementsBody_cube, sortByClockwise
from readInp import readInp
import cellPartition
import yaml
import numpy as np
import torch as tch


def shapeFunc1d(naturalXyz):
    return np.array([
        0.5 * (1. - naturalXyz), 
        0.5 * (1. + naturalXyz)
    ])


def shapeFunc2d(naturalXyz):
    shapeFunc = np.array([0.25 for _ in range(4)])
    natXyz_vertex = [
        [-1., 1.], [-1., -1.],
        [1., -1.], [1., 1.]
    ]
    for i in range(len(shapeFunc)):
        for j, ksi in enumerate(natXyz_vertex[i]):
            shapeFunc[i] *= 1. + ksi * naturalXyz[j]
    return shapeFunc


def shapeFunc3d(naturalXyz):
    shapeFunc = np.array([0.125 for _ in range(8)])
    natXyz_vertex = [
        (-1,  1,  1), (-1, -1,  1), 
        (-1, -1, -1), (-1,  1, -1), 
        ( 1,  1,  1), ( 1, -1,  1), 
        ( 1, -1, -1), ( 1,  1, -1), 
    ]
    for i in range(len(shapeFunc)):
        for j, ksi in enumerate(natXyz_vertex[i]):
            shapeFunc[i] *= 1. + ksi * naturalXyz[j]
    return shapeFunc


def findNearestNodeIdx(globalXyz, mapIdx, mapToNode, nodes):
    idx = min(
        mapToNode[mapIdx],
        key = lambda id:
            sum(((globalXyz - np.array(nodes[id])) ** 2))
    )
    return idx


def _coord_key(xyz):
    return tuple(round(float(value), 12) for value in xyz)


def _ensure_node(body, node_map, key, globalXyz, nextNode):
    if key not in node_map:
        node_map[key] = nextNode
        body.nodes[nextNode] = np.array(globalXyz, dtype=float).tolist()
        nextNode += 1
    return node_map[key], nextNode


def _validate_cell_edges(cell, nodeFeatures):
    for edge in cell.get("edges", []):
        for id_local in edge:
            if id_local not in nodeFeatures:
                raise ValueError(f"unknown cell node referenced by edge: {id_local}")


def conformalLattice(body, cellFile):
    if not isinstance(body, ElementsBody_cube):
        raise ValueError("error, 1st input args should be instance of ElementsBody_cube")
    body.get_allEdges()
    body.get_allFacets()

    with open(cellFile, encoding='utf-8') as cell_stream:
        cell = yaml.load(cell_stream, Loader=yaml.FullLoader)
    (
        nodeFeatures,
        _edgeNodesByTopo,
        _facetNodesByTopo,
        _inVoxelNodeIds,
    ) = cellPartition.cellPartitionGeneralized(cell)
    _validate_cell_edges(cell, nodeFeatures)

    mapEdgeToNode = {}
    mapFacetToNode = {}
    mapVoxelToNode = {}
    nextNode = max(body.nodes) + 1

    for iele, ele in enumerate(body.elements):
        globalNodes = np.array([body.nodes[int(node)] for node in ele])
        for id_local, feature in sorted(nodeFeatures.items(), key=lambda item: str(item[0])):
            feature_type = feature[0]
            if feature_type == "vertex":
                continue

            localXyz = cell["nodes"][id_local]
            shapeFunc = shapeFunc3d(localXyz)
            globalXyz = np.einsum("i, ik -> k", shapeFunc, globalNodes)

            if feature_type == "inEdge":
                tmp = feature[1]
                edgeTuple = tuple(sorted(int(ele[x]) for x in tmp))
                key = (edgeTuple, _coord_key(globalXyz))
                _nodeIdx, nextNode = _ensure_node(
                    body, mapEdgeToNode, key, globalXyz, nextNode
                )
            elif feature_type == "inFacet":
                tmp = feature[1]
                facetTuple = tuple(sortByClockwise([int(ele[x]) for x in tmp]))
                key = (facetTuple, _coord_key(globalXyz))
                _nodeIdx, nextNode = _ensure_node(
                    body, mapFacetToNode, key, globalXyz, nextNode
                )
            elif feature_type == "inBody":
                key = (iele, id_local)
                _nodeIdx, nextNode = _ensure_node(
                    body, mapVoxelToNode, key, globalXyz, nextNode
                )
            else:
                raise ValueError("error, nodeFeatures[id_local][0] doesn't match any existence cases")
    
    ### get all edges of new body
    newEdges = set()
    for iele, ele in enumerate(body.elements):
        globalNodes = np.array([body.nodes[int(node)] for node in ele])
        for edge in sorted(cell["edges"], key=lambda item: tuple(map(str, item))):
            indexes = []
            for id_local in edge:
                if nodeFeatures[id_local][0] == "vertex":
                    id_global = int(ele[
                        nodeFeatures[id_local][1]
                    ])
                    indexes.append(id_global)
                elif nodeFeatures[id_local][0] == "inEdge":
                    tmp = nodeFeatures[id_local][1]
                    edgeTuple = tuple(sorted(
                        int(ele[x]) for x in tmp
                    ))
                    localXyz = cell["nodes"][id_local]
                    shapeFunc = shapeFunc3d(localXyz)
                    globalXyz = np.einsum("i, ik -> k", shapeFunc, globalNodes)
                    key = (edgeTuple, _coord_key(globalXyz))
                    id_global = mapEdgeToNode[key]
                    indexes.append(id_global)
                elif nodeFeatures[id_local][0] == "inFacet":
                    tmp = nodeFeatures[id_local][1]
                    facetTuple = tuple(sortByClockwise(
                        [int(ele[x]) for x in tmp]
                    ))
                    localXyz = cell["nodes"][id_local]
                    shapeFunc = shapeFunc3d(localXyz)
                    globalXyz = np.einsum("i, ik -> k", shapeFunc, globalNodes)
                    key = (facetTuple, _coord_key(globalXyz))
                    id_global = mapFacetToNode[key]
                    indexes.append(id_global)
                elif nodeFeatures[id_local][0] == "inBody":
                    id_global = mapVoxelToNode[(iele, id_local)]
                    indexes.append(id_global)
                else:
                    raise ValueError("error, nodeFeatures[id_local][0] doesn't match any existence cases")
            newEdges.add(tuple(sorted(indexes)))
    
    beamsBody = ElementsBody_beam(body.nodes, tch.tensor(sorted(newEdges)))
    return beamsBody
