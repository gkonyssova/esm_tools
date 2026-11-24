"""Which code version produced each component, as a ``components`` table.

The Collection gets one row per component and ESM-Tools; each Item gets the
row of its own component (see :func:`component_row`). Sources, in order:

1. ``log/<expid>_vcs_info_initial.yaml``, recorded by esm_runscripts'
   ``add_vcs_info`` during the run ("recorded");
2. otherwise git, run in each ``model_dir`` at scan time ("reconstructed":
   the folder may have changed since the run).

Each component's ``build`` fingerprint (its programs and libraries in the
latest run's ``bin`` folder) decides whether it changed between scans.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
import subprocess
from datetime import date, timedelta
from typing import Any, Optional

import yaml
from loguru import logger
from upath import UPath

from esm_catalog.models import ExperimentMetadata
from esm_catalog.plugins import hookimpl

_DIFF_LINE_CAP = 500
"""Full diffs beyond this are replaced with a stat-only note -- a drifted
shared checkout can be arbitrarily dirty, and this extension must not dump an
unbounded diff into a Collection document."""

_GIT_TIMEOUT_SECONDS = 10

_RECONSTRUCTED_NOTE = (
    "Reconstructed after the fact from the shared model_dir, not a "
    "per-experiment snapshot -- may not reflect the exact code state "
    "actually used for this run (drift is possible)."
)


def _vcs_info_initial_path(exp_root: UPath, experiment_id: str) -> UPath:
    return exp_root / "log" / f"{experiment_id}_vcs_info_initial.yaml"


def read_vcs_info_initial(exp_metadata: ExperimentMetadata) -> Optional[dict]:
    """The authoritative, frozen VCS snapshot, or ``None`` if never written."""
    # exp_metadata.experiment_path is already the real UPath (storage_options
    # and all) -- re-deriving one from str() loses the host for remote
    # protocols (sftp://, ...); confirmed live (SSHFileSystem.__init__()
    # missing 'host').
    path = _vcs_info_initial_path(exp_metadata.experiment_path, exp_metadata.experiment_id)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except Exception:  # noqa: BLE001 -- a corrupt file is not fatal to the scan
        return None
    return data if isinstance(data, dict) else None


def _ssh_host(exp_metadata: ExperimentMetadata) -> Optional[str]:
    """The remote host *model_dirs* live on, or ``None`` for a local scan.

    ``model_dirs`` are plain path strings taken from the finished_config,
    which was itself written on whatever host ran the experiment -- the same
    host ``experiment_path`` points at. A local ``git -C <model_dir>``
    subprocess only makes sense when that host is this machine.
    """
    path = exp_metadata.experiment_path
    if getattr(path, "protocol", None) in ("ssh", "sftp"):
        return getattr(path, "storage_options", {}).get("host") or None
    return None


def _run_git(path: str, *args: str, host: Optional[str] = None) -> Optional[str]:
    if host is None:
        argv = ["git", "-C", path, *args]
    else:
        # ssh joins trailing argv with bare spaces, not shell-quoting --
        # build one already-quoted remote command string ourselves.
        argv = ["ssh", host, shlex.join(["git", "-C", path, *args])]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _is_git_checkout(path: str, host: Optional[str] = None) -> bool:
    return _run_git(path, "rev-parse", "--is-inside-work-tree", host=host) == "true"


def _esm_tools_version(path: str, host: Optional[str] = None) -> Optional[str]:
    """ESM-Tools' version number from its code, e.g. "6.67.0" (its git tags are not release names)."""
    source = _run_git(path, "show", "HEAD:src/esm_tools/__init__.py", host=host) or ""
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", source, re.MULTILINE)
    return match.group(1) if match else None


def _reconstruct_from_model_dir(
    component: str, model_dir: str, host: Optional[str] = None
) -> dict[str, Any]:
    """Like :func:`_reconstruct_component_vcs`, but also tries ``<model_dir>/<component>``."""
    if component == "esm_tools" and _is_git_checkout(model_dir, host):
        found = _reconstruct_component_vcs(model_dir, host=host)
        found["software_version"] = _esm_tools_version(model_dir, host) or found["software_version"]
        return found
    if _is_git_checkout(model_dir, host):
        return _reconstruct_component_vcs(model_dir, host=host)
    subfolder = posixpath.join(model_dir, component)
    if _is_git_checkout(subfolder, host):
        found = _reconstruct_component_vcs(subfolder, host=host)
        found["note"] = f"found in the {component} subfolder of model_dir"
        return found
    return _reconstruct_component_vcs(model_dir, host=host)


