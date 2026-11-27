"""
    get the body that composed by C3D8R elements

    attention: node index start from 1, not 0!
    (本程序的节点编号从 1 开始， 与 inp 中的节点编号相同)

    hashNodes algorithm:
        very fast to identify all edges,
        edges stored by a dict(), 
        (see 'edgeDic')
        key of dict:
            sorted tuple of two nodes numbers of an edge
    
    author: mohanxuan, 
    license: Apache 2.0
"""
import torch as tch
import numpy as np
import time
from readInp import readInp
from eleBody import EleBody
from elementsBody_beam import ElementsBody_beam
import copy


def leftIdx(lis, idx):
    left = idx - 1
    if left < 0:
        left = len(lis) - 1
    return left


def rightIdx(lis, idx):
    right = idx + 1
    if right > len(lis) - 1:
        right = 0
    return right


def sortByClockwise(lis):
    """
        sort a list by clockwise or counterClockwise order
        applied for the facet nodes list

        start from the smallest nodes,
        if original direction's next node < reverse direction's next node
            use original direction
        else:
            use reverse direction
    """
    ### find the smallest node's idx
    start = lis.index(min(lis))
    res = [lis[start]]
    if lis[rightIdx(lis, start)] < lis[leftIdx(lis, start)]:
        cur = start
        for i in range(len(lis) - 1):
            res.append(lis[rightIdx(lis, cur)])
            cur = rightIdx(lis, cur)
    else:
        cur = start
        for i in range(len(lis) - 1):
            res.append(lis[leftIdx(lis, cur)]) 
            cur = leftIdx(lis, cur)
    return res
    

