"""VCS/provenance extension: read the frozen snapshot, or reconstruct live."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import yaml
from upath import UPath

from esm_catalog.models import ExperimentMetadata
from esm_catalog.vcs import (
    RECONSTRUCTED_FOOTNOTE,
    _ssh_host,
    add_components_table,
    _vcs_by_component_once,
    add_vcs_item_extension,
    read_vcs_info_initial,
    vcs_by_component,
)


def _experiment_metadata(exp_root: Path, **kwargs) -> ExperimentMetadata:
    return ExperimentMetadata(
        experiment_id="exp1", experiment_path=exp_root, **kwargs
    )


def _git_repo(path: Path, remote: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "f.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)


def test_reads_frozen_snapshot_when_present(tmp_path):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    frozen = {"echam": {"path": "/x", "hash": "abc123", "branch_name": "release"}}
    (log_dir / "exp1_vcs_info_initial.yaml").write_text(yaml.dump(frozen))

    em = _experiment_metadata(tmp_path)
    assert read_vcs_info_initial(em) == frozen
    assert vcs_by_component(em) == frozen


def test_no_snapshot_file_returns_none(tmp_path):
    em = _experiment_metadata(tmp_path)
    assert read_vcs_info_initial(em) is None


def test_reconstructs_live_from_model_dir_when_no_snapshot(tmp_path):
    model_dir = tmp_path / "models" / "echam"
    _git_repo(model_dir, remote="https://example.com/echam.git")

    em = _experiment_metadata(tmp_path, model_dirs={"echam": str(model_dir)})
    software = vcs_by_component(em)

    assert software["echam"]["reconstructed"] is True
    assert software["echam"]["remote"] == "https://example.com/echam.git"
    assert software["echam"]["branch_name"] in ("main", "master")
    assert software["echam"]["hash"]
    assert "note" in software["echam"]


def test_reconstruction_reports_the_release_tag(tmp_path):
    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)
    subprocess.run(["git", "tag", "2.7.7"], cwd=model_dir, check=True)

    em = _experiment_metadata(tmp_path, model_dirs={"fesom": str(model_dir)})

    assert vcs_by_component(em)["fesom"]["software_version"] == "2.7.7"


def test_reconstruction_without_tags_has_no_software_version(tmp_path):
    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)

    em = _experiment_metadata(tmp_path, model_dirs={"fesom": str(model_dir)})

    assert vcs_by_component(em)["fesom"]["software_version"] is None


def test_checkout_at_a_tag_shows_the_version_not_head(tmp_path, item):
    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)
    subprocess.run(["git", "tag", "2.7.7"], cwd=model_dir, check=True)
    subprocess.run(["git", "checkout", "-q", "2.7.7"], cwd=model_dir, check=True)
    em = _experiment_metadata(tmp_path, model_dirs={"fesom": str(model_dir)})

    add_vcs_item_extension(item, "fesom", em)

    row = item.properties["components"]["fesom"]
    assert row["version"] == "2.7.7"
    assert "branch" not in row


def test_reconstruction_flags_non_git_directory(tmp_path):
    model_dir = tmp_path / "models" / "xios"
    model_dir.mkdir(parents=True)

    em = _experiment_metadata(tmp_path, model_dirs={"xios": str(model_dir)})
    software = vcs_by_component(em)

    assert software["xios"]["reconstructed"] is True
    assert software["xios"]["note"] == "not a git-controlled checkout"


def test_item_gets_its_own_component_row(tmp_path, item):
    model_dir = tmp_path / "models" / "echam"
    _git_repo(model_dir, remote="https://user:token@example.com/echam.git")
    em = _experiment_metadata(tmp_path, model_dirs={"echam": str(model_dir)})

    add_vcs_item_extension(item, "echam", em)

    row = item.properties["components"]["echam"]
    assert row["revision"]
    assert row["branch"] in ("main", "master")
    assert row["remote"] == "https://example.com/echam.git"  # login removed
    assert set(row) == {"revision", "branch", "remote"}  # no tags here, so no version
    assert item.properties["software_note"] == RECONSTRUCTED_FOOTNOTE
    assert not any(key.startswith("vcs:") for key in item.properties)


def test_code_in_a_subfolder_of_model_dir_is_found(tmp_path, item):
    setup_dir = tmp_path / "awiesm3-v3.4.2"
    _git_repo(setup_dir / "lpj_guess")
    subprocess.run(["git", "tag", "4.1.7"], cwd=setup_dir / "lpj_guess", check=True)
    em = _experiment_metadata(tmp_path, model_dirs={"lpj_guess": str(setup_dir)})

    add_vcs_item_extension(item, "lpj_guess", em)

    row = item.properties["components"]["lpj_guess"]
    assert row["version"] == "4.1.7"


def test_not_found_is_said_on_the_item(tmp_path, item):
    model_dir = tmp_path / "models" / "rnfmap"
    model_dir.mkdir(parents=True)
    em = _experiment_metadata(tmp_path, model_dirs={"rnfmap": str(model_dir)})

    add_vcs_item_extension(item, "rnfmap", em)

    assert item.properties["components"] == {"rnfmap": {"status": "not found"}}
    assert "software_note" not in item.properties


def test_recorded_snapshot_is_labelled_recorded(tmp_path, item):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    frozen = {
        "echam": {"path": "/snap/echam", "hash": "abc123", "branch_name": "release", "diffs": ""},
        "xios": "Not a git-controlled model!",
    }
    (log_dir / "exp1_vcs_info_initial.yaml").write_text(yaml.dump(frozen))
    em = _experiment_metadata(tmp_path)

    add_vcs_item_extension(item, "echam", em)
    assert item.properties["components"]["echam"] == {"revision": "abc123", "branch": "release"}
    assert "software_note" not in item.properties

    other = item.clone()
    other.properties = {}
    add_vcs_item_extension(other, "xios", em)
    assert other.properties["components"] == {"xios": {"status": "not found"}}


def test_item_without_model_dirs_is_not_found(item, tmp_path):
    em = _experiment_metadata(tmp_path)
    add_vcs_item_extension(item, "echam", em)
    assert item.properties["components"] == {"echam": {"status": "not found"}}


def test_item_for_unknown_component_is_not_found(tmp_path, item):
    model_dir = tmp_path / "models" / "echam"
    _git_repo(model_dir)
    em = _experiment_metadata(tmp_path, model_dirs={"echam": str(model_dir)})

    add_vcs_item_extension(item, "fesom", em)  # not in model_dirs

    assert item.properties["components"] == {"fesom": {"status": "not found"}}


# --- missing-hash/missing-component logging: confirmed live that every real --
# production collection had zero vcs:revision stamping with nothing in the
# logs saying why; these pin that the reason is now surfaced, not silent.


def test_warns_once_per_component_with_no_hash(tmp_path, monkeypatch):
    import esm_catalog.vcs as vcs_mod

    warnings = []
    monkeypatch.setattr(
        vcs_mod.logger, "warning", lambda *a, **k: warnings.append((a, k))
    )
    model_dir = tmp_path / "models" / "xios"
    model_dir.mkdir(parents=True)  # not a git checkout -> no hash
    em = _experiment_metadata(tmp_path, model_dirs={"xios": str(model_dir)})

    _vcs_by_component_once(em)
    _vcs_by_component_once(em)  # cached -- must not warn a second time

    assert len(warnings) == 1
    args, _kwargs = warnings[0]
    assert args[1] == "xios"
    assert "not a git-controlled checkout" in args[3]


def test_warns_when_the_code_is_found_in_a_subfolder(tmp_path, monkeypatch):
    import esm_catalog.vcs as vcs_mod

    warnings = []
    monkeypatch.setattr(
        vcs_mod.logger, "warning", lambda *a, **k: warnings.append((a, k))
    )
    setup_dir = tmp_path / "awiesm3-v3.4.2"
    _git_repo(setup_dir / "lpj_guess")
    em = _experiment_metadata(tmp_path, model_dirs={"lpj_guess": str(setup_dir)})

    _vcs_by_component_once(em)

    assert len(warnings) == 1
    args, _kwargs = warnings[0]
    assert args[1] == "lpj_guess"
    assert args[3] == str(setup_dir / "lpj_guess")


def test_warns_once_per_missing_component(tmp_path, item, monkeypatch):
    import esm_catalog.vcs as vcs_mod

    warnings = []
    monkeypatch.setattr(
        vcs_mod.logger, "warning", lambda *a, **k: warnings.append((a, k))
    )
    model_dir = tmp_path / "models" / "echam"
    _git_repo(model_dir)
    em = _experiment_metadata(tmp_path, model_dirs={"echam": str(model_dir)})

    add_vcs_item_extension(item, "fesom", em)
    add_vcs_item_extension(item, "fesom", em)  # second item, same component

    assert len(warnings) == 1
    args, _kwargs = warnings[0]
    assert args[1] == "fesom"
    assert "not present" in args[0]


# --- remote (sftp) scans: model_dir reconstruction must run on the remote host ---


def test_ssh_host_none_for_local_scan(tmp_path):
    em = _experiment_metadata(tmp_path)
    assert _ssh_host(em) is None


def test_ssh_host_from_sftp_experiment_path():
    em = ExperimentMetadata(
        experiment_id="exp1",
        experiment_path=UPath("sftp://albedo0.dmawi.de/work/exp1"),
    )
    assert _ssh_host(em) == "albedo0.dmawi.de"


def test_reconstruction_runs_git_over_ssh_for_remote_scan(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        out = "true" if argv[:2] == ["ssh", "albedo0.dmawi.de"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    em = ExperimentMetadata(
        experiment_id="exp1",
        experiment_path=UPath("sftp://albedo0.dmawi.de/work/exp1"),
        model_dirs={"echam": "/work/models/echam"},
    )
    software = vcs_by_component(em)

    assert software["echam"]["reconstructed"] is True
    assert all(argv[0] == "ssh" and argv[1] == "albedo0.dmawi.de" for argv in calls)
    # the git invocation itself is one already-quoted string, not split argv
    assert all(len(argv) == 3 for argv in calls)
    assert "git -C /work/models/echam" in calls[0][2]


def test_reconstruction_remote_unreachable_note_distinct_from_non_git(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 255, stdout="", stderr="ssh: connect failed")

    monkeypatch.setattr(subprocess, "run", fake_run)

    em = ExperimentMetadata(
        experiment_id="exp1",
        experiment_path=UPath("sftp://albedo0.dmawi.de/work/exp1"),
        model_dirs={"echam": "/work/models/echam"},
    )
    software = vcs_by_component(em)

    assert software["echam"]["note"] == "unreachable over ssh"


# --------------------------------------------------------------------------- #
# Collection: components table, one footnote.
# --------------------------------------------------------------------------- #


def _collection():
    from pystac import Collection, Extent, SpatialExtent, TemporalExtent

    return Collection(
        id="c",
        description="c",
        extent=Extent(SpatialExtent([[-180, -90, 180, 90]]), TemporalExtent([[None, None]])),
    )


def test_collection_gets_one_components_table_and_one_footnote(tmp_path):
    fesom_dir = tmp_path / "models" / "fesom-2.7"
    _git_repo(fesom_dir)
    subprocess.run(["git", "tag", "2.7.7"], cwd=fesom_dir, check=True)
    subprocess.run(["git", "checkout", "-q", "2.7.7"], cwd=fesom_dir, check=True)
    em = _experiment_metadata(
        tmp_path, components=["fesom", "xios"], model_dirs={"fesom": str(fesom_dir)}
    )
    collection = _collection()

    add_components_table(collection, em)

    table = collection.extra_fields["components"]
    assert list(table) == ["fesom", "xios"]
    assert table["fesom"]["version"] == "2.7.7"
    assert table["xios"] == {"status": "not found"}
    assert collection.extra_fields["software_note"] == RECONSTRUCTED_FOOTNOTE


def test_recorded_versions_need_no_footnote(tmp_path):
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    frozen = {
        "echam": {"path": "/snap/echam", "hash": "abc123", "branch_name": "release"},
        "esm_tools": {"path": "/sw/esm_tools", "hash": "75f86e4", "branch_name": "release"},
    }
    (log_dir / "exp1_vcs_info_initial.yaml").write_text(yaml.dump(frozen))
    em = _experiment_metadata(tmp_path, components=["echam"])
    collection = _collection()

    add_components_table(collection, em)

    assert collection.extra_fields["components"] == {
        "echam": {"revision": "abc123", "branch": "release"},
        "esm_tools": {"revision": "75f86e4", "branch": "release"},
    }
    assert "software_note" not in collection.extra_fields


def test_esm_tools_version_is_its_own_version_number(tmp_path):
    esm_tools_dir = tmp_path / "esm_tools"
    _git_repo(esm_tools_dir)
    init = esm_tools_dir / "src" / "esm_tools" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text('"""ESM-Tools."""\n__version__ = "6.67.0"\n')
    subprocess.run(["git", "add", "."], cwd=esm_tools_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "version"], cwd=esm_tools_dir, check=True)
    subprocess.run(["git", "tag", "before_japan"], cwd=esm_tools_dir, check=True)
    em = _experiment_metadata(tmp_path, model_dirs={"esm_tools": str(esm_tools_dir)})

    assert vcs_by_component(em)["esm_tools"]["software_version"] == "6.67.0"


# --------------------------------------------------------------------------- #
# Version history across scans.
# --------------------------------------------------------------------------- #


def _scan(tmp_path, model_dir, history, end):
    from datetime import datetime

    from esm_catalog.vcs import _SOFTWARE_CACHE, updated_history

    _SOFTWARE_CACHE.clear()
    em = _experiment_metadata(
        tmp_path,
        components=["fesom"],
        model_dirs={"fesom": str(model_dir)},
        software_history=history,
        run_start=datetime(1850, 1, 1),
        run_end=end,
    )
    return em, updated_history(em)


def test_history_extends_while_the_revision_stays(tmp_path):
    from datetime import datetime

    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)
    _, first = _scan(tmp_path, model_dir, {}, datetime(1851, 1, 1))
    _, second = _scan(tmp_path, model_dir, first, datetime(1852, 1, 1))

    assert len(second["fesom"]) == 1
    assert second["fesom"][0]["from"] == "1850-01-01"
    assert second["fesom"][0]["to"] == "1852-01-01"


def test_changed_revision_adds_an_entry_and_shows_in_the_table(tmp_path):
    from datetime import datetime

    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)
    _, first = _scan(tmp_path, model_dir, {}, datetime(1851, 1, 1))
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "fix"], cwd=model_dir, check=True)
    em, second = _scan(tmp_path, model_dir, first, datetime(1852, 1, 1))

    old, new = second["fesom"]
    assert old["to"] == "1851-01-01"
    assert new["from"] == "1851-01-02"  # dates are inclusive: the day after
    assert new["to"] == "1852-01-01"
    assert old["revision"] != new["revision"]

    collection = _collection()
    add_components_table(collection, em)
    row = collection.extra_fields["components"]["fesom"]
    assert row["revision"] == new["revision"]  # latest on top
    assert row["changed"] is True
    assert row["history"] == second["fesom"]


def test_changed_component_shows_its_history_on_the_item(tmp_path, item):
    from datetime import datetime

    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)
    _, first = _scan(tmp_path, model_dir, {}, datetime(1851, 1, 1))
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "fix"], cwd=model_dir, check=True)
    em, second = _scan(tmp_path, model_dir, first, datetime(1852, 1, 1))

    add_vcs_item_extension(item, "fesom", em)

    row = item.properties["components"]["fesom"]
    assert row["revision"] == second["fesom"][-1]["revision"]
    assert row["changed"] is True
    assert row["history"] == second["fesom"]


def test_unchanged_component_has_no_history_in_the_table(tmp_path):
    from datetime import datetime

    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)
    em, _ = _scan(tmp_path, model_dir, {}, datetime(1851, 1, 1))

    collection = _collection()
    add_components_table(collection, em)
    row = collection.extra_fields["components"]["fesom"]
    assert "changed" not in row and "history" not in row


# --------------------------------------------------------------------------- #
# Build fingerprint: what the run actually used.
# --------------------------------------------------------------------------- #


def _build(tmp_path, program=b"v1", library=b"lib1"):
    run_bin = tmp_path / "run_18500101-18500101" / "bin" / "fesom"
    (run_bin / "lib").mkdir(parents=True, exist_ok=True)
    (run_bin / "fesom").write_bytes(program)
    (run_bin / "lib" / "libfesom.so").write_bytes(library)


def _scan_with_build(tmp_path, model_dir, history, end):
    from esm_catalog.vcs import _SOFTWARE_CACHE, updated_history

    _SOFTWARE_CACHE.clear()
    em = _experiment_metadata(
        tmp_path,
        components=["fesom"],
        model_dirs={"fesom": str(model_dir)},
        software_history=history,
        latest_run="run_18500101-18500101",
        run_start=datetime(1850, 1, 1),
        run_end=end,
    )
    return updated_history(em)


def test_build_fingerprint_covers_libraries(tmp_path):
    from esm_catalog.vcs import build_fingerprint

    _build(tmp_path)
    folder = UPath(tmp_path / "run_18500101-18500101" / "bin" / "fesom")
    before = build_fingerprint(folder)
    _build(tmp_path, library=b"lib2")

    assert len(before) == 12
    assert build_fingerprint(folder) != before
    assert build_fingerprint(UPath(tmp_path / "empty")) is None


def test_rebuild_without_new_commit_is_a_change(tmp_path):
    # Case 4/6: same revision, different program.
    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)
    _build(tmp_path)
    first = _scan_with_build(tmp_path, model_dir, {}, datetime(1851, 1, 1))
    _build(tmp_path, program=b"v2")
    second = _scan_with_build(tmp_path, model_dir, first, datetime(1852, 1, 1))

    old, new = second["fesom"]
    assert old["revision"] == new["revision"]
    assert old["build"] != new["build"]


def test_new_commit_without_rebuild_is_not_a_change(tmp_path):
    # Case 5: new revision, same program.
    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)
    _build(tmp_path)
    first = _scan_with_build(tmp_path, model_dir, {}, datetime(1851, 1, 1))
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=model_dir, check=True)
    second = _scan_with_build(tmp_path, model_dir, first, datetime(1852, 1, 1))

    assert len(second["fesom"]) == 1
    assert second["fesom"][0]["to"] == "1852-01-01"


def test_history_from_before_builds_does_not_count_as_a_change(tmp_path):
    model_dir = tmp_path / "models" / "fesom"
    _git_repo(model_dir)
    revision = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=model_dir, capture_output=True, text=True
    ).stdout.strip()
    old_history = {"fesom": [{"revision": revision, "from": "1850-01-01", "to": "1851-01-01"}]}
    _build(tmp_path)

    history = _scan_with_build(tmp_path, model_dir, old_history, datetime(1852, 1, 1))

    assert len(history["fesom"]) == 1
    assert history["fesom"][0]["build"]  # filled in


def test_published_history_keeps_fingerprints_local(tmp_path):
    from esm_catalog.vcs import _published_history

    entries = [
        {"version": "2.7.7", "revision": "abc", "build": "b1", "from": "1850-01-01", "to": "1851-01-01"},
        {"version": "2.7.7", "revision": "abc", "build": "b2", "from": "1851-01-02", "to": "1852-01-01"},
        {"version": "2.7.8", "revision": "def", "build": "b3", "from": "1852-01-02", "to": "1853-01-01"},
    ]

    shown = _published_history(entries)

    assert all("build" not in entry for entry in shown)
    assert [entry.get("rebuilt") for entry in shown] == [None, True, None]
    assert entries[0]["build"] == "b1"  # the local history is unchanged