def _reconstruct_component_vcs(path: str, host: Optional[str] = None) -> dict[str, Any]:
    """Live git state for *path*, marked as a reconstruction, never a record.

    Mirrors ``esm_runscripts.helpers.get_all_git_info``'s shape (path, hash,
    branch_name, remote, diffs), minus that module's own dependency, plus the
    ``reconstructed``/``note`` pair this extension adds everywhere it has to
    guess instead of read a recorded fact.

    *host* runs every git invocation over ``ssh <host> ...`` instead of a
    local subprocess -- required when *path* is a model_dir on the same
    remote machine ``exp_root`` was scanned from (see :func:`_ssh_host`);
    passwordless ssh to that host is assumed, same as the sftp access the
    scan itself already needs to reach ``exp_root``.
    """
    note = _RECONSTRUCTED_NOTE
    if not _is_git_checkout(path, host):
        reason = "unreachable over ssh" if host else "not a git-controlled checkout"
        return {"path": path, "reconstructed": True, "note": reason}

    diff = _run_git(path, "diff", host=host) or ""
    if diff.count("\n") > _DIFF_LINE_CAP:
        stat = _run_git(path, "diff", "--stat", host=host) or ""
        diff = f"[diff truncated, over {_DIFF_LINE_CAP} lines -- stat: {stat.strip().splitlines()[-1] if stat else 'unavailable'}]"

    return {
        "path": path,
        "hash": _run_git(path, "rev-parse", "--short", "HEAD", host=host),
        "branch_name": _run_git(path, "rev-parse", "--abbrev-ref", "HEAD", host=host),
        # Nearest release tag, e.g. "2.7.7" or "2.7.7-3-gcdbab44f".
        "software_version": _run_git(path, "describe", "--tags", host=host),
        "remote": _run_git(path, "remote", "get-url", "origin", host=host),
        "diffs": diff,
        "reconstructed": True,
        "note": note,
    }


def vcs_by_component(exp_metadata: ExperimentMetadata) -> dict[str, dict]:
    """Every component's VCS state (frozen snapshot if present, else reconstructed)."""
    initial = read_vcs_info_initial(exp_metadata)
    if initial is not None:
        return initial
    host = _ssh_host(exp_metadata)
    return {
        component: _reconstruct_from_model_dir(component, model_dir, host=host)
        for component, model_dir in exp_metadata.model_dirs.items()
    }


#: collection_id -> already-computed vcs_by_component() result, computed once
#: per experiment (reading the snapshot file or shelling out to git is not
#: free, and every Item in the experiment would otherwise trigger it again).
_SOFTWARE_CACHE: dict[str, dict] = {}


def build_fingerprint(folder: UPath) -> Optional[str]:
    """A checksum of every file under *folder* (programs and libraries), or None if empty."""
    if not folder.exists():
        return None
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(folder)).encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()[:12]


def _add_builds(exp_metadata: ExperimentMetadata, software: dict) -> None:
    """Add each component's ``build`` fingerprint from the latest run's ``bin`` folder."""
    root = exp_metadata.experiment_path
    for component in exp_metadata.components:
        candidates = [root / "bin" / component]
        if exp_metadata.latest_run:
            candidates.insert(0, root / exp_metadata.latest_run / "bin" / component)
        build = next(filter(None, map(build_fingerprint, candidates)), None)
        if build is None:
            continue
        entry = software.get(component)
        if not isinstance(entry, dict):  # missing, or recorded as plain text
            entry = software[component] = {"note": entry} if entry else {}
        entry["build"] = build


def _vcs_by_component_once(exp_metadata: ExperimentMetadata) -> dict[str, dict]:
    collection_id = exp_metadata.collection_id
    if collection_id in _SOFTWARE_CACHE:
        return _SOFTWARE_CACHE[collection_id]
    software = vcs_by_component(exp_metadata)
    _add_builds(exp_metadata, software)
    # Say once per experiment why a version was not found, or found elsewhere.
    for component, component_vcs in software.items():
        if isinstance(component_vcs, dict) and "subfolder" in str(component_vcs.get("note")):
            logger.warning(
                "vcs:* info for component {!r} in {} read from {}: its model_dir "
                "is not a git checkout",
                component,
                collection_id,
                component_vcs.get("path"),
            )
        if isinstance(component_vcs, dict) and not component_vcs.get("hash"):
            logger.warning(
                "No vcs:* info for component {!r} in {}: {}",
                component,
                collection_id,
                component_vcs.get("note", "no revision hash recorded"),
            )
    _SOFTWARE_CACHE[collection_id] = software
    return software


def _public_remote(remote: str) -> str:
    """*remote* without any login, e.g. ``https://user:token@host/x`` -> ``https://host/x``."""
    return re.sub(r"//[^/@]+@", "//", remote)


def _is_reconstructed(component_vcs: Any) -> bool:
    """Whether a version was found, by git at scan time rather than recorded."""
    return isinstance(component_vcs, dict) and bool(
        component_vcs.get("hash") and component_vcs.get("reconstructed")
    )


