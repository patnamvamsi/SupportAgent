# Releasing

How a version gets from `main` to a published image. [06 §6](../tech-spec/06-versioning-and-release.md#6-release-procedure) is the policy; this is the procedure. Each step is automated or checked, so a release that skips one fails instead of shipping.

## What guards a release

| Stage | What runs | Where |
|---|---|---|
| Every pull request | lint, strict types, architecture contracts, unit and property tests, contract tests, invariants and integration on SQLite and Postgres, e2e, the eval replays when prompts, packs or model code change, pip-audit, bandit, gitleaks, shellcheck, actionlint, the image smoke test, the upgrade test from the previous release on both engines, and the check that released migrations are unchanged | `ci.yml` |
| Every night | the storm soak on both engines | `nightly.yml` |
| The tag | the tag matches `pyproject.toml`, the release notes name the head migration, and every migration is recorded as released; then all of CI again with every eval, and the soak | `release.yml` (verify, ci, soak) |
| The image | built for amd64 and arm64, with an SBOM and build provenance, signed keylessly by the workflow | `release.yml` (build) |
| Before publishing | the signature verifies, and the pulled image runs on its own (version, offline eval, migrate, `/healthz`, `/readyz`) | `release.yml` (smoke) |
| Publishing | a maintainer approves the `release` environment; the tested digest is tagged, never rebuilt | `release.yml` (publish) |

## One-time setup (maintainer)

1. **Settings → Environments → New environment `release`:** add yourself as a required reviewer, and restrict deployments to tags matching `v*`.
2. **Settings → Rules:** add a ruleset for `main`, with pull requests required, the `ci` checks required, and no force pushes. Add a tag ruleset for `v*` so that only maintainers create release tags.
3. After the first release, make the package public: **Packages → supportagent → Package settings → Change visibility**.

## Cutting a release

1. **Pick the version** (06 §1): `0.MINOR.0` for a roadmap release, `0.MINOR.PATCH` for fixes, and `X.Y.ZrcN` for a candidate.
2. **Write `docs/releases/X.Y.Z.md`:** what's delivered, the head migration revision as `` `00NN` ``, config changes, prompt, model and taxonomy versions, calibration grade, and known limitations (06 §6 step 3). A candidate's notes may still say draft; a final release's may not.
3. **Bump `version` in `pyproject.toml`**, run `uv lock`, and run `python3 scripts/release_check.py --record-migrations`. That marks the migrations as released, and from then on CI fails if one is edited. Open a PR with all three. CI must pass, and `python3 scripts/release_check.py vX.Y.Z` must too.
4. **Merge, then tag the merge commit:**

   ```bash
   git checkout main && git pull
   git tag -s vX.Y.Z -m "SupportAgent X.Y.Z"    # -s signs the tag
   git push origin vX.Y.Z
   ```

5. **Watch the `release` workflow.** When build and smoke pass, it waits for you. Approve the `release` environment to publish.
6. **Release candidates go to staging first.** Deploy `ghcr.io/<owner>/supportagent:X.Y.ZrcN` there and run the exit demo (ROADMAP §3). Record the result in the notes. Then open a PR that sets the version to `X.Y.Z` and takes "draft" out of the notes, with no other change, and tag its merge commit. The final image is built from that commit and goes through every check again.

## Verifying a published image

```bash
cosign verify ghcr.io/<owner>/supportagent:X.Y.Z \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity https://github.com/<owner>/SupportAgent/.github/workflows/release.yml@refs/tags/vX.Y.Z
gh attestation verify oci://ghcr.io/<owner>/supportagent:X.Y.Z --owner <owner>
```

## When a release fails

- **verify:** fix the version, the notes or the head revision in a PR. Delete the tag (`git push origin :vX.Y.Z`), then tag again.
- **ci, soak or smoke:** nothing was published. Fix forward on `main`, and release the next patch or candidate. Don't move a pushed tag.
- **After publishing:** release a fixed patch. Released migrations are immutable (06 §2), so a fix is a new migration.
