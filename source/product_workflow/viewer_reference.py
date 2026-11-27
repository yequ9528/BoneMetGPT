"""Read-only native-coordinate display projection of an uploaded INP."""
from __future__ import annotations

from .mesh_domain import HEX_TYPES, parse_original_mesh

MAX_REFERENCE_NODES = 5000
MAX_REFERENCE_ELEMENTS = 2000
_PAIRS = {
    'C3D8': ((0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)),
    'C3D8R': ((0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)),
    'C3D4': ((0,1),(1,2),(2,0),(0,3),(1,3),(2,3)),
    'C3D6': ((0,1),(1,2),(2,0),(3,4),(4,5),(5,3),(0,3),(1,4),(2,5)),
    'S3': ((0,1),(1,2),(2,0)), 'S3R': ((0,1),(1,2),(2,0)),
    'S4': ((0,1),(1,2),(2,3),(3,0)), 'S4R': ((0,1),(1,2),(2,3),(3,0)),
    'B31': ((0,1),),
}


class ReferenceDomainMismatch(ValueError):
    """The recorded selected IDs cannot belong to this uploaded mesh."""


def build_original_reference(raw: bytes, *, selected_ids=None,
                             max_nodes=MAX_REFERENCE_NODES,
                             max_elements=MAX_REFERENCE_ELEMENTS) -> dict:
    parsed = parse_original_mesh(raw)
    nodes, elements = parsed['nodes'], parsed['elements']
    summary = {'node_count': len(nodes), 'element_count': len(elements),
               'representation': 'original_inp_wireframe', 'units': 'mm',
               'frame': 'source_native_cartesian_frame'}
    if len(nodes) > max_nodes or len(elements) > max_elements:
        return {**summary, 'status': 'too_large', 'message': 'The input mesh exceeds the reference display limit. Result and file access remain available.'}
    unsupported = sorted({e['type'] for e in elements.values()} - set(_PAIRS))
    if unsupported:
        return {**summary, 'status': 'unavailable', 'unsupported_types': unsupported,
                'message': 'The input mesh contains element types that cannot be displayed completely.'}
    selected = set(selected_ids) if selected_ids is not None else {i for i,e in elements.items() if e['type'] in HEX_TYPES}
    if not selected.issubset(elements) or any(elements[i]['type'] not in HEX_TYPES for i in selected):
        raise ReferenceDomainMismatch('Original reference selected domain does not match input elements')
    groups = {key: {'element_ids': [], 'edges': set()} for key in ('selected', 'excluded_solid', 'excluded_other')}
    for index, item in sorted(elements.items()):
        key = 'selected' if index in selected else 'excluded_solid' if item['type'].startswith('C3D') else 'excluded_other'
        group = groups[key]
        group['element_ids'].append(index)
        group['edges'].update(tuple(sorted((item['node_ids'][a], item['node_ids'][b]))) for a,b in _PAIRS[item['type']])
    for group in groups.values():
        group['element_count'] = len(group['element_ids'])
        group['edges'] = [list(edge) for edge in sorted(group['edges'])]
    lower = [min(p[a] for p in nodes.values()) for a in range(3)]
    upper = [max(p[a] for p in nodes.values()) for a in range(3)]
    return {**summary, 'status': 'available',
            'nodes': [{'id': i, 'xyz': list(p)} for i,p in sorted(nodes.items())],
            'groups': groups, 'bbox': {'min': lower, 'max': upper, 'size': [b-a for a,b in zip(lower,upper)]}}
