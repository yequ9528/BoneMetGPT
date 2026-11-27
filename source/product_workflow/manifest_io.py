"""Canonical manifest I/O shared by the LatticeGPT V1 release contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import copy
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping


_DETACHED_SHA_RE = re.compile(r"^([0-9A-F]{64})  ([^\r\n]+)\n$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class OutputSetHold(RuntimeError):
    """Terminal create-once failure; any created subset must stay quarantined."""

    def __init__(
        self,
        code: str,
        *,
        writes_performed: int = 0,
        created: tuple[str, ...] = (),
        partial_quarantined: bool = False,
    ) -> None:
        self.code = code
        self.writes_performed = writes_performed
        self.created = created
        self.status = (
            "PARTIAL_QUARANTINED"
            if writes_performed or partial_quarantined
            else "HOLD"
        )
        super().__init__(code)


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _directory_identity(path: Path) -> tuple[int, int, int]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or _is_reparse_or_symlink(path):
        raise OutputSetHold("UNSAFE_OUTPUT_DIRECTORY")
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(getattr(info, "st_file_attributes", 0)),
    )


def _beneath(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def detached_sha256_bytes(manifest_bytes: bytes, filename: str) -> bytes:
    """Return the exact create-once detached SHA line used by R3 Gate 3-5."""

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
        raise ValueError("detached SHA filename must be one basename")
    return f"{sha256_bytes(manifest_bytes)}  {filename}\n".encode("ascii")


def exclusive_create_set_v1(
    root: str | Path,
    outputs: Mapping[str, bytes],
    *,
    before_create: Callable[[int, Path], None] | None = None,
) -> dict[str, Any]:
    """Create one ordered output set once, or stop without overwrite/cleanup/retry.

    The caller supplies mappings in normative creation order.  Namespace and
    directory identities are revalidated immediately before and after every
    exclusive create.  The injection hook exists only for deterministic Gate 2
    race/namespace-substitution tests.
    """

    exact_root = Path(root)
    if not exact_root.is_absolute():
        exact_root = exact_root.resolve(strict=False)
    root_missing = not exact_root.exists()
    root_chain: list[Path] = []
    if root_missing:
        anchor = exact_root
        while not anchor.exists() and anchor != anchor.parent:
            root_chain.append(anchor)
            anchor = anchor.parent
        if not anchor.is_dir() or _is_reparse_or_symlink(anchor):
            raise OutputSetHold("OUTPUT_ROOT_NOT_REGULAR_DIRECTORY")
        anchor_identity = _directory_identity(anchor)
        root_resolved = exact_root.resolve(strict=False)
        root_identity = None
    else:
        if not exact_root.is_dir():
            raise OutputSetHold("OUTPUT_ROOT_NOT_REGULAR_DIRECTORY")
        if _is_reparse_or_symlink(exact_root):
            raise OutputSetHold("OUTPUT_ROOT_REPARSE_FORBIDDEN")
        root_resolved = exact_root.resolve(strict=True)
        root_identity = _directory_identity(exact_root)
    if not outputs:
        raise ValueError("outputs must be nonempty")

    planned: list[tuple[str, Path, bytes]] = []
    for raw_relative, raw_bytes in outputs.items():
        relative = validate_relative_path(raw_relative, label="output path")
        if not isinstance(raw_bytes, bytes):
            raise TypeError("output bytes must be bytes")
        target = exact_root.joinpath(*relative.split("/"))
        parent = target.parent
        if not root_missing:
            probe = parent
            while probe != exact_root and not probe.exists():
                probe = probe.parent
            if not _beneath(root_resolved, probe.resolve(strict=True)):
                raise OutputSetHold("OUTPUT_PATH_ESCAPE")
            while probe != exact_root:
                if _is_reparse_or_symlink(probe) or not probe.is_dir():
                    raise OutputSetHold("UNSAFE_OUTPUT_PARENT")
                probe = probe.parent
        if target.exists() or _is_reparse_or_symlink(target):
            raise OutputSetHold("OUTPUT_SET_NOT_EMPTY")
        planned.append((relative, target, raw_bytes))

    # Parents are created only after the complete all-absent target decision.
    if root_missing:
        if _directory_identity(anchor) != anchor_identity:
            raise OutputSetHold("OUTPUT_PARENT_IDENTITY_CHANGED")
        for missing in reversed(root_chain):
            try:
                missing.mkdir()
            except FileExistsError as exc:
                raise OutputSetHold("OUTPUT_PARENT_CREATE_RACE") from exc
        root_resolved = exact_root.resolve(strict=True)
        root_identity = _directory_identity(exact_root)
    parents: dict[Path, tuple[int, int, int]] = {exact_root: root_identity}
    for _, target, _ in planned:
        chain: list[Path] = []
        parent = target.parent
        while parent != exact_root and not parent.exists():
            chain.append(parent)
            parent = parent.parent
        for missing in reversed(chain):
            try:
                missing.mkdir()
            except FileExistsError as exc:
                raise OutputSetHold("OUTPUT_PARENT_CREATE_RACE") from exc
        parent = target.parent
        if not _beneath(root_resolved, parent.resolve(strict=True)):
            raise OutputSetHold("OUTPUT_PATH_ESCAPE")
        parents[parent] = _directory_identity(parent)

    created: list[str] = []
    for position, (relative, target, raw_bytes) in enumerate(planned, start=1):
        if before_create is not None:
            before_create(position, target)
        try:
            if _directory_identity(exact_root) != root_identity:
                raise OutputSetHold(
                    "OUTPUT_PARENT_IDENTITY_CHANGED",
                    writes_performed=len(created),
                    created=tuple(created),
                )
            if _directory_identity(target.parent) != parents[target.parent]:
                raise OutputSetHold(
                    "OUTPUT_PARENT_IDENTITY_CHANGED",
                    writes_performed=len(created),
                    created=tuple(created),
                )
            with target.open("xb") as stream:
                stream.write(raw_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            if _directory_identity(exact_root) != root_identity or _directory_identity(
                target.parent
            ) != parents[target.parent]:
                raise OutputSetHold(
                    "OUTPUT_PARENT_IDENTITY_CHANGED",
                    writes_performed=len(created) + 1,
                    created=tuple([*created, relative]),
                )
        except OutputSetHold:
            raise
        except (FileExistsError, IsADirectoryError, NotADirectoryError, OSError) as exc:
            raise OutputSetHold(
                "EXCLUSIVE_CREATE_FAILED",
                writes_performed=len(created),
                created=tuple(created),
                partial_quarantined=target.exists() or bool(created),
            ) from exc
        created.append(relative)

    return {
        "status": f"CREATED_{len(created)}_OF_{len(planned)}",
        "writes_performed": len(created),
        "created": [
            {"position": index, "relative_path": relative}
            for index, relative in enumerate(created, start=1)
        ],
    }


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _validate_json_value(value: Any, *, path: str = "") -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        if isinstance(value, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError(f"unpaired surrogate at {path or '/'}")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path or '/'}")
        return 0.0 if value == 0.0 else value
    if isinstance(value, list):
        return [
            _validate_json_value(item, path=f"{path}/{index}")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object key at {path or '/'} must be a string")
            _validate_json_value(key, path=f"{path}/<key>")
            normalized[key] = _validate_json_value(
                item,
                path=f"{path}/{key.replace('~', '~0').replace('/', '~1')}",
            )
        return normalized
    raise TypeError(f"unsupported JSON value at {path or '/'}: {type(value).__name__}")


def _remove_json_pointer_leaf(value: dict[str, Any], pointer: str) -> None:
    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
        raise ValueError("self_hash_pointer must name one object leaf")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    target: Any = value
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            raise ValueError("self_hash_pointer does not resolve")
        target = target[part]
    leaf = parts[-1]
    if not isinstance(target, dict) or leaf not in target or isinstance(target[leaf], (dict, list)):
        raise ValueError("self_hash_pointer must resolve to one leaf")
    del target[leaf]


def canonical_json_object_v1(
    value: object,
    *,
    self_hash_pointer: str | None = None,
) -> bytes:
    """Serialize one validated R3 object using the frozen compact byte contract."""

    if not isinstance(value, dict):
        raise TypeError("canonical_json_object_v1 requires one JSON object")
    normalized = _validate_json_value(copy.deepcopy(value))
    if self_hash_pointer is not None:
        _remove_json_pointer_leaf(normalized, self_hash_pointer)
    return (
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def validate_relative_path(value: str, *, label: str) -> str:
    candidate = str(value)
    parts = candidate.split("/")
    invalid = (
        not candidate
        or "\\" in candidate
        or candidate.startswith("/")
        or candidate.startswith("//")
        or _WINDOWS_DRIVE_RE.match(candidate) is not None
        or PurePosixPath(candidate).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(token in candidate for token in ("*", "?", "[", "]"))
    )
    if invalid:
        raise ValueError(f"{label} must be a normalized relative path")
    return candidate


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {token}")


def parse_closed_json_object_v1(raw_utf8: bytes, schema_version: str) -> dict[str, Any]:
    """Parse exactly one canonical R3 object and validate its closed schema."""

    if not isinstance(raw_utf8, bytes):
        raise TypeError("raw_utf8 must be bytes")
    if raw_utf8.startswith(b"\xef\xbb\xbf"):
        raise ValueError("UTF-8 BOM is forbidden")
    try:
        text = raw_utf8.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("raw object must use strict UTF-8") from exc
    if not text.startswith("{"):
        raise ValueError("raw object must begin with '{'")
    if not text.endswith("\n") or text.endswith("\n\n") or "\r" in text:
        raise ValueError("raw object must have exactly one final LF")
    body = text[:-1]
    try:
        payload = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("raw object is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("raw JSON root must be an object")
    if payload.get("schema_version") != schema_version:
        raise ValueError("schema_version identity mismatch")
    _validate_json_value(payload)
    from .reproducibility import validate_closed_r3_schema

    validated = validate_closed_r3_schema(schema_version, payload)
    if canonical_json_object_v1(validated) != raw_utf8:
        raise ValueError("raw object is not canonical_json_object_v1 bytes")
    return validated


def read_verified_json(
    manifest_path: str | Path,
    detached_sha_path: str | Path,
) -> tuple[dict[str, Any], str]:
    manifest = Path(manifest_path)
    detached = Path(detached_sha_path)
    raw = manifest.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("manifest must use UTF-8 without BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("manifest must use UTF-8 without BOM") from exc

    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError("manifest JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("manifest JSON must be an object")
    if "manifest_sha256" in payload:
        raise ValueError("manifest cannot contain its self SHA256")
    if canonical_json_bytes(payload) != raw:
        raise ValueError("manifest must use canonical JSON bytes")

    try:
        detached_text = detached.read_text(encoding="ascii", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise ValueError("detached SHA256 file is invalid") from exc
    match = _DETACHED_SHA_RE.fullmatch(detached_text)
    if match is None or match.group(2) != manifest.name:
        raise ValueError("detached SHA256 file is invalid")
    actual = sha256_bytes(raw)
    if match.group(1) != actual:
        raise ValueError("detached SHA256 mismatch")
    return payload, actual


__all__ = [
    "OutputSetHold",
    "canonical_json_object_v1",
    "canonical_json_bytes",
    "detached_sha256_bytes",
    "exclusive_create_set_v1",
    "parse_closed_json_object_v1",
    "read_verified_json",
    "sha256_bytes",
    "sha256_file",
    "validate_relative_path",
]
