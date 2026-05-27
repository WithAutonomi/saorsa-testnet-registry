"""Load, merge and validate deployment manifests.

Resolution order for ``load_manifest``:

1. Read the YAML file.
2. Resolve ``extends:`` (a path, relative to the file) by deep-merging the base
   under the child. Bases may themselves ``extends:`` another file.
3. Apply ``--set dotted.key=value`` overrides.
4. Apply a ``--name`` override.
5. Validate into a :class:`~saorsa_deploy.config.models.Manifest`.

Merge semantics (locked decision): dicts merge recursively; everything else,
**including lists, is replaced** by the overriding value. So a child that
redefines ``testnet.clients.uploaders`` fully replaces the base's list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from saorsa_deploy.config.models import Manifest


class ManifestError(Exception):
    """Raised for any manifest load/merge/validation failure (user-facing)."""


def _read_yaml(path: Path) -> dict:
    try:
        raw = path.read_text()
    except OSError as e:
        raise ManifestError(f"cannot read manifest '{path}': {e}") from e
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ManifestError(f"invalid YAML in '{path}': {e}") from e
    if data is None:
        raise ManifestError(f"manifest '{path}' is empty")
    if not isinstance(data, dict):
        raise ManifestError(f"manifest '{path}' must be a YAML mapping at the top level")
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto ``base``. Lists/scalars replace."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolve_extends(data: dict, base_dir: Path, _seen: set[Path]) -> dict:
    """Pop ``extends`` and deep-merge the (recursively-resolved) base under ``data``."""
    extends = data.pop("extends", None)
    if extends is None:
        return data
    if not isinstance(extends, str):
        raise ManifestError("`extends` must be a string path to another manifest")

    base_path = (base_dir / extends).resolve()
    if base_path in _seen:
        chain = " -> ".join(str(p) for p in _seen) + f" -> {base_path}"
        raise ManifestError(f"circular `extends` chain: {chain}")
    _seen.add(base_path)

    base_data = _read_yaml(base_path)
    base_data = _resolve_extends(base_data, base_path.parent, _seen)
    # child (``data``) overrides base
    return _deep_merge(base_data, data)


def _coerce_set_value(value: str) -> Any:
    """Interpret a --set value with YAML scalar rules (8 -> int, true -> bool, etc.)."""
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


def _apply_set(data: dict, dotted_key: str, value: Any) -> None:
    """Set ``data[a][b][c] = value`` for dotted_key 'a.b.c', creating dicts as needed."""
    parts = dotted_key.split(".")
    cursor: Any = data
    for part in parts[:-1]:
        if not isinstance(cursor, dict):
            raise ManifestError(f"--set {dotted_key}: '{part}' is not a mapping")
        cursor = cursor.setdefault(part, {})
    if not isinstance(cursor, dict):
        raise ManifestError(f"--set {dotted_key}: parent is not a mapping")
    cursor[parts[-1]] = value


def merge_manifest_dict(
    path: str | Path,
    *,
    sets: list[str] | None = None,
    name: str | None = None,
) -> dict:
    """Return the fully-merged manifest dict (extends + --set + --name), unvalidated."""
    path = Path(path).expanduser()
    data = _read_yaml(path)
    data = _resolve_extends(data, path.parent, {path.resolve()})

    for item in sets or []:
        if "=" not in item:
            raise ManifestError(f"--set must be KEY=VALUE, got '{item}'")
        key, raw_value = item.split("=", 1)
        _apply_set(data, key.strip(), _coerce_set_value(raw_value))

    if name is not None:
        data["name"] = name

    return data


def load_manifest(
    path: str | Path,
    *,
    sets: list[str] | None = None,
    name: str | None = None,
) -> Manifest:
    """Load, merge and validate a manifest into a :class:`Manifest`."""
    data = merge_manifest_dict(path, sets=sets, name=name)
    try:
        return Manifest.model_validate(data)
    except Exception as e:  # pydantic ValidationError -> friendly message
        raise ManifestError(f"invalid manifest '{path}':\n{e}") from e
