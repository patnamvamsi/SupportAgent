"""A plugin catalogue from git: a URL and a pinned commit, fetched read-only (ADR-0017 §2).

A catalogue is a supply-chain boundary, so the ref must be a full commit id:
a branch or a tag can move under the pin, a commit can't. The commit is
fetched shallowly into ``<cache>/<url digest>/<commit>`` and checked against
the pin. Its ``.git`` is then dropped, so the checkout is only data. Once a
commit is fetched it's reused without the network.

What git may do here:

- **Transports:** https, ssh and file only. Git's ``ext::`` and the rest run
  commands, so they're refused, both by the URL check and by
  ``protocol.allow``.
- **No hooks, submodules or symlinks:** a catalogue is data (ADR-0016). A
  symlink could point the loader outside the checkout.
- **No prompt:** a fetch that needs credentials fails instead of waiting on a
  terminal (``credential.interactive``, ssh ``BatchMode``). The usual ssh keys
  and credential helpers still apply.

The catalogue's own ``index.yaml`` pins each plugin's sha256 as before, and
:func:`~supportagent.adapters.files.plugin_loader.load_registry` checks them.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess  # nosec B404 — git, with a fixed argument list and no shell
from collections.abc import Callable, Sequence
from pathlib import Path
from secrets import token_hex

from supportagent.domain.errors import ConfigError

#: A full commit id: SHA-1 (40 hex) or SHA-256 (64 hex).
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SCHEMES = ("https://", "ssh://", "file://")
_SCP = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^:]")  # git@host:org/repo.git
_VERIFIED = ".supportagent-commit"
_GIT_CONFIG = (
    "protocol.allow=never",
    "protocol.https.allow=always",
    "protocol.ssh.allow=always",
    "protocol.file.allow=always",
    "core.hooksPath=/dev/null",
    "core.symlinks=false",
    "submodule.recurse=false",
    "credential.interactive=never",
    "core.sshCommand=ssh -o BatchMode=yes",
    "advice.detachedHead=false",
)

Runner = Callable[[Sequence[str]], str]


def check_source(url: str, ref: str) -> None:
    """Raises ``ConfigError`` unless ``url`` is an allowed transport and ``ref`` a commit."""
    if not (url.startswith(_SCHEMES) or _SCP.match(url)):
        raise ConfigError(  # noqa: TRY003
            f"git catalogue {url!r}: only https://, ssh://, git@host:path and file:// URLs"
        )
    if not COMMIT.match(ref):
        raise ConfigError(  # noqa: TRY003
            f"git catalogue {url}: ref {ref!r} must be a full commit id (a branch or tag can move)"
        )


def checkout_dir(cache: Path, url: str, ref: str) -> Path:
    return cache / hashlib.sha256(url.encode()).hexdigest()[:16] / ref


def cached(cache: Path, url: str, ref: str) -> Path | None:
    """The verified checkout of ``url`` at ``ref``, if it was already fetched."""
    check_source(url, ref)
    dest = checkout_dir(cache, url, ref)
    marker = dest / _VERIFIED
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == ref:
        return dest
    return None


def _git(args: Sequence[str]) -> str:
    git = shutil.which("git")
    if git is None:
        raise ConfigError("git catalogue: git is not installed")  # noqa: TRY003
    config = [part for item in _GIT_CONFIG for part in ("-c", item)]
    verb = args[2] if args[0] == "-C" else args[0]
    try:
        done = subprocess.run(  # noqa: S603  # nosec B603 — a fixed git argv, no shell
            [git, *config, *args],
            capture_output=True,
            text=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigError(f"git {verb}: timed out") from exc  # noqa: TRY003
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["failed"]
        raise ConfigError(f"git {verb}: {detail[0][:200]}")  # noqa: TRY003
    return done.stdout


def fetch(cache: Path, url: str, ref: str, *, run: Runner = _git) -> Path:
    """The checkout of ``url`` at ``ref``: from the cache, or fetched and verified now."""
    found = cached(cache, url, ref)
    if found is not None:
        return found
    dest = checkout_dir(cache, url, ref)
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.with_name(f"{ref}.fetching-{token_hex(4)}")
    try:
        run(["init", "--quiet", str(work)])
        run(["-C", str(work), "fetch", "--quiet", "--depth", "1", "--no-tags", url, ref])
        run(["-C", str(work), "checkout", "--quiet", "FETCH_HEAD"])
        head = run(["-C", str(work), "rev-parse", "HEAD"]).strip()
        if head != ref:
            raise ConfigError(  # noqa: TRY003
                f"git catalogue {url}: fetched {head or 'nothing'}, not the pinned {ref}"
            )
        shutil.rmtree(work / ".git")
        (work / _VERIFIED).write_text(ref + "\n", encoding="utf-8")
        if dest.exists():
            shutil.rmtree(dest)  # an unverified leftover
        work.rename(dest)
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
    return dest


__all__ = ["COMMIT", "cached", "check_source", "checkout_dir", "fetch"]
