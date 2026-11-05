#!/usr/bin/env python3
"""Plan, apply, and diagnose a minimal Qlib research workspace.

Core implementation uses only the Python 3.12 standard library.  It never
installs packages, initializes Qlib, trains, backtests, or writes providers.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePath, PurePosixPath
import re
import stat
import struct
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime
from typing import Any, Iterable, Mapping, NamedTuple


SCHEMA_VERSION = 1
TEMPLATE_VERSION = 1
SCRIPT_VERSION = "1.0.0"
MAX_PLAN_BYTES = 2 * 1024 * 1024
SECRET_ENV_NAMES = {"QLIB_IFIND_TOKEN_FILE", "IFIND_REFRESH_TOKEN"}
ASSET_ROOT = Path(__file__).resolve().parents[1] / "assets" / "project-template"


class ConfigError(ValueError):
    pass


class PathSafetyError(ConfigError):
    pass


class PlanError(ConfigError):
    pass


class ManagedConflict(ConfigError):
    pass


class BootstrapRequest(NamedTuple):
    target: str
    project_name: str
    package: str
    profile: dict[str, Any]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash_payload(plan: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(plan)
    payload.pop("plan_hash", None)
    payload.pop("display_metadata", None)
    return payload


def compute_plan_hash(plan: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(_hash_payload(plan))).hexdigest()


def with_plan_hash(plan: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    result["plan_hash"] = compute_plan_hash(result)
    return result


def _reject_constant(value: str) -> None:
    raise ConfigError(f"non-JSON numeric constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: str | bytes) -> Any:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > MAX_PLAN_BYTES:
        raise ConfigError(f"plan exceeds {MAX_PLAN_BYTES} bytes")
    try:
        return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid plan JSON: {exc}") from exc


def strict_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read plan file: {exc}") from exc
    value = strict_json_loads(raw)
    if not isinstance(value, dict):
        raise ConfigError("plan root must be an object")
    return value


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"TOML root must be a table: {path}")
    return value


def machine_profile(
    name: str,
    *,
    python: str,
    day_provider: str,
    minute_provider: str,
    allowed_roots: list[str],
) -> dict[str, Any]:
    if not name or not python or not allowed_roots:
        raise ConfigError("profile name, python, and allowed roots are required")
    return {
        "schema_version": 1,
        "profile": name,
        "environment": {"python": python, "manager": "conda", "name": "qlib_ifind_beta"},
        "data_paths": {
            "QLIB_IFIND_DAY_DATA": day_provider,
            "QLIB_IFIND_MINUTE_DATA": minute_provider,
        },
        "credential_references": {
            "token_file_env": "QLIB_IFIND_TOKEN_FILE",
            "refresh_token_env": "IFIND_REFRESH_TOKEN",
        },
        "runtime": {"allowed_project_roots": allowed_roots},
    }


def builtin_profile(
    name: str,
    *,
    home: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if name != "h20-linux":
        raise ConfigError(f"unknown machine profile: {name}")
    selected_environ = os.environ if environ is None else environ
    selected_home = PurePosixPath(str(Path.home()) if home is None else os.fspath(home))
    if not selected_home.is_absolute():
        raise ConfigError("h20-linux home must be an absolute POSIX path")
    codex_home = PurePosixPath(selected_environ.get("CODEX_HOME", str(selected_home / ".codex")))
    if not codex_home.is_absolute():
        raise ConfigError("CODEX_HOME must be an absolute POSIX path")
    return machine_profile(
        "h20-linux",
        python=selected_environ.get(
            "QLIB_IFIND_PYTHON",
            str(selected_home / "miniconda3" / "envs" / "qlib_ifind_beta" / "bin" / "python"),
        ),
        day_provider=selected_environ.get(
            "QLIB_IFIND_DAY_DATA",
            str(selected_home / ".qlib" / "qlib_data" / "cn_data"),
        ),
        minute_provider=selected_environ.get(
            "QLIB_IFIND_MINUTE_DATA",
            str(selected_home / ".qlib" / "qlib_data" / "cn_data_1min"),
        ),
        allowed_roots=[str(selected_home / "projects"), str(codex_home / "worktrees")],
    )


def _contains_parent_reference(raw: str) -> bool:
    return ".." in PurePath(raw).parts


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _existing_ancestors_have_no_symlink(path: Path, stop: Path) -> None:
    current = path
    chain: list[Path] = []
    while True:
        chain.append(current)
        if current == stop or current.parent == current:
            break
        current = current.parent
    for item in reversed(chain):
        try:
            mode = item.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise PathSafetyError(f"symlink ancestor is not allowed: {item}")


def validate_target(target: Path, allowed_roots: Iterable[Path], providers: Iterable[Path] = ()) -> tuple[Path, Path]:
    raw = str(target)
    if not raw.strip() or _contains_parent_reference(raw):
        raise PathSafetyError("target is empty or contains '..'")
    absolute = _resolved(target)
    if absolute == Path(absolute.anchor) or absolute == Path.home().resolve():
        raise PathSafetyError("filesystem root and home root are not project targets")
    roots = [_resolved(root) for root in allowed_roots]
    for root in roots:
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise PathSafetyError(f"allowed root is too broad: {root}")
    matches = [root for root in roots if absolute != root and _is_within(absolute, root)]
    if not matches:
        raise PathSafetyError(f"target is outside allowed roots: {absolute}")
    root = max(matches, key=lambda item: len(item.parts))
    _existing_ancestors_have_no_symlink(absolute, root)
    for provider in providers:
        provider_real = _resolved(provider)
        if absolute == provider_real or _is_within(absolute, provider_real) or _is_within(provider_real, absolute):
            raise PathSafetyError(f"write target overlaps provider: {provider_real}")
    return absolute, root


AGENTS_BODY = (ASSET_ROOT / "AGENTS.managed.md").read_text(encoding="utf-8")
GITIGNORE_BODY = (ASSET_ROOT / "gitignore.managed").read_text(encoding="utf-8")


def _managed_spec(filename: str) -> tuple[str, str, str]:
    if filename == "AGENTS.md":
        digest = sha256_bytes(AGENTS_BODY.encode("utf-8"))
        return (
            f"<!-- qlib-init-project:managed version=1 schema=1 hash=sha256:{digest} -->",
            "<!-- /qlib-init-project:managed -->",
            AGENTS_BODY,
        )
    if filename == ".gitignore":
        digest = sha256_bytes(GITIGNORE_BODY.encode("utf-8"))
        return (
            f"# qlib-init-project:managed version=1 hash=sha256:{digest}",
            "# /qlib-init-project:managed",
            GITIGNORE_BODY,
        )
    raise ConfigError(f"not a managed file: {filename}")


def _managed_block(filename: str) -> str:
    start, end, body = _managed_spec(filename)
    return f"{start}\n{body}{end}\n"


def _inspect_managed(filename: str, existing: str) -> tuple[str, tuple[int, int] | None]:
    start_expected, end, _ = _managed_spec(filename)
    marker = "qlib-init-project:managed"
    start_positions = [m.start() for m in re.finditer(re.escape(marker), existing)]
    end_positions = [m.start() for m in re.finditer(re.escape(end), existing)]
    if not start_positions and not end_positions:
        return "absent", None
    if len(start_positions) != 2 or len(end_positions) != 1:
        raise ManagedConflict(f"duplicate or damaged managed markers in {filename}")
    # One occurrence is in the start marker and one in the end marker.
    starts = [line for line in existing.splitlines() if marker in line and "/qlib-init" not in line]
    if len(starts) != 1:
        raise ManagedConflict(f"duplicate or damaged managed start in {filename}")
    start_line = starts[0]
    begin = existing.find(start_line)
    body_begin = begin + len(start_line)
    if existing[body_begin:body_begin + 2] == "\r\n":
        body_begin += 2
    elif existing[body_begin:body_begin + 1] == "\n":
        body_begin += 1
    finish = existing.find(end, body_begin)
    if finish < 0:
        raise ManagedConflict(f"missing managed end in {filename}")
    body = existing[body_begin:finish]
    match = re.search(r"hash=sha256:([0-9a-f]{64})", start_line)
    if not match or sha256_bytes(body.encode("utf-8")) != match.group(1):
        raise ManagedConflict(f"managed body drift in {filename}")
    block_end = finish + len(end)
    if existing[block_end:block_end + 2] == "\r\n":
        block_end += 2
    elif existing[block_end:block_end + 1] == "\n":
        block_end += 1
    if start_line == start_expected and body == _managed_spec(filename)[2]:
        return "current", (begin, block_end)
    return "upgrade", (begin, block_end)


def render_managed_file(filename: str, existing: str) -> str:
    status, span = _inspect_managed(filename, existing)
    block = _managed_block(filename)
    if status == "absent":
        if not existing:
            return block
        separator = "" if existing.endswith(("\n", "\r")) else "\n"
        return existing + separator + block
    assert span is not None
    if status == "current":
        return existing
    return existing[: span[0]] + block + existing[span[1] :]


def render_manifest(request: BootstrapRequest) -> str:
    name = request.project_name
    package = request.package
    adjustment = "backward" if request.profile.get("profile") == "h20-linux" else "unknown"
    text = f'''schema_version = 1
strategy_profile = ""

[project]
name = {toml_string(name)}
package = {toml_string(package)}
layout = "package-root"

[environment]
mode = "reuse"
python_constraint = ">=3.12,<3.13"
qlib_constraint = "==0.9.7"
direct_dependencies = ["pyqlib==0.9.7"]

[data]
adjustment = {toml_string(adjustment)}
missing_policy = "fail-closed"

[data.field_units]
price = "adjusted"
volume = "unknown"

[data.providers.day]
frequency = "day"
uri_env = "QLIB_IFIND_DAY_DATA"
read_only = true

[data.providers.minute]
frequency = "1min"
uri_env = "QLIB_IFIND_MINUTE_DATA"
read_only = true

[data.calendar]
source = "day"

[data.instruments]
source = "project-profile"

[runtime]
workers = 4
cache_dir = "artifacts/cache"

[tracking]
backend = "qlib-file"
uri = "artifacts/mlruns"
experiment_name = {toml_string(name)}

[outputs]
root = "artifacts"

[validation]
level = "data"
instruments = []

[permissions]
requested_only = true
network = false
install = false
data_write = false
run_training = false
'''
    tomllib.loads(text)
    return text


def render_local_profile(profile: dict[str, Any]) -> str:
    env = profile["environment"]
    paths = profile["data_paths"]
    roots = profile["runtime"]["allowed_project_roots"]
    text = f'''schema_version = 1
profile = {toml_string(profile["profile"])}

[environment]
python = {toml_string(env["python"])}
manager = {toml_string(env.get("manager", "unknown"))}
name = {toml_string(env.get("name", ""))}

[data_paths]
QLIB_IFIND_DAY_DATA = {toml_string(paths["QLIB_IFIND_DAY_DATA"])}
QLIB_IFIND_MINUTE_DATA = {toml_string(paths["QLIB_IFIND_MINUTE_DATA"])}

[credential_references]
token_file_env = "QLIB_IFIND_TOKEN_FILE"
refresh_token_env = "IFIND_REFRESH_TOKEN"

[runtime]
allowed_project_roots = [{", ".join(toml_string(str(root)) for root in roots)}]
'''
    tomllib.loads(text)
    return text


def render_pyproject(request: BootstrapRequest) -> str:
    text = (ASSET_ROOT / "pyproject.toml.j2").read_text(encoding="utf-8")
    text = text.replace("__PROJECT_NAME_TOML__", toml_string(request.project_name))
    text = text.replace("__PACKAGE_TOML__", toml_string(request.package))
    tomllib.loads(text)
    return text


def render_strategy_example() -> str:
    return '''schema_version = 1
name = "example"

[universe]
source = "unknown"

[labels]
definition = "unknown"

[execution]
timing = "unknown"
costs = "unknown"
'''


def _desired_tree(request: BootstrapRequest) -> tuple[list[str], dict[str, str]]:
    package = request.package
    directories = [
        ".qlib-init/staging", ".qlib-init/journal", "config/machines", "config/strategies", f"{package}/data",
        f"{package}/factors", f"{package}/workflows", "research/experiments", "scripts",
        "tests/unit", "tests/contracts", "tests/integration", "artifacts/cache", "artifacts/logs",
        "artifacts/mlruns", "artifacts/reports",
    ]
    files = {
        "AGENTS.md": "",
        ".gitignore": "",
        "qlib-project.toml": render_manifest(request),
        "config/machines/local.toml": render_local_profile(request.profile),
        "config/machines/h20.example.toml": render_local_profile(request.profile),
        "config/strategies/example.toml": render_strategy_example(),
        "pyproject.toml": render_pyproject(request),
        f"{package}/__init__.py": '"""Qlib research package."""\n',
        f"{package}/data/__init__.py": '"""Data contracts and adapters."""\n',
        f"{package}/factors/__init__.py": '"""Replaceable factor modules."""\n',
        f"{package}/workflows/__init__.py": '"""Explicit research workflows."""\n',
    }
    return directories, files


def _valid_name(value: str, *, package: bool = False) -> None:
    pattern = r"[A-Za-z_][A-Za-z0-9_]*" if package else r"[A-Za-z0-9][A-Za-z0-9._-]*"
    if not re.fullmatch(pattern, value or ""):
        raise ConfigError(f"invalid {'package' if package else 'project'} name: {value!r}")


def _validate_profile_schema(profile: dict[str, Any]) -> None:
    allowed = {"schema_version", "profile", "environment", "data_paths", "credential_references", "runtime"}
    unknown = set(profile) - allowed
    if unknown:
        raise ConfigError(f"unknown machine profile fields: {sorted(unknown)}")
    if set(profile.get("environment", {})) - {"python", "manager", "name"}:
        raise ConfigError("unknown machine profile environment field")
    environment = profile.get("environment")
    if not isinstance(environment, dict) or not isinstance(environment.get("python"), str) or not environment["python"]:
        raise ConfigError("machine profile environment.python must be a non-empty string")
    if set(profile.get("data_paths", {})) != {"QLIB_IFIND_DAY_DATA", "QLIB_IFIND_MINUTE_DATA"}:
        raise ConfigError("machine profile must define exactly the day and minute providers")
    if set(profile.get("credential_references", {})) != {"token_file_env", "refresh_token_env"}:
        raise ConfigError("invalid credential reference fields")
    refs = profile["credential_references"]
    if refs != {"token_file_env": "QLIB_IFIND_TOKEN_FILE", "refresh_token_env": "IFIND_REFRESH_TOKEN"}:
        raise ConfigError("credential references must use the approved environment variable names")
    for value in profile["data_paths"].values():
        cross_platform_absolute = isinstance(value, str) and (value.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", value)))
        if not cross_platform_absolute:
            raise ConfigError("provider paths must be absolute strings")
    if set(profile.get("runtime", {})) != {"allowed_project_roots"}:
        raise ConfigError("invalid runtime fields")


def validate_manifest_schema(manifest: dict[str, Any]) -> None:
    top = {"schema_version", "strategy_profile", "project", "environment", "data", "runtime", "tracking", "outputs", "validation", "permissions"}
    if set(manifest) != top or manifest.get("schema_version") != 1:
        raise ConfigError("invalid manifest top-level schema")
    project = manifest.get("project")
    if not isinstance(project, dict) or set(project) != {"name", "package", "layout"} or not all(isinstance(project.get(k), str) and project[k] for k in ("name", "package", "layout")):
        raise ConfigError("invalid manifest project table")
    environment = manifest.get("environment")
    if not isinstance(environment, dict) or environment.get("mode") not in {"reuse", "declare", "create"} or not isinstance(environment.get("direct_dependencies"), list) or not all(isinstance(v, str) for v in environment["direct_dependencies"]):
        raise ConfigError("invalid manifest environment")
    data = manifest.get("data")
    if not isinstance(data, dict) or set(data) != {"adjustment", "missing_policy", "field_units", "providers", "calendar", "instruments"}:
        raise ConfigError("invalid manifest data table")
    if data.get("adjustment") not in {"backward", "forward", "raw", "unknown"} or data.get("missing_policy") != "fail-closed":
        raise ConfigError("invalid data contract")
    providers = data.get("providers")
    if not isinstance(providers, dict) or set(providers) != {"day", "minute"}:
        raise ConfigError("day and minute providers are required")
    for key, frequency in (("day", "day"), ("minute", "1min")):
        provider = providers[key]
        if not isinstance(provider, dict) or provider.get("frequency") != frequency or provider.get("read_only") is not True or not isinstance(provider.get("uri_env"), str):
            raise ConfigError(f"invalid {key} provider")
    if manifest.get("permissions") != {"requested_only": True, "network": False, "install": False, "data_write": False, "run_training": False}:
        raise ConfigError("manifest permissions may only deny side effects")
    for key in ("runtime", "tracking", "outputs", "validation"):
        if not isinstance(manifest.get(key), dict):
            raise ConfigError(f"manifest {key} must be a table")


def _file_hash(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _path_identity(path: Path) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return [int(info.st_dev), int(info.st_ino), int(stat.S_IFMT(info.st_mode))]


def build_plan(request: BootstrapRequest) -> dict[str, Any]:
    _valid_name(request.project_name)
    _valid_name(request.package, package=True)
    profile = request.profile
    _validate_profile_schema(profile)
    if profile.get("schema_version") != 1 or not isinstance(profile.get("runtime", {}).get("allowed_project_roots"), list):
        raise ConfigError("invalid machine profile schema")
    providers = [Path(value) for value in profile.get("data_paths", {}).values()]
    target, allowed_root = validate_target(
        Path(request.target),
        [Path(value) for value in profile["runtime"]["allowed_project_roots"]],
        providers,
    )
    directories, files = _desired_tree(request)
    actions: list[dict[str, Any]] = []
    for relative in directories:
        path = target / relative
        if path.exists() and path.is_dir() and not path.is_symlink():
            status_value = "skip"
        elif path.exists() or path.is_symlink():
            status_value = "conflict"
        else:
            status_value = "create"
        actions.append({"kind": "directory", "path": relative, "status": status_value})
    for relative, desired in files.items():
        path = target / relative
        expected_old_hash: str | None = None
        if relative in {"AGENTS.md", ".gitignore"}:
            if path.is_symlink():
                status_value, content, reason = "blocked", "", "managed target is a symlink"
            elif path.exists() and not path.is_file():
                status_value, content, reason = "conflict", "", "managed target is not a regular file"
            else:
                raw = path.read_bytes() if path.exists() else b""
                expected_old_hash = sha256_bytes(raw) if path.exists() else None
                try:
                    existing = raw.decode("utf-8")
                    content = render_managed_file(relative, existing)
                    status_value = "skip" if path.exists() and content.encode("utf-8") == raw else "create"
                    reason = "managed append or create" if status_value == "create" else "managed block current"
                except (UnicodeDecodeError, ManagedConflict) as exc:
                    status_value, content, reason = "blocked", "", str(exc)
        elif path.exists():
            if not path.is_file() or path.is_symlink():
                status_value, content, reason = "conflict", desired, "target is not a regular file"
            else:
                raw = path.read_bytes()
                expected_old_hash = sha256_bytes(raw)
                if raw == desired.encode("utf-8"):
                    status_value, content, reason = "skip", desired, "identical"
                else:
                    status_value, content, reason = "conflict", desired, "existing non-managed content differs"
        else:
            status_value, content, reason = "create", desired, "missing"
        action = {"kind": "file", "path": relative, "status": status_value, "content": content, "reason": reason}
        if expected_old_hash is not None:
            action["expected_old_hash"] = expected_old_hash
            action["expected_old_identity"] = _path_identity(path)
        actions.append(action)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "target": str(target),
        "target_identity": _path_identity(target),
        "allowed_root": str(allowed_root),
        "allowed_root_identity": _path_identity(allowed_root),
        "machine_profile": profile,
        "project": {"name": request.project_name, "package": request.package},
        "permissions": {"network": False, "install": False, "data_write": False, "run_training": False},
        "actions": actions,
        "display_metadata": {"summary": _action_summary(actions)},
    }
    return with_plan_hash(plan)


def _action_summary(actions: list[dict[str, Any]]) -> dict[str, int]:
    result = {key: 0 for key in ("create", "skip", "conflict", "blocked")}
    for action in actions:
        result[action["status"]] += 1
    return result


def write_plan_file(path: Path, plan: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(canonical_json(plan) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _linux_apply_capable() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.link in os.supports_dir_fd
        and os.replace in os.supports_dir_fd
    )


def _open_dir_at(parent_fd: int, name: str, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, 0o755, dir_fd=parent_fd)
        return os.open(name, flags, dir_fd=parent_fd)


def _walk_dir(root_fd: int, parts: Iterable[str], *, create: bool) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts:
            if part in {"", ".", ".."}:
                raise PathSafetyError(f"unsafe path component: {part!r}")
            child = _open_dir_at(fd, part, create=create)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


def _write_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]
    os.fsync(fd)


def _read_at(parent_fd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _publish_new(parent_fd: int, name: str, data: bytes, staging_fd: int, token: str) -> None:
    temp_name = f"{token}.tmp"
    fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=staging_fd)
    try:
        _write_fd(fd, data)
    finally:
        os.close(fd)
    try:
        os.link(temp_name, name, src_dir_fd=staging_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.unlink(temp_name, dir_fd=staging_fd)


def _replace_managed(parent_fd: int, name: str, data: bytes, expected_hash: str, token: str) -> None:
    if sha256_bytes(_read_at(parent_fd, name)) != expected_hash:
        raise PlanError(f"stale managed file: {name}")
    temp_name = f".{name}.{token}.tmp"
    fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent_fd)
    try:
        _write_fd(fd, data)
    finally:
        os.close(fd)
    if sha256_bytes(_read_at(parent_fd, name)) != expected_hash:
        os.unlink(temp_name, dir_fd=parent_fd)
        raise PlanError(f"managed file changed concurrently: {name}")
    os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def _put_json(parent_fd: int, name: str, payload: dict[str, Any], staging_fd: int, token: str) -> None:
    data = canonical_json(payload) + b"\n"
    try:
        old = _read_at(parent_fd, name)
    except FileNotFoundError:
        _publish_new(parent_fd, name, data, staging_fd, token)
    else:
        _replace_managed(parent_fd, name, data, sha256_bytes(old), token)


def _validate_plan(plan: dict[str, Any], confirmation: str) -> None:
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("script_version") != SCRIPT_VERSION:
        raise PlanError("unsupported plan schema or script version")
    recorded = plan.get("plan_hash")
    actual = compute_plan_hash(plan)
    if not isinstance(recorded, str) or recorded != actual or confirmation != actual:
        raise PlanError("plan hash or confirmation mismatch")
    permissions = plan.get("permissions")
    if permissions != {"network": False, "install": False, "data_write": False, "run_training": False}:
        raise PlanError("plan contains forbidden permissions")
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise PlanError("plan actions must be a list")
    for action in actions:
        if not isinstance(action, dict) or action.get("kind") not in {"file", "directory"} or action.get("status") not in {"create", "skip", "conflict", "blocked"}:
            raise PlanError("invalid action schema")
        value = action.get("path")
        if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or re.match(r"^[A-Za-z]:", value):
            raise PlanError(f"unsafe action path: {value!r}")
        path = PurePath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise PlanError(f"unsafe action path: {value!r}")


def validate_plan_freshness(plan: dict[str, Any]) -> None:
    target = Path(plan["target"])
    if _path_identity(Path(plan["allowed_root"])) != plan.get("allowed_root_identity"):
        raise PlanError("allowed root identity changed after plan")
    if _path_identity(target) != plan.get("target_identity"):
        raise PlanError("target identity changed after plan")
    for action in plan.get("actions", []):
        path = target / action["path"]
        if action["kind"] == "directory":
            if action["status"] == "create" and (path.exists() or path.is_symlink()):
                raise PlanError(f"planned directory appeared: {action['path']}")
            if action["status"] == "skip" and (not path.is_dir() or path.is_symlink()):
                raise PlanError(f"planned directory changed: {action['path']}")
            continue
        expected = action.get("expected_old_hash")
        if expected is None:
            if action["status"] == "create" and (path.exists() or path.is_symlink()):
                raise PlanError(f"planned file appeared: {action['path']}")
        else:
            if not path.is_file() or path.is_symlink() or _file_hash(path) != expected or _path_identity(path) != action.get("expected_old_identity"):
                raise PlanError(f"planned file changed: {action['path']}")


def apply_plan(plan: dict[str, Any], confirmation: str) -> dict[str, Any]:
    _validate_plan(plan, confirmation)
    validate_plan_freshness(plan)
    if not _linux_apply_capable():
        raise PathSafetyError("apply requires Linux dir_fd, O_NOFOLLOW, O_DIRECTORY, hard-link and replace support")
    profile = plan["machine_profile"]
    providers = [Path(value) for value in profile["data_paths"].values()]
    target, allowed_root = validate_target(
        Path(plan["target"]),
        [Path(value) for value in profile["runtime"]["allowed_project_roots"]],
        providers,
    )
    if str(allowed_root) != plan["allowed_root"]:
        raise PlanError("allowed root identity changed")
    root_fd = os.open(allowed_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    target_fd = -1
    staging_fd = -1
    completed: list[str] = []
    try:
        root_info = os.fstat(root_fd)
        opened_identity = [int(root_info.st_dev), int(root_info.st_ino), int(stat.S_IFMT(root_info.st_mode))]
        if opened_identity != plan.get("allowed_root_identity"):
            raise PlanError("opened allowed root does not match the reviewed plan")
        target_fd = _walk_dir(root_fd, target.relative_to(allowed_root).parts, create=True)
        for action in plan["actions"]:
            if action["kind"] != "directory" or action["status"] != "create":
                continue
            fd = _walk_dir(target_fd, PurePath(action["path"]).parts, create=True)
            os.close(fd)
            completed.append(action["path"])
        staging_fd = _walk_dir(target_fd, (".qlib-init", "staging"), create=True)
        journal_fd = _walk_dir(target_fd, (".qlib-init", "journal"), create=True)
        journal_name = plan["plan_hash"] + ".json"
        try:
            _put_json(journal_fd, journal_name, {"status": "running", "plan_hash": plan["plan_hash"], "completed": completed}, staging_fd, "journal-start-" + plan["plan_hash"][:12])
            try:
                for index, action in enumerate(plan["actions"]):
                    if action["kind"] != "file" or action["status"] != "create":
                        continue
                    relative = PurePath(action["path"])
                    parent_fd = _walk_dir(target_fd, relative.parts[:-1], create=True)
                    try:
                        data = action["content"].encode("utf-8")
                        token = f"{plan['plan_hash'][:12]}-{index}"
                        if "expected_old_hash" in action:
                            _replace_managed(parent_fd, relative.name, data, action["expected_old_hash"], token)
                        else:
                            _publish_new(parent_fd, relative.name, data, staging_fd, token)
                        completed.append(action["path"])
                        _put_json(journal_fd, journal_name, {"status": "running", "plan_hash": plan["plan_hash"], "completed": completed}, staging_fd, "journal-step-" + token)
                    finally:
                        os.close(parent_fd)
            except Exception as exc:
                _put_json(journal_fd, journal_name, {"status": "failed", "plan_hash": plan["plan_hash"], "completed": completed, "error": type(exc).__name__}, staging_fd, "journal-failed-" + plan["plan_hash"][:12])
                raise
            _put_json(journal_fd, journal_name, {"status": "complete", "plan_hash": plan["plan_hash"], "completed": completed}, staging_fd, "journal-done-" + plan["plan_hash"][:12])
        finally:
            os.close(journal_fd)
        state = {
            "schema_version": 1,
            "template_version": TEMPLATE_VERSION,
            "last_successful_plan_hash": plan["plan_hash"],
            "completed": completed,
            "managed_files": {action["path"]: sha256_bytes(action["content"].encode("utf-8")) for action in plan["actions"] if action.get("path") in {"AGENTS.md", ".gitignore"} and action.get("content")},
        }
        state_parent = _walk_dir(target_fd, (".qlib-init",), create=True)
        try:
            state_data = canonical_json(state) + b"\n"
            try:
                old = _read_at(state_parent, "state.json")
            except FileNotFoundError:
                _publish_new(state_parent, "state.json", state_data, staging_fd, "state-" + plan["plan_hash"][:12])
            else:
                _replace_managed(state_parent, "state.json", state_data, sha256_bytes(old), "state-" + plan["plan_hash"][:12])
        finally:
            os.close(state_parent)
        return {"status": "pass", "plan_hash": plan["plan_hash"], "completed": completed}
    finally:
        for fd in (staging_fd, target_fd, root_fd):
            if fd >= 0:
                os.close(fd)


def snapshot_tree(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        info = path.lstat()
        item: dict[str, Any] = {"path": path.relative_to(root).as_posix(), "mode": stat.S_IFMT(info.st_mode), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
        if path.is_file() and not path.is_symlink():
            item["sha256"] = _file_hash(path)
        elif path.is_symlink():
            item["link"] = os.readlink(path)
        result.append(item)
    return result


def snapshot_provider_sample(provider: Path, frequency: str, instrument: str, field: str) -> list[dict[str, Any]]:
    paths = [
        provider / "calendars" / f"{frequency}.txt",
        provider / "instruments" / "all.txt",
        provider / "features" / instrument.lower() / f"{field}.{frequency}.bin",
    ]
    result: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            result.append({"path": str(path), "exists": False})
            continue
        info = path.lstat()
        result.append({"path": str(path), "exists": True, "size": info.st_size, "mtime_ns": info.st_mtime_ns, "sha256": _file_hash(path) if path.is_file() else None})
    return result


def doctor_data(provider: Path, *, frequency: str, instrument: str, field: str) -> dict[str, Any]:
    if not provider.is_dir():
        return {"status": "blocked", "reason": "provider directory missing", "provider": str(provider)}
    calendar = provider / "calendars" / f"{frequency}.txt"
    instruments = provider / "instruments" / "all.txt"
    feature = provider / "features" / instrument.lower() / f"{field}.{frequency}.bin"
    missing = [str(path) for path in (calendar, instruments, feature) if not path.is_file()]
    if missing:
        return {"status": "blocked", "reason": "sample files missing", "missing": missing, "provider": str(provider)}
    lines = calendar.read_text(encoding="utf-8").splitlines()
    instrument_lines = instruments.read_text(encoding="utf-8").splitlines()
    size = feature.stat().st_size
    with feature.open("rb") as handle:
        head = handle.read(4)
    try:
        for value in (lines[0], lines[-1]):
            datetime.fromisoformat(value)
    except (IndexError, ValueError):
        return {"status": "error", "reason": "invalid calendar format", "provider": str(provider)}
    instrument_present = any(line.split("\t", 1)[0].upper() == instrument.upper() for line in instrument_lines)
    if not lines or not instrument_lines or not instrument_present or size < 4 or size % 4:
        return {"status": "error", "reason": "invalid calendar, instrument, or bin shape", "provider": str(provider)}
    start_index = struct.unpack("<f", head)[0]
    if not math.isfinite(start_index):
        return {"status": "error", "reason": "non-finite bin start index", "provider": str(provider)}
    return {
        "status": "pass",
        "provider": str(provider),
        "calendar": {"rows": len(lines), "first": lines[0], "last": lines[-1]},
        "instrument_records": len(instrument_lines),
        "sample": {"instrument": instrument, "field": field, "bytes": size},
        "start_index": start_index,
        "contracts": {"adjustment": "unknown", "volume_unit": "unknown"},
    }


def doctor_environment(python: Path, *, expected_qlib: str | None = "0.9.7", timeout: int = 20) -> dict[str, Any]:
    if not python.is_file():
        return {"status": "blocked", "reason": "interpreter missing", "python": str(python), "python_version": "unknown"}
    script = r'''import importlib.metadata as m, json, sys
result = {"python_version": sys.version.split()[0]}
try:
    result["pyqlib_version"] = m.version("pyqlib")
    import qlib
    result["qlib_version"] = getattr(qlib, "__version__", "unknown")
    result["qlib_file"] = getattr(qlib, "__file__", "unknown")
except Exception as exc:
    result["error"] = type(exc).__name__ + ": " + str(exc)
print(json.dumps(result, sort_keys=True))
'''
    with tempfile.TemporaryDirectory(prefix="qlib-doctor-") as temp:
        env = isolated_environment(temp)
        try:
            completed = subprocess.run([str(python), "-I", "-B", "-c", script], shell=False, capture_output=True, text=True, env=env, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "error", "reason": str(exc), "python": str(python), "python_version": "unknown"}
    try:
        detail = strict_json_loads(completed.stdout.strip()) if completed.stdout.strip() else {}
    except ConfigError:
        detail = {"error": "unparseable interpreter output"}
    status_value = "pass" if completed.returncode == 0 and "error" not in detail else "blocked"
    if expected_qlib and detail.get("pyqlib_version") != expected_qlib:
        status_value = "blocked"
    return {"status": status_value, "python": str(python), **detail, "returncode": completed.returncode}


def isolated_environment(temp: str) -> dict[str, str]:
    allowed = {"PATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL", "CONDA_PREFIX", "SYSTEMROOT", "WINDIR"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1", "MPLCONFIGDIR": temp,
        "XDG_CACHE_HOME": temp, "XDG_CONFIG_HOME": temp, "XDG_DATA_HOME": temp,
        "TMPDIR": temp, "TEMP": temp, "TMP": temp,
    })
    return env


def doctor_structure(target: Path) -> dict[str, Any]:
    required = [target / "qlib-project.toml", target / "config" / "machines" / "local.toml", target / "AGENTS.md", target / ".gitignore"]
    missing = [str(path.relative_to(target)) for path in required if not path.is_file()]
    if missing:
        return {"status": "blocked", "missing": missing}
    try:
        manifest = load_toml(required[0])
        profile = load_toml(required[1])
        validate_manifest_schema(manifest)
        _validate_profile_schema(profile)
        if manifest.get("schema_version") != 1 or profile.get("schema_version") != 1:
            raise ConfigError("unsupported schema")
        for filename in ("AGENTS.md", ".gitignore"):
            status_value, _ = _inspect_managed(filename, (target / filename).read_text(encoding="utf-8", newline=""))
            if status_value not in {"current", "upgrade"}:
                raise ManagedConflict(f"missing managed block: {filename}")
    except (ConfigError, ManagedConflict, UnicodeError) as exc:
        return {"status": "error", "reason": str(exc)}
    return {"status": "pass", "project": manifest.get("project", {}).get("name", "unknown"), "profile": profile.get("profile", "unknown")}


def _load_existing_request(target: Path, profile: dict[str, Any]) -> BootstrapRequest:
    manifest = load_toml(target / "qlib-project.toml")
    validate_manifest_schema(manifest)
    project = manifest.get("project")
    if not isinstance(project, dict):
        raise ConfigError("manifest [project] table missing")
    return BootstrapRequest(str(target), project.get("name", ""), project.get("package", ""), profile)


def _profile_from_file(path: Path) -> dict[str, Any]:
    profile = load_toml(path)
    allowed = profile.get("runtime", {}).get("allowed_project_roots")
    data_paths = profile.get("data_paths")
    environment = profile.get("environment")
    if profile.get("schema_version") != 1 or not isinstance(allowed, list) or not isinstance(data_paths, dict) or not isinstance(environment, dict):
        raise ConfigError("invalid local profile schema")
    return profile


def _request_from_args(args: argparse.Namespace) -> BootstrapRequest:
    target = Path(args.target)
    if args.profile_file:
        profile = _profile_from_file(Path(args.profile_file))
    elif args.machine_profile:
        profile = builtin_profile(args.machine_profile)
    elif (target / "config" / "machines" / "local.toml").is_file():
        profile = _profile_from_file(target / "config" / "machines" / "local.toml")
    else:
        raise ConfigError("machine profile is required for bootstrap")
    if args.project_name or args.package:
        if not args.project_name or not args.package:
            raise ConfigError("project-name and package must be supplied together")
        return BootstrapRequest(str(target), args.project_name, args.package, profile)
    return _load_existing_request(target, profile)


def command_plan(args: argparse.Namespace) -> int:
    plan = build_plan(_request_from_args(args))
    summary = plan["display_metadata"]["summary"]
    print(f"plan {plan['plan_hash']} {summary}", file=sys.stderr)
    for action in plan["actions"]:
        print(f"{action['status'].upper():8} {action['kind']:9} {action['path']} {action.get('reason', '')}", file=sys.stderr)
    if args.plan_file:
        plan_path = Path(args.plan_file).expanduser().resolve(strict=False)
        target = Path(plan["target"])
        if plan_path == target or _is_within(plan_path, target):
            raise PathSafetyError("plan file must be outside the target project")
        write_plan_file(plan_path, plan)
    else:
        sys.stdout.buffer.write(canonical_json(plan) + b"\n")
    return 2 if summary["blocked"] or summary["conflict"] else 0


def command_apply(args: argparse.Namespace) -> int:
    plan = strict_json_file(Path(args.plan_file))
    result = apply_plan(plan, args.confirm_plan_hash)
    sys.stdout.buffer.write(canonical_json(result) + b"\n")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser().resolve(strict=False)
    target_before = snapshot_tree(target)
    profile_path = target / "config" / "machines" / "local.toml"
    layers = [args.layer] if args.layer != "all" else ["structure", "environment", "data"]
    results: dict[str, Any] = {}
    profile: dict[str, Any] | None = None
    provider_before: list[dict[str, Any]] | None = None
    provider_path: Path | None = None
    if profile_path.is_file():
        try:
            profile = _profile_from_file(profile_path)
        except ConfigError as exc:
            results["profile"] = {"status": "error", "reason": str(exc)}
    for layer in layers:
        if layer == "structure":
            results[layer] = doctor_structure(target)
        elif layer == "environment":
            results[layer] = doctor_environment(Path(profile["environment"]["python"])) if profile else {"status": "blocked", "reason": "profile unavailable"}
        elif layer == "data":
            if not profile:
                results[layer] = {"status": "blocked", "reason": "profile unavailable"}
            else:
                key = "QLIB_IFIND_MINUTE_DATA" if args.frequency == "1min" else "QLIB_IFIND_DAY_DATA"
                provider_path = Path(profile["data_paths"][key])
                provider_before = snapshot_provider_sample(provider_path, args.frequency, args.instrument, args.field)
                results[layer] = doctor_data(provider_path, frequency=args.frequency, instrument=args.instrument, field=args.field)
    statuses = {result.get("status") for result in results.values() if isinstance(result, dict)}
    if snapshot_tree(target) != target_before:
        results["write_guard"] = {"status": "error", "reason": "target changed during doctor"}
        statuses.add("error")
    if provider_path is not None and snapshot_provider_sample(provider_path, args.frequency, args.instrument, args.field) != provider_before:
        results["provider_write_guard"] = {"status": "error", "reason": "provider changed during doctor"}
        statuses.add("error")
    overall = "error" if "error" in statuses else "blocked" if "blocked" in statuses else "pass"
    output = {"status": overall, "target": str(target), "layers": results}
    sys.stdout.buffer.write(canonical_json(output) + b"\n")
    return {"pass": 0, "blocked": 2, "error": 3}[overall]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qlib_init_project.py", allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", aliases=["dry-run"], allow_abbrev=False, help="compute a no-write initialization plan")
    plan.add_argument("--target", required=True)
    plan.add_argument("--project-name")
    plan.add_argument("--package")
    profiles = plan.add_mutually_exclusive_group()
    profiles.add_argument("--machine-profile")
    profiles.add_argument("--profile-file")
    plan.add_argument("--plan-file")
    plan.set_defaults(handler=command_plan)
    apply = subparsers.add_parser("apply", allow_abbrev=False, help="apply an exact confirmed plan on Linux")
    apply.add_argument("--plan-file", required=True)
    apply.add_argument("--confirm-plan-hash", required=True)
    apply.set_defaults(handler=command_apply)
    doctor = subparsers.add_parser("doctor", allow_abbrev=False, help="run persistent-write-free diagnostics")
    doctor.add_argument("--target", required=True)
    doctor.add_argument("--layer", choices=["all", "structure", "environment", "data"], default="all")
    doctor.add_argument("--frequency", choices=["day", "1min"], default="day")
    doctor.add_argument("--instrument", default="SH600519")
    doctor.add_argument("--field", default="close")
    doctor.set_defaults(handler=command_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
