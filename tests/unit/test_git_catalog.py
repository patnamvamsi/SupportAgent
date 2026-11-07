"""ADR-0017 §2: a plugin catalogue from git, at a pinned commit, fetched read-only.

The repository is a real one on disk, reached over ``file://``, so git's own
fetch and checkout run, with no network.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from supportagent.adapters.files import git_catalog
from supportagent.adapters.files.plugin_loader import load_registry, write_index
from supportagent.bootstrap import PLUGINS_DIR, catalog_dirs, fetch_catalogs, load_catalogs
from supportagent.config.schema import GitCatalog, PluginsConfig
from supportagent.domain.errors import ConfigError
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit

_OFFERED = ("github", "grafana")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 — the test's own fixed git argv
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[str, str]:
    """A repository with a catalogue under ``catalog/``; its URL and commit."""
    root = tmp_path / "upstream"
    catalog = root / "catalog"
    catalog.mkdir(parents=True)
    for plugin in _OFFERED:
        shutil.copytree(PLUGINS_DIR / plugin, catalog / plugin)
    write_index(catalog)
    _git(root, "init", "--quiet")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "catalogue")
    return root.as_uri(), _git(root, "rev-parse", "HEAD")


def _config(cache: Path, *catalogs: object) -> object:
    config = _shadow_config()
    plugins = PluginsConfig.model_validate({"catalogs": list(catalogs), "cache_dir": str(cache)})
    return config.model_copy(update={"plugins": plugins})


def test_a_catalogue_is_fetched_at_its_commit_and_then_reused_offline(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, commit = repo
    cache = tmp_path / "cache"
    checkout = git_catalog.fetch(cache, url, commit)
    assert sorted(p.manifest.id for p in load_registry(checkout / "catalog")) == sorted(_OFFERED)
    assert not (checkout / ".git").exists()  # only data is left
    assert [p.name for p in checkout.parent.iterdir()] == [commit]  # no leftovers

    def offline(args: Sequence[str]) -> str:
        raise AssertionError(f"the network was used: git {args}")  # noqa: TRY003

    assert git_catalog.fetch(cache, url, commit, run=offline) == checkout


def test_config_resolves_a_git_catalogue_beside_a_directory(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, commit = repo
    cache = tmp_path / "cache"
    local = tmp_path / "local-catalog"
    shutil.copytree(PLUGINS_DIR / "github", local / "github")
    write_index(local)
    config = _config(cache, str(local), {"git": url, "ref": commit, "path": "catalog"})

    # doctor reads and never fetches: not fetched yet says so
    label, directory, error = catalog_dirs(config, fetch=False)[1]  # type: ignore[arg-type]
    assert directory is None and "not fetched yet" in error and commit[:12] in label

    assert [(n, e) for _, n, e in fetch_catalogs(config)] == [(1, ""), (len(_OFFERED), "")]  # type: ignore[arg-type]
    offered = sorted(p.manifest.id for p in load_catalogs(config))  # type: ignore[arg-type]
    assert offered == sorted(["github", *_OFFERED])
    assert catalog_dirs(config, fetch=False)[1][1] is not None  # type: ignore[arg-type]


def test_a_commit_the_repository_does_not_have_is_refused_and_leaves_nothing(
    tmp_path: Path, repo: tuple[str, str]
) -> None:
    url, _ = repo
    cache = tmp_path / "cache"
    with pytest.raises(ConfigError, match="git fetch"):
        git_catalog.fetch(cache, url, "0" * 40)
    assert not any(cache.rglob("*.fetching-*"))
    assert git_catalog.cached(cache, url, "0" * 40) is None
    config = _config(cache, {"git": url, "ref": "0" * 40})
    [(_, offered, error)] = fetch_catalogs(config)  # type: ignore[arg-type]
    assert offered is None and "git fetch" in error
    with pytest.raises(ConfigError, match="catalogue"):
        load_catalogs(config)  # type: ignore[arg-type]


def test_a_fetch_that_lands_on_another_commit_is_refused(tmp_path: Path) -> None:
    pinned = "a" * 40

    def lying(args: Sequence[str]) -> str:
        if "init" in args:
            Path(args[-1]).mkdir(parents=True)
            (Path(args[-1]) / ".git").mkdir()
        return "b" * 40 if "rev-parse" in args else ""

    with pytest.raises(ConfigError, match="not the pinned"):
        git_catalog.fetch(tmp_path, "https://example.test/c.git", pinned, run=lying)
    assert git_catalog.cached(tmp_path, "https://example.test/c.git", pinned) is None


@pytest.mark.parametrize(
    ("url", "ref", "why"),
    [
        ("https://example.test/c.git", "main", "full commit id"),
        ("https://example.test/c.git", "v1.2.0", "full commit id"),
        ("https://example.test/c.git", "a" * 39, "full commit id"),
        ("ext::sh -c touch% /tmp/pwned", "a" * 40, "only https"),
        ("http://example.test/c.git", "a" * 40, "only https"),
        ("/srv/catalog", "a" * 40, "only https"),
    ],
)
def test_only_a_commit_over_an_allowed_transport_is_accepted(url: str, ref: str, why: str) -> None:
    with pytest.raises(ConfigError, match=why):
        git_catalog.check_source(url, ref)


def test_allowed_sources_pass() -> None:
    for url in (
        "https://github.com/acme/catalog.git",
        "ssh://git@github.com/acme/catalog.git",
        "git@github.com:acme/catalog.git",
        "file:///srv/catalog",
    ):
        git_catalog.check_source(url, "c" * 40)
    git_catalog.check_source("https://example.test/c.git", "d" * 64)  # SHA-256 repositories


def test_the_config_refuses_a_moving_ref_or_a_path_out_of_the_repository() -> None:
    with pytest.raises(ValidationError):
        GitCatalog(git="https://example.test/c.git", ref="main")
    with pytest.raises(ValidationError):
        GitCatalog(git="https://example.test/c.git", ref="a" * 40, path="../etc")
    assert GitCatalog(git="https://x/c.git", ref="a" * 40, path="cat").label == (
        "https://x/c.git@aaaaaaaaaaaa:cat"
    )