def component_row(component_vcs: Any) -> dict[str, Any]:
    """One row of the ``components`` table: version, revision, branch, remote."""
    if not isinstance(component_vcs, dict) or not component_vcs.get("hash"):
        return {"status": "not found"}
    row: dict[str, Any] = {}
    if component_vcs.get("software_version"):
        row["version"] = component_vcs["software_version"]
    row["revision"] = component_vcs["hash"]
    if component_vcs.get("branch_name") not in (None, "", "HEAD"):  # "HEAD": no branch
        row["branch"] = component_vcs["branch_name"]
    if component_vcs.get("remote"):
        row["remote"] = _public_remote(component_vcs["remote"])
    return row


#: (collection_id, component) pairs already warned about by
#: add_vcs_item_extension -- called once per Item, so without this a
#: component missing from model_dirs/the snapshot entirely would log once
#: per file instead of once per experiment.
_MISSING_COMPONENT_WARNED: set[tuple[str, str]] = set()


def add_vcs_item_extension(item, component: str, exp_metadata: ExperimentMetadata) -> None:
    """Set ``components`` on *item* to its own component's row."""
    component_vcs = _vcs_by_component_once(exp_metadata).get(component)
    if component_vcs is None:
        key = (exp_metadata.collection_id, component)
        if key not in _MISSING_COMPONENT_WARNED:
            _MISSING_COMPONENT_WARNED.add(key)
            logger.warning(
                "No vcs:* info for component {!r} in {}: not present in "
                "the vcs_info_initial snapshot or model_dirs at all.",
                component,
                exp_metadata.collection_id,
            )
        component_vcs = {}
    item.properties["components"] = {component: component_row(component_vcs)}
    if _is_reconstructed(component_vcs):
        item.properties["software_note"] = RECONSTRUCTED_FOOTNOTE


RECONSTRUCTED_FOOTNOTE = (
    "Reconstructed: read from the code folder at scan time, not recorded "
    "during the run; may differ from the code actually used."
)
"""Set once per Collection or Item when a version is reconstructed."""


def _day(when: Any) -> Optional[str]:
    return when.date().isoformat() if when is not None else None


def _next_day(day: Optional[str]) -> Optional[str]:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat() if day else None


def updated_history(exp_metadata: ExperimentMetadata) -> dict[str, list[dict[str, str]]]:
    """Earlier scans' version history, extended by this scan.

    A component changed when its ``build`` fingerprint changed, or, without one
    (e.g. ESM-Tools), when its revision changed. Unchanged: the last entry now
    reaches the end of the run. Changed: a new entry from the day after the last
    one ended (dates are inclusive).
    """
    history = {
        name: [dict(entry) for entry in entries]
        for name, entries in exp_metadata.software_history.items()
    }
    start, end = _day(exp_metadata.run_start), _day(exp_metadata.run_end)
    for name, component_vcs in _vcs_by_component_once(exp_metadata).items():
        row = component_row(component_vcs)
        build = component_vcs.get("build") if isinstance(component_vcs, dict) else None
        if "revision" not in row and build is None:
            continue
        entries = history.setdefault(name, [])
        last = entries[-1] if entries else None
        if last is not None and _same_code(last, row.get("revision"), build):
            last["to"] = end
            if build:
                last.setdefault("build", build)
            continue
        entry = {key: row[key] for key in ("version", "revision") if key in row}
        if build:
            entry["build"] = build
        entry["from"] = _next_day(last["to"]) if last else start
        entry["to"] = end
        entries.append(entry)
    return history


def _same_code(last: dict, revision: Optional[str], build: Optional[str]) -> bool:
    """Same program (by build), or same revision when either side has no build."""
    if build and last.get("build"):
        return last["build"] == build
    return last.get("revision") == revision


def _published_history(entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    """*entries* without build fingerprints (kept local); a rebuild of the same revision is marked ``rebuilt``."""
    published = []
    for previous, entry in zip([None, *entries], entries):
        shown: dict[str, Any] = {k: v for k, v in entry.items() if k != "build"}
        if previous is not None and previous.get("revision") == entry.get("revision"):
            shown["rebuilt"] = True
        published.append(shown)
    return published


def add_components_table(collection, exp_metadata: ExperimentMetadata) -> None:
    """Set ``components`` to ``{name: row}``, ESM-Tools included; changed ones get ``changed`` and ``history``."""
    software = dict(_vcs_by_component_once(exp_metadata))
    for component in exp_metadata.components:
        software.setdefault(component, {})
    history = updated_history(exp_metadata)
    table = {}
    for name in sorted(software):
        table[name] = component_row(software[name])
        if len(history.get(name, [])) > 1:
            table[name]["changed"] = True
            table[name]["history"] = _published_history(history[name])
    collection.extra_fields["components"] = table
    if any(_is_reconstructed(v) for v in software.values()):
        collection.extra_fields["software_note"] = RECONSTRUCTED_FOOTNOTE


@hookimpl
def apply_to_collection(collection, exp_metadata, hints) -> None:
    add_components_table(collection, exp_metadata)


@hookimpl
def apply_to_item(item, file_metadata, exp_metadata, hints) -> None:
    component = file_metadata.component
    if not component:
        return
    add_vcs_item_extension(item, component, exp_metadata)
