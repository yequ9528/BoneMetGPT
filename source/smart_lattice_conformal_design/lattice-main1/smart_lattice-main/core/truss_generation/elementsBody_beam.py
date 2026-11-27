"""
    get the body that composed by tetrahedron elements

    attention: node index start from 1, not 0!
    (注意！ 本程序的节点编号从 1 开始,  与 inp 中的节点编号相同)

    hashNodes algorithm:
        very fast to identify all facets,
        facets stored by a dict(), 
        (see 'facetDic')
        key of dict:
            sorted tuple of node numbers of a facet
    
    author: mohanxuan, 
    license: Apache 2.0
"""
import torch as tch
import threading, time
from readInp import readInp
import numpy as np

from torch._C import Value
from progressBar import *
from eleBody import EleBody


class ElementsBody_beam(EleBody):
    """
        author: mohanxuan,  
        license: Apache 2.0
        v0, v1, are the 2 nodes of beam, respectively
        node number starts from 1
        element number starts from 0
    """
    
    
    def getNodeGraph(self):
        """
            get the node link of the  body
            every node links to other nodes, as a graph
        """
        if not hasattr(self, 'graph'):
            links = [
                [0, 1]
            ]
            graph = {i: set() for i in self.nodes}
            for ele_ in self.elements:
                ele = ele_.tolist()
                for link in links:
                    graph[ele[link[0]]].add(ele[link[1]])
                    graph[ele[link[1]]].add(ele[link[0]])
            self.graph = graph
        return self.graph
    

    def writeEdgesInp(self, file_):
        """
            write an .inp file with beam (or edge) elements
        """
        with open(file_, "w") as file:
            file.write("*NODE \n")
            for node in self.nodes:
                file.write("%s,  %s, %s, %s\n" % (
                    node, *self.nodes[node]
                ))
            file.write("*ELEMENT,TYPE=B31 \n")
            idx = 0
            for beam in self.elements:
                idx += 1
                file.write("%s,  %s, %s\n" % (
                    idx, *(beam.tolist())
                ))
    

    def writeEdgesLength(self, file_=None):
        """
            write an .inp file with beam (or edge) elements
        """
        if file_ == None:
            path = input("\033[40;35;1m please give the path for output file: \033[0m")
            fileName = input("\033[40;33;1m please give the .inp file name: \033[0m")
            file_ = path + "/" + fileName + ".txt"
        with open(file_, "w") as file:
            for beam in self.elements:
                file.write("{}\n".format(
                    np.linalg.norm(np.array(self.nodes[int(beam[0])]) \
                        - np.array(self.nodes[int(beam[1])]))
                ))


    def averageBeamsLength(self):
        if not hasattr(self, "averageBeaLen"):
            lengths = []
            for ele in self.elements:
                nod1 = tch.tensor(self.nodes[int(ele[0])])
                nod2 = tch.tensor(self.nodes[int(ele[1])])
                lengths.append(sum((nod2 - nod1) ** 2) ** 0.5)
            self.averageBeaLen = sum(lengths) / len(lengths)
        return self.averageBeaLen
    

    def departByBeamDirections(self):
        """
            depart the beam elements into sets, 
            sets have different directions
        """
        direc = tch.tensor([0., 1., 0.])
        angleEps = 0.1
        set_vertical, set_horizontal = [], []
        for iele, ele in enumerate(self.elements):
            vec = tch.tensor(self.nodes[int(ele[1])]) - tch.tensor(self.nodes[int(ele[0])])
            cosAngle = abs((vec @ direc) / tch.norm(vec))
            if cosAngle > np.cos(np.pi / 2 * angleEps):
                set_vertical.append(iele)
            else:
                set_horizontal.append(iele)
        self.set_vertical, self.set_horizontal = set_vertical, set_horizontal
    

    def partitionByAngles(self, 
                          angles: list=None):
        """partition the beams into sets with different angles"""
        eps = 2.e-2
        if angles == None:
            angles = [(0., eps), (eps, 30.), 
                    (30., 60.), (60., 90. - eps), (90. - eps, 90.)][::-1]
        
        direc = tch.tensor([0., 1., 0.])
        sets = [[] for _ in range(len(angles))]
        for iele, ele in enumerate(self.elements):
            vec = tch.tensor(self.nodes[int(ele[1])]) - tch.tensor(self.nodes[int(ele[0])])
            cosAngle = abs((vec @ direc) / tch.norm(vec))
            angle = np.arccos(cosAngle) / np.pi * 180.
            for id, angleRange in enumerate(angles):
                if angleRange[0] <= angle < angleRange[1]:
                    sets[id].append(iele)
                    break
        self.sets_of_angles = sets
        return self.sets_of_angles
    

    def writePartitionSets(self, fileName=None):
        """
            write the partition result of the beam elements, 
            sets have different directions
        """
        if not hasattr(self, "set_vertical"):
            self.departByBeamDirections()
        path = input("\033[25;1m {} \033[0m".format("please give the output file path: "))
        if fileName == None:
            fileName = input("\033[40;33;1m {} \033[0m".format("please give the .inp file name: "))
        file1 = path + "/" + fileName + "_part1.inp"
        with open(file1, 'w') as file:
            file.write("*NODE \n")
            for node in self.nodes:
                file.write("%s,  %s, %s, %s\n" % (
                    node, *self.nodes[node]
                ))
            idx = 0
            file.write("*ELEMENT,TYPE=B31 \n")
            for iele in self.set_vertical:
                idx += 1
                file.write("%s,  %s, %s\n" % (
                    idx, *(map(int, self.elements[iele]))
                ))
        file2 = path + "/" + fileName + "_part2.inp"
        with open(file2, 'w') as file:
            file.write("*NODE \n")
            for node in self.nodes:
                file.write("%s,  %s, %s, %s\n" % (
                    node, *self.nodes[node]
                ))
            idx = 0
            file.write("*ELEMENT,TYPE=B31 \n")
            for iele in self.set_horizontal:
                idx += 1
                file.write("%s,  %s, %s\n" % (
                    idx, *(map(int, self.elements[iele]))
                ))
    

    def writeSetsOfAngles(self, fileName: str):
        """partition the body into sets of different angles and write the beams file"""
        if not hasattr(self, "sets_of_angles"):
            self.partitionByAngles()
        path = input("\033[25;1m {} \033[0m".format("please give the output file path: "))
        if fileName == None:
            fileName = input("\033[40;33;1m {} \033[0m".format("please give the .inp file name: "))
        for id, sets in enumerate(self.sets_of_angles):
            file1 = path + "/" + fileName + f"_part{id}.inp"
            with open(file1, 'w') as file:
                file.write("*NODE \n")
                for node in self.nodes:
                    file.write("%s,  %s, %s, %s\n" % (
                        node, *self.nodes[node]
                    ))
                idx = 0
                file.write("*ELEMENT,TYPE=B31 \n")
                for iele in sets:
                    idx += 1
                    file.write("%s,  %s, %s\n" % (
                        idx, *(map(int, self.elements[iele]))
                    ))
    

    def get_allEdges(self, ):
        if not hasattr(self, "edgeDic") or self.edgeDic == None:
            edgeDic = {}
            for iele, ele in enumerate(self.elements):
                edgeDic[(int(ele[0]), int(ele[1]))] = iele
            self.edgeDic = edgeDic
        return self.edgeDic
