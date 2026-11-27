"""Pure geometry contract from the delivered 2026-11-10 owner viewer.

Coordinates and diameter are dimensionless. No inference, clipping or rounding.
"""
import math

GEOMETRY_CONTRACT = 'truss_20261110_unit_cell_v1'
FIXED_NODES = [[0,0,0],[1,0,0],[1,1,0],[0,1,0],
               [0,0,1],[1,0,1],[1,1,1],[0,1,1]]
CONNECTIONS = [[8,i] for i in range(8)] + [
    [9,0],[9,1],[9,2],[9,3], [10,4],[10,5],[10,6],[10,7],
    [11,0],[11,1],[11,5],[11,4], [12,2],[12,3],[12,7],[12,6],
    [13,0],[13,3],[13,7],[13,4], [14,1],[14,2],[14,6],[14,5]]


def build_unit_cell(parameters):
    if len(parameters) != 10 or any(isinstance(v, bool) for v in parameters):
        raise ValueError('unit cell requires ten finite numeric parameters')
    rho, x1,y1,z1,x2,y2,x4,z4,y6,z6 = values = list(map(float, parameters))
    if not all(math.isfinite(v) for v in values) or not 0 < rho <= 1:
        raise ValueError('unit cell relative density must be finite in (0, 1]')
    if any(not 0 <= v <= 1 for v in values[1:]):
        raise ValueError('unit cell coordinates must be finite in [0, 1]')
    nodes = [list(map(float, p)) for p in FIXED_NODES] + [
        [x1,y1,z1],[x2,y2,0.],[x2,y2,1.],
        [x4,0.,z4],[x4,1.,z4],[0.,y6,z6],[1.,y6,z6]]
    lengths = [math.dist(nodes[a], nodes[b]) for a,b in CONNECTIONS]
    if any(length <= 0 for length in lengths):
        raise ValueError('unit cell contains a zero-length member')
    length = sum(lengths)
    return dict(nodes=nodes, connections=[edge[:] for edge in CONNECTIONS],
                diameter=2*math.sqrt(rho/(math.pi*length)),
                total_effective_length=length, effective_member_count=len(CONNECTIONS),
                num_nodes=len(nodes), num_connections=len(CONNECTIONS),
                geometry_contract=GEOMETRY_CONTRACT)
