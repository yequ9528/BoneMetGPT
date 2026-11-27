"""
    partition the cell nodes into 4 catagories:
        vertex nodes
        edge nodes
        facet nodes
        inside-body nodes

                      v3------v7
                     /|      /|
                    v0------v4|
                    | |     | |
                    | v2----|-v6
             y ^    |/      |/
               |    v1------v5
               --->
              /    x
             z  
"""

import yaml
from elementsBody_beam import ElementsBody_beam
import torch as tch


def topologicalXyz(x):
    if abs(x - 1.) < 1e-4:
        return 1
    elif abs(x + 1.) < 1e-4:
        return -1
    else:
        return 0


def cellPartition(obj):
    inEdgeNodes, inFacetNodes, inBodyNodes = [], [], []
    nodeFeatures = {}

    mapCooToFacet = {
        (-1, 0, 0): (0, 3, 2, 1),
        (1, 0, 0): (4, 5, 6, 7),
        (0, -1, 0): (2, 6, 5, 1),
        (0, 1, 0): (0, 4, 7, 3),
        (0, 0, -1): (2, 3, 7, 6),
        (0, 0, 1): (0, 1, 5, 4)
    }
    mapCooToEdge = {
        (0, -1, 1): (1, 5), (0, -1, -1): (2, 6),
        (0, 1, -1): (3, 7), (0, 1, 1): (0, 4),

        (-1, 0, 1): (1, 0), (1, 0, 1): (5, 4),
        (1, 0, -1): (6, 7), (-1, 0, -1): (2, 3),

        (-1, -1, 0): (2, 1), (1, -1, 0): (6, 5),
        (1, 1, 0): (7, 4), (-1, 1, 0): (3, 0)
    }
    mapCooToVertex = {
        (-1,  1,  1): 0, 
        (-1, -1,  1): 1, 
        (-1, -1, -1): 2, 
        (-1,  1, -1): 3, 
        ( 1,  1,  1): 4, 
        ( 1, -1,  1): 5, 
        ( 1, -1, -1): 6, 
        ( 1,  1, -1): 7, 
    }

    inBodyCount = 0
    for node in obj["nodes"]:
        ### get the nodeFeatures
        features = []
        topoXyz = tuple(map(
            topologicalXyz, obj["nodes"][node]
        ))
        countOne = sum(abs(x) for x in topoXyz)
        if countOne == 3:
            features.append("vertex")
            features.append(mapCooToVertex[topoXyz])
        elif countOne == 2:
            features.append("inEdge")
            features.append(mapCooToEdge[topoXyz])
        elif countOne == 1:
            features.append("inFacet")
            features.append(mapCooToFacet[topoXyz])
        else:
            features.append("inBody")
            features.append(inBodyCount)
            inBodyCount += 1
        nodeFeatures[node] = features

        ### get the catagories nodes
        if topoXyz == (0, 1, 1):
            inEdgeNodes.append(obj["nodes"][node][0])
        elif topoXyz == (0, 0, 1):
            inFacetNodes.append(obj["nodes"][node][0:2])
        elif topoXyz == (0, 0, 0):
            inBodyNodes.append(obj["nodes"][node])
    return nodeFeatures, inEdgeNodes, inFacetNodes, inBodyNodes


def cellPartitionGeneralized(obj):
    """Return topology groups without assuming a single canonical edge/facet.

    ``cellPartition`` keeps the original return shape used by existing scripts.
    This helper preserves ``nodeFeatures`` while additionally grouping boundary
    nodes by the actual local edge/facet they belong to, so conformal mapping can
    support generated cells whose boundary nodes are not on the canonical
    ``(0, 1, 1)`` edge or ``(0, 0, 1)`` facet.
    """

    nodeFeatures, _inEdgeNodes, _inFacetNodes, _inBodyNodes = cellPartition(obj)
    edgeNodesByTopo = {}
    facetNodesByTopo = {}
    inBodyNodeIds = []

    for node, feature in nodeFeatures.items():
        feature_type = feature[0]
        if feature_type == "inEdge":
            edge_key = tuple(feature[1])
            edgeNodesByTopo.setdefault(edge_key, []).append(node)
        elif feature_type == "inFacet":
            facet_key = tuple(feature[1])
            facetNodesByTopo.setdefault(facet_key, []).append(node)
        elif feature_type == "inBody":
            inBodyNodeIds.append(node)

    return nodeFeatures, edgeNodesByTopo, facetNodesByTopo, inBodyNodeIds
