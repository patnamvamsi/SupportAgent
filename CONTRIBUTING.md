# Contributing

Thanks for helping. Bug reports, fixes, packs (incident knowledge for a stack), tool adapters and docs are all welcome.

## Before you start

- **A bug:** open an issue with the bug template. For anything security-related, follow [SECURITY.md](SECURITY.md) instead.
- **A behaviour or design change:** open an issue first. `docs/spec/` is frozen and changes through an ADR (`docs/adr/`), so a design change starts as an ADR proposal.
- **Incident knowledge for your stack:** usually a pack or a plugin, not core code. The `extend-supportagent` skill (`.claude/skills/extend-supportagent/SKILL.md`) explains each kind of extension.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and Docker (Postgres tests and the image build use it).

```bash
uv sync
make check                     # lint, format, strict types, architecture contracts
uv run pytest -m unit          # fast tests, no network
make ci-local                  # every CI job, with a pass/fail table
```

## What a pull request needs

CI runs these, and all of them must pass:

- `ruff check` and `ruff format --check`; `mypy --strict src tests`; `lint-imports` (the layering contracts);
- unit and property tests (coverage floors in `docs/tech-spec/04-test-strategy.md`), contract tests;
- invariant and integration tests on **SQLite and Postgres**, end-to-end tests;
- the eval replays (smoke, incident lab, faithfulness, tracing, onboarding) when prompts, taxonomy, packs or model code change;
- security: `pip-audit`, `bandit`, `gitleaks`, `shellcheck`;
- CodeQL code scanning (public repository).

Dependabot opens the weekly dependency, action and base-image updates, and they go through the same checks.

Also:

- Tests come with the change. A fix includes the test that would have caught it.
- A prompt, pack, tool or model change includes a lab report (see the PR template).
- A new dependency needs an ADR (`pyproject.toml` lists the allowlist rule).
- Never commit secrets, real ticket data or personal data. Fixtures use fake, low-entropy values in the paths `.gitleaks.toml` allows.
- Keep commits focused, with messages that say why.

## License

SupportAgent is licensed under the [Apache License 2.0](LICENSE). By contributing, you agree that your contribution is licensed under it too (section 5 of the license).
