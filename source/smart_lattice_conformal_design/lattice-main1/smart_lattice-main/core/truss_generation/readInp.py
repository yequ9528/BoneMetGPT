"""
    get the body that composed by elements

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
import re

import torch as tch
import numpy as np


_ELEMENT_TYPE_RE = re.compile(
    r"(?:^|,)\s*TYPE\s*=\s*([^,\s]+)",
    re.IGNORECASE,
)
_ELEMENT_TYPE_ALIASES = {
    "C3D8": "C3D8",
    "C3D8R": "C3D8R",
    "C3D20": "C3D20",
    "C3D4": "3D4",
    "3D4": "3D4",
    "C3D10": "C3D10",
    "B31": "B31",
    "S3": "S3",
    "C3D6": "C3D6",
    "S4": "S4",
}
_ELEMENT_RECORD_WIDTHS = {
    "C3D8": 9,
    "C3D8R": 9,
    "C3D20": 21,
    "3D4": 5,
    "C3D10": 11,
    "B31": 3,
    "S3": 4,
    "C3D6": 7,
    "S4": 5,
}


def _keyword_name(line):
    return line.strip().split(",", 1)[0].upper()


def _element_type_from_header(line):
    match = _ELEMENT_TYPE_RE.search(line)
    if not match:
        return None
    return _ELEMENT_TYPE_ALIASES.get(match.group(1).strip().upper())

def readInpOutputSingle(file_name):
    '''
    read abaqus output report file, returns:
    element index set
    element output value set
    '''

    resultblock = []

    with open(file_name, "r") as inputf:
        for line in inputf:
            items = line.split()
            if len(items) == 2:
                if items[0].isdigit():
                    resultblock.append([float(item) for item in items])

    return np.array(resultblock)
def readInp(fileName='donut.inp'):
    """
    read the inp file, returns:
        nodes: the coordinates of all nodes
        elements: corresponding node numbers of all elements
    """

    nodes = {}
    reading_nodes = False
    with open(fileName, 'r', encoding='utf-8') as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith('**'):
                continue
            if line.startswith('*'):
                reading_nodes = _keyword_name(line) == '*NODE'
                continue
            if reading_nodes:
                data = [value.strip() for value in line.rstrip(',').split(',')]
                values = list(map(float, data))
                nodes[int(values[0])] = values[1:]

    current_type = None
    text = {}
    with open(fileName, 'r', encoding='utf-8') as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith('**'):
                continue
            if line.startswith('*'):
                current_type = None
                if _keyword_name(line) == '*ELEMENT':
                    current_type = _element_type_from_header(line)
                    if current_type is not None:
                        text.setdefault(current_type, [])
                continue
            if current_type is not None:
                data = [value.strip() for value in line.rstrip(',').split(',')]
                text[current_type].extend(value for value in data if value)
    eSets = {}
    for eType in text:
        data = list(map(int, text[eType]))
        elements = tch.tensor(data, dtype=tch.int64)
        elements = elements.reshape((-1, _ELEMENT_RECORD_WIDTHS[eType]))
        if eType in {"C3D8", "C3D8R"}:
            elements = elements[:, 1:]
        elif eType == "C3D20":
            elements = elements[:, 1:9]
        elif eType in ["3D4", "S4"]:
            elements = elements[:, 1:]
        elif eType == "C3D10":
            elements = elements[:, 1:5]
        elif eType == "B31":
            elements = elements[:, 1:]
        elif eType == "S3":
            elements = elements[:, 1:]
        elif eType == "C3D6":
            elements = elements[:, 1:]
        eSets[eType] = elements
        
    return nodes, eSets
