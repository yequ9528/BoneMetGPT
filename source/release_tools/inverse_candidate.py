"""Explicit, frozen inverse candidates; never selects or writes a release.

Preparation order: copy candidate_source_paths(), write candidate_registry() in
that copy, copy the controlled environment, freeze this spec outside the source
tree, compute source outputs, assemble bundle 2 for that source SHA, then plan
and exclusively write the existing R4 identity outputs. All functions here are
read-only. The build spec's externally recorded SHA is required by the CLI.

v51, v52 and v53 are independently frozen against the immutable v50 baseline.
Legacy candidates retain the exact 248-member set; v54 adds geometry and its tests, totaling 250 members. Each generation and freezes every final member's bytes.
Each delta explicitly names v50, not an incremental update to a prior candidate;
reading/building a new candidate never changes any historical identity.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat

from product_workflow.artifact_manifest import load_artifact_binding
from product_workflow.r4_lite_release_generation import load_generation_registry_v1
from release_tools.v1_manifest import canonical_json_bytes, file_identity
from release_tools.v1_scope import classify_source, normalize_relative_path


BASELINE_GENERATION = 'r4_lite_single_reference_workstation_v50'
CANDIDATE_GENERATION = 'r4_lite_single_reference_workstation_v51'
NMSE_GENERATION = 'r4_lite_single_reference_workstation_v54'
LIFECYCLE_GENERATION = 'r4_lite_single_reference_workstation_v56'
_CANDIDATE_PROFILES = {
    LIFECYCLE_GENERATION: ('latticegpt_v1.paths.r4_lite_single_reference_workstation_v57.json', 'v56_service_lifecycle_and_job_facts_changes'),
    'r4_lite_single_reference_workstation_v55': ('latticegpt_v1.paths.r4_lite_single_reference_workstation_v56.json', 'v55_default_api_model_changes'),
    NMSE_GENERATION: ('latticegpt_v1.paths.r4_lite_single_reference_workstation_v55.json', 'v54_nmse_geometry_changes'),
    CANDIDATE_GENERATION: ('latticegpt_v1.paths.r4_lite_single_reference_workstation_v52.json', 'v51_inverse_module_changes'),
    'r4_lite_single_reference_workstation_v52': ('latticegpt_v1.paths.r4_lite_single_reference_workstation_v53.json', 'v52_inverse_closeout_changes'),
    'r4_lite_single_reference_workstation_v53': ('latticegpt_v1.paths.r4_lite_single_reference_workstation_v54.json', 'v53_controlled_reply_changes'),
}
BASELINE_SOURCE_SHA = 'E126BAB0BFB0C4F5E061D019E948A82E2DD8BCA2CDF1F5DA1BA12F63BDECBE6E'
BASELINE_ACTIVE_SHA = '0397053C6B0003B37DFD9ADDB2E62184A894A7AD491DD6996599FCF62C24B5A4'
BASELINE_ENVIRONMENT_SHA = '40185A3A86422BA69177661D6CC1929C4080DB45FD66FED43E36D318EB6E6C84'
ADDED_PATHS = frozenset({
    'TrussInvertibleDesign/src/latent_optimization.py',
    'tests/test_inverse_20261110_integration.py',
    'tests/test_inverse_dialogue_acceptance.py',
    'release_tools/inverse_candidate.py',
    'tests/test_inverse_candidate_release.py',
})
NMSE_ADDED_PATHS = frozenset({'TrussInvertibleDesign/src/unit_cell_geometry.py', 'tests/test_inverse_nmse_unification.py'})
LIFECYCLE_ADDED_PATHS = frozenset({'scripts/latticegpt_v1_service_lifecycle.ps1', 'scripts/open_latticegpt_v1_history.py'})


def added_paths(generation):
    candidate_delta_key(generation)
    if generation == LIFECYCLE_GENERATION:
        return ADDED_PATHS | NMSE_ADDED_PATHS | LIFECYCLE_ADDED_PATHS
    return ADDED_PATHS | NMSE_ADDED_PATHS if generation in {NMSE_GENERATION, 'r4_lite_single_reference_workstation_v55'} else ADDED_PATHS


_SPEC_KEYS = {'schema_version', 'generation', 'project_root', 'release_root',
              'baseline_source_root', 'baseline_release_root', 'source_members', 'modified_paths'}


def _json(path: Path, expected_sha: str | None = None) -> dict:
    raw = path.read_bytes()
    if expected_sha is not None and hashlib.sha256(raw).hexdigest().upper() != expected_sha:
        raise ValueError(f'frozen input SHA drift: {path}')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('frozen input must be an object')
    return value


def _baseline(source_root: Path) -> tuple[dict, dict, dict]:
    prefix = source_root / 'release' / 'v1' / BASELINE_GENERATION
    path = prefix / 'source_manifest_v1.json'
    manifest = _json(path, BASELINE_SOURCE_SHA)
    if Path(str(path) + '.sha256').read_text('ascii').splitlines() != [f'{BASELINE_SOURCE_SHA}  {path.name}']:
        raise ValueError('baseline detached SHA mismatch')
    if len(manifest['files']) != 243 or manifest['source_count'] != 243:
        raise ValueError('baseline member cardinality mismatch')
    controls = []
    for key in ('allowlist', 'legacy'):
        record = manifest['control_files'][key]
        control = source_root / normalize_relative_path(record['path'])
        if control.stat().st_size != record['size_bytes']:
            raise ValueError('baseline control size drift')
        controls.append(_json(control, record['sha256']))
    return manifest, controls[0], controls[1]


def candidate_source_paths(baseline_source_root: str | Path, *, generation: str = CANDIDATE_GENERATION) -> list[str]:
    previous, _, _ = _baseline(Path(baseline_source_root))
    inherited = {row['path'] for row in previous['files']}
    additions = added_paths(generation)
    if inherited & additions or len(inherited) != 243:
        raise ValueError('candidate additions overlap baseline')
    return sorted(inherited | additions, key=str.casefold)


def candidate_delta_key(generation: str) -> str:
    if not isinstance(generation, str) or generation not in _CANDIDATE_PROFILES:
        raise ValueError('unsupported inverse candidate generation')
    return _CANDIDATE_PROFILES[generation][1]


def candidate_registry(baseline_source_root: str | Path, *, generation: str = CANDIDATE_GENERATION) -> dict:
    candidate_delta_key(generation)
    baseline_root = Path(baseline_source_root)
    previous, _, _ = _baseline(baseline_root)
    member = next(row for row in previous['files'] if row['path'] == 'config/r4_lite_release_generation_registry_v1.json')
    # The old registry is an immutable v50 manifest member, not an editable input.
    registry = _json(baseline_root / member['path'], member['sha256'])
    registry = json.loads(json.dumps(registry).replace(BASELINE_GENERATION, generation))
    registry['config_name'] = _CANDIDATE_PROFILES[generation][0]
    registry['r4_delta_count'] = 76 + len(added_paths(generation))
    registry['source_count'] = 243 + len(added_paths(generation))
    return registry


def _regular_member(root: Path, relative: str) -> Path:
    if normalize_relative_path(relative) != relative:
        raise ValueError('noncanonical member path')
    target = root / relative
    for part in (target, *target.parents):
        if part == root:
            break
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
            raise ValueError('source member uses a link or reparse point')
    if not target.resolve(strict=True).is_relative_to(root.resolve(strict=True)) or not target.is_file():
        raise ValueError('source member escapes candidate root')
    return target


def validate_source_members(root: Path, rows: list[dict], expected_paths: set[str]) -> None:
    if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != {'path', 'size_bytes', 'sha256'} for row in rows):
        raise ValueError('source member schema mismatch')
    paths = [row['path'] for row in rows]
    if any(not isinstance(path, str) for path in paths) or len(set(path.casefold() for path in paths)) != len(paths) or set(paths) != expected_paths:
        raise ValueError('candidate source membership mismatch')
    for row in rows:
        target = _regular_member(root, row['path'])
        if file_identity(target) != {key: row[key] for key in ('size_bytes', 'sha256')}:
            raise ValueError(f'candidate source byte drift: {row["path"]}')


def _roots(spec: dict) -> tuple[Path, Path, Path, Path]:
    roots = []
    for name in ('project_root', 'release_root', 'baseline_source_root', 'baseline_release_root'):
        path = Path(spec[name])
        if not path.is_absolute() or not path.is_dir() or path.is_symlink():
            raise ValueError(f'candidate root must be an existing regular absolute directory: {name}')
        roots.append(path.resolve(strict=True))
    for index in (0, 1):
        for other in range(4):
            if index != other and (roots[index].is_relative_to(roots[other]) or roots[other].is_relative_to(roots[index])):
                raise ValueError('candidate and baseline roots must be independent')
    return tuple(roots)


def freeze_candidate_spec(*, project_root, release_root, baseline_source_root, baseline_release_root,
                          generation: str = CANDIDATE_GENERATION) -> dict:
    candidate_delta_key(generation)
    spec = {'schema_version': 'inverse_candidate_build_spec_v1', 'generation': generation,
            'project_root': str(Path(project_root).resolve(strict=True)), 'release_root': str(Path(release_root).resolve(strict=True)),
            'baseline_source_root': str(Path(baseline_source_root).resolve(strict=True)),
            'baseline_release_root': str(Path(baseline_release_root).resolve(strict=True))}
    source, _, baseline, _ = _roots(spec)
    previous, _, _ = _baseline(baseline)
    spec['source_members'] = [{'path': path, **file_identity(_regular_member(source, path))} for path in candidate_source_paths(baseline, generation=generation)]
    current = {row['path']: row for row in spec['source_members']}
    spec['modified_paths'] = sorted(row['path'] for row in previous['files'] if any(row[key] != current[row['path']][key] for key in ('size_bytes', 'sha256')))
    validate_candidate_spec(spec)
    return spec


def validate_candidate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict) or set(spec) != _SPEC_KEYS or spec['schema_version'] != 'inverse_candidate_build_spec_v1':
        raise ValueError('candidate spec schema mismatch')
    candidate_delta_key(spec['generation'])
    source, _, baseline, _ = _roots(spec)
    previous, _, _ = _baseline(baseline)
    validate_source_members(source, spec['source_members'], set(candidate_source_paths(baseline, generation=spec['generation'])))
    current = {row['path']: row for row in spec['source_members']}
    modified = sorted(row['path'] for row in previous['files'] if any(row[key] != current[row['path']][key] for key in ('size_bytes', 'sha256')))
    if spec['modified_paths'] != modified:
        raise ValueError('candidate modified-path delta mismatch')
    if load_generation_registry_v1(source / 'config/r4_lite_release_generation_registry_v1.json') != candidate_registry(baseline, generation=spec['generation']):
        raise ValueError('candidate registry does not match frozen generation profile')
    return deepcopy(spec)


def read_candidate_spec(path: str | Path, expected_sha256: str) -> dict:
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or any(char not in '0123456789ABCDEF' for char in expected_sha256):
        raise ValueError('candidate spec SHA must be uppercase SHA256')
    value = _json(Path(path), expected_sha256)
    validated = validate_candidate_spec(value)
    if Path(path).read_bytes() != canonical_json_bytes(validated):
        raise ValueError('candidate spec must use canonical JSON bytes')
    return validated


def source_inputs(spec: dict) -> tuple[dict, dict, dict, dict]:
    validate_candidate_spec(spec)
    source, _, baseline, _ = _roots(spec)
    previous, allowlist, legacy = _baseline(baseline)
    for path in sorted(added_paths(spec['generation'])):
        role, layers = classify_source(path)
        allowlist['files'].append({'path': path, 'role': role, 'mainline_layers': layers})
    allowlist['status'] = 'FROZEN_R4_LITE'
    registry = load_generation_registry_v1(source / 'config/r4_lite_release_generation_registry_v1.json')
    r3 = _json(baseline / registry['r3_source_manifest_relative'], registry['r3_source_manifest_sha256'])
    return previous, allowlist, legacy, r3


def environment_inputs(spec: dict) -> dict:
    source, _, _, baseline_release = _roots(spec)
    prefix = baseline_release / 'release_metadata' / BASELINE_GENERATION
    active = _json(prefix / 'active_release_binding_v1.json', BASELINE_ACTIVE_SHA)
    environment = _json(prefix / 'environment_manifest_v1.json', BASELINE_ENVIRONMENT_SHA)
    if active['source_manifest_sha256'] != BASELINE_SOURCE_SHA or active['environment_manifest_sha256'] != BASELINE_ENVIRONMENT_SHA:
        raise ValueError('baseline environment relationship mismatch')
    if file_identity(source / 'config/reproduction_tolerances_v1.json')['sha256'] != active['tolerance_schema_sha256']:
        raise ValueError('candidate tolerance drift')
    return {'environment': environment, 'active_binding': active}


def candidate_artifact_binding(release_root: Path, source_sha: str):
    binding = load_artifact_binding(release_root / 'artifacts_v1')
    if binding.bundle_version != '2' or binding.source_release_sha256 != source_sha:
        raise ValueError('candidate requires bundle 2 bound to the exact candidate source')
    return binding
