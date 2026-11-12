"""06 §6: ``scripts/release_check.py`` decides whether a tag may be released."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release_check.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_check", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


rc = _module()


def _repo(
    tmp_path: Path,
    version: str,
    notes: str | None,
    heads: tuple[str, ...] = ("0002",),
    *,
    record: bool = True,
) -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_base.py").write_text('revision = "0001"\ndown_revision = None\n')
    for head in heads:
        (versions / f"{head}_x.py").write_text(f'revision = "{head}"\ndown_revision = "0001"\n')
    if notes is not None:
        (tmp_path / "docs" / "releases").mkdir(parents=True)
        base = version.split("rc", maxsplit=1)[0]
        (tmp_path / "docs" / "releases" / f"{base}.md").write_text(notes)
    if record:
        rc.record_migrations(tmp_path)
    return tmp_path


FINAL = "# SupportAgent 0.1.0\n\n## Migrations\n\nHead revision **`0002`**.\n"


def test_a_final_release_with_notes_naming_the_head(tmp_path: Path) -> None:
    release = rc.check("v0.1.0", _repo(tmp_path, "0.1.0", FINAL))
    assert (release.version, release.minor, release.prerelease) == ("0.1.0", "0.1", False)
    assert release.head == "0002"


def test_a_release_candidate_may_still_be_a_draft(tmp_path: Path) -> None:
    notes = "# SupportAgent 0.1.0 (draft)\n\nHead revision **`0002`**.\n"
    release = rc.check("v0.1.0rc1", _repo(tmp_path, "0.1.0rc1", notes))
    assert release.prerelease
    assert str(release.notes) == "docs/releases/0.1.0.md"


@pytest.mark.parametrize(
    ("tag", "version", "notes", "heads", "error"),
    [
        ("v0.1.1", "0.1.0", FINAL, ("0002",), "doesn't match pyproject"),
        ("v0.1", "0.1", FINAL, ("0002",), "must be X.Y.Z"),
        ("v0.1.0", "0.1.0", None, ("0002",), "are missing"),
        ("v0.1.0", "0.1.0", "# 0.1.0\n\nHead `0001`.\n", ("0002",), "head revision `0002`"),
        ("v0.1.0", "0.1.0", "> Status: draft\n\n`0002`\n", ("0002",), "still marked draft"),
        ("v0.1.0", "0.1.0", FINAL, ("0002", "0003"), "exactly one head"),
    ],
)
def test_refused(  # noqa: PLR0913, PLR0917 — one parameter per way a release goes wrong
    tmp_path: Path, tag: str, version: str, notes: str | None, heads: tuple[str, ...], error: str
) -> None:
    with pytest.raises(rc.NotReleasableError, match=error):
        rc.check(tag, _repo(tmp_path, version, notes, heads))


def test_writes_the_workflow_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, "0.2.0rc2", "notes `0002`\n")
    out = tmp_path / "out"
    monkeypatch.setattr(rc.check, "__defaults__", (repo,))
    assert rc.main(["v0.2.0rc2", "--github-output", str(out)]) == 0
    assert "version=0.2.0rc2\nminor=0.2\nprerelease=true\n" in out.read_text()
    assert rc.main(["v9.9.9"]) == 1


def test_prints_the_head_revision(capsys: pytest.CaptureFixture[str]) -> None:
    assert rc.main(["--head"]) == 0
    assert capsys.readouterr().out.strip() == rc.head_revision(rc.ROOT)


def test_every_migration_must_be_recorded_as_released(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "0.1.0", FINAL, record=False)
    with pytest.raises(rc.NotReleasableError, match="not recorded as released: versions/0001"):
        rc.check("v0.1.0", repo)
    assert rc.record_migrations(repo) == ["versions/0001_base.py", "versions/0002_x.py"]
    assert rc.record_migrations(repo) == []  # idempotent
    assert rc.check("v0.1.0", repo).head == "0002"


def test_released_migrations_are_unchanged() -> None:
    """06 §2: a migration in a release is immutable; fix forward with a new one."""
    now = rc.migration_hashes(rc.ROOT)
    for name, digest in rc.recorded(rc.ROOT).items():
        assert name in now, f"released migration {name} was removed"
        assert now[name] == digest, f"released migration {name} was edited"