class ElementsBody_cube(EleBody):
    """
        author: mohanxuan,  
        license: Apache 2.0
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

    def get_allFacets(self):  # get the element facets
        """
            get all facets (no overlapped) of this body
            use hash table, or dict,
                key: tuple of sorted nodes by clockwse or counter-clockwise,
                     depending on which way has smaller for the first two nodes
                value: facet number
            
            author: 
            license: Apache 2.0
        """

        if not self.eleNeighbor:
            self.get_eleNeighbor()

        if not hasattr(self, "facetDic"):
            
            ### facets points
            facets = tch.tensor([
                [0, 3, 2, 1], [4, 5, 6, 7],
                [1, 2, 6, 5], [0, 4, 7, 3],
                [0, 1, 5, 4], [2, 3, 7, 6],                  
            ], dtype=tch.int)
            
            eleFacet = {
                i: [] for i in range(len(self.elements))
            }

            facetDic = {}  # key: tuple of facet nodes (sorted by clockwise), value: elements of this facet
            for iele, ele in enumerate(self.elements):

                for ifacet, facet in enumerate(facets):
                    f = []
                    for node in facet:
                        f.append(int(ele[node]))
                    tmp = tuple(sortByClockwise(f))
                    if tmp in facetDic:
                        facetDic[tmp].append(iele)
                    else:
                        facetDic[tmp] = [iele]
                    eleFacet[iele].append(tmp)
            
            self.facetDic = facetDic
            self.eleFacet = eleFacet
        
        return self.facetDic
    

    def get_allEdges(self):  # get the element edges
        """
            get all edges (no overlapped) of this body
            use hash table, or dict,
                key: tuple of sorted nodes
                value: edge number
            
            author: 
            license: Apache 2.0
        """

        if not self.eleNeighbor:
            self.get_eleNeighbor()

        if not hasattr(self, "edgeDic"):
            
            # edges points to the positive direction of natural coordinates
            edges = tch.tensor([
                [0, 1], [1, 2], [2, 3], [3, 0],
                [4, 5], [5, 6], [6, 7], [7, 4],
                [0, 4], [3, 7], [2, 6], [1, 5]                     
            ], dtype=tch.int)
            
            eleEdge = {
                i: [] for i in range(len(self.elements))
            }

            edgeDic = {}  # key: sorted tuple of edge nodes, value: elements of this edge
            for iele, ele in enumerate(self.elements):

                for iedge, edge in enumerate(edges):
                    f = []
                    for node in edge:
                        f.append(int(ele[node]))
                    tmp = tuple(sorted(f))
                    if tmp in edgeDic:
                        edgeDic[tmp].append(iele)
                    else:
                        edgeDic[tmp] = [iele]
                    eleEdge[iele].append(f)
            
            self.edgeDic = edgeDic
            self.eleEdge = eleEdge
        
        return self.edgeDic


    def get_allFacialEdges(self):  # get the element edges
        """
            get all facial edges (no overlapped) of this body
            use hash table, or dict,
                key: tuple of sorted nodes
                value: edge number
            
            author: 
            license: Apache 2.0
        """

        if not self.eleNeighbor:
            self.get_eleNeighbor()
        
        if not hasattr(self, "facetDic"):
            self.get_allFacets()

        if not hasattr(self, "facialEdgeDic"):
            
            # edges points to the positive direction of natural coordinates
            edges = tch.tensor([
                [0, 1], [1, 2], [2, 3], [3, 0]                   
            ], dtype=tch.int)

            edgeDic = set()  # sorted tuple of edge nodes
            for facet in self.facetDic:
                for edge in edges:
                    f = [facet[edge[0]], facet[edge[1]]]
                    tmp = tuple(sorted(f))
                    edgeDic.add(tmp)
            
            self.facialEdgeDic = edgeDic
        
        return self.facialEdgeDic
    

    def getNodeGraph(self):
        """
            get the node link of the  body
            every node links to other nodes, as a graph
        """
        if not hasattr(self, 'graph'):
            links = [
                [0, 1], [1, 2], [2, 3], [3, 0],
                [4, 5], [5, 6], [6, 7], [7, 4],
                [0, 4], [3, 7], [2, 6], [1, 5]     
            ]
            graph = {i: set() for i in self.nodes}
            for ele_ in self.elements:
                ele = ele_.tolist()
                for link in links:
                    graph[ele[link[0]]].add(ele[link[1]])
                    graph[ele[link[1]]].add(ele[link[0]])
            self.graph = graph
        return self.graph
      

    def writeEdgesInp(self):
        """
            write an .inp file with beam (or edge) elements
        """
        if not hasattr(self, "edgeDic") or self.edgeDic == None:
            self.get_allEdges()
        path = input("\033[40;35;1m please give the path for output file: \033[0m")
        fileName = input("\033[40;33;1m please give the .inp file name: \033[0m")

        file_ = path + "/" + fileName + ".inp"
        with open(file_, "w") as file:
            file.write("*NODE \n")
            for node in self.nodes:
                file.write("%s,  %s, %s, %s\n" % (
                    node, *self.nodes[node]
                ))
            file.write("*ELEMENT,TYPE=B31 \n")
            idx = 0
            for beam in self.edgeDic:
                idx += 1
                file.write("%s,  %s, %s\n" % (
                    idx, *beam
                ))


    def getOctetTruss(self):
        ### get the extra FCC nodes
        maxNodes = max(self.nodes)
        nodes = copy.deepcopy(self.nodes)
        self.get_allFacets()
        fromFacetTupleToNodeNumber = {}
        for ifacet, facet in enumerate(self.facetDic):
            nodesCoo = tch.tensor([self.nodes[nod] for nod in facet])
            nodesCoo = tch.einsum("ij -> j", nodesCoo) / len(nodesCoo)
            nodes[maxNodes + ifacet + 1] = list(map(float, nodesCoo))
            fromFacetTupleToNodeNumber[facet] = maxNodes + ifacet + 1

        ### get all beams in FCC cells
        beamsEles = []
        for ifacet, facet in enumerate(self.facetDic):
            for nod in facet:
                beamsEles.append([nod, maxNodes + ifacet + 1])

        ### get regular octahedron beams in all cubic cells
        for iele, ele in enumerate(self.elements):
            nod = []
            for ifacet, facet1 in enumerate(self.eleFacet[iele]):
                for jfacet in range(ifacet + 1, len(self.eleFacet[iele])):
                    facet2 = self.eleFacet[iele][jfacet]
                    if len(set(facet1) & set(facet2)) == 2:
                        nod1 = fromFacetTupleToNodeNumber[facet1]
                        nod2 = fromFacetTupleToNodeNumber[facet2]
                        beamsEles.append([nod1, nod2])

        ### get the body of beams
        beamsBody = ElementsBody_beam(nodes, tch.tensor(beamsEles))

        return beamsBody


    def getFCC(self):
        ### get the extra FCC nodes
        maxNodes = max(self.nodes)
        nodes = copy.deepcopy(self.nodes)
        self.get_allFacets()
        for ifacet, facet in enumerate(self.facetDic):
            nodesCoo = tch.tensor([self.nodes[nod] for nod in facet])
            nodesCoo = tch.einsum("ij -> j", nodesCoo) / len(nodesCoo)
            nodes[maxNodes + ifacet + 1] = list(map(float, nodesCoo))

        ### get all beams in FCC cells
        beamsEles = []
        for ifacet, facet in enumerate(self.facetDic):
            for nod in facet:
                beamsEles.append([nod, maxNodes + ifacet + 1])
        beamsBody = ElementsBody_beam(nodes, tch.tensor(beamsEles))

        return beamsBody
