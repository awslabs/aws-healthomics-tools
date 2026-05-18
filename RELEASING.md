# Releasing

This project uses GitHub Actions with OIDC trusted publishing to release to PyPI. No API tokens or credentials are needed — authentication is handled automatically via the GitHub ↔ PyPI trust relationship.

## Prerequisites

- Write access to this repository
- `poetry` and `gh` CLI installed locally

## Test Release (TestPyPI)

Use this to validate the release pipeline before publishing to production.

```bash
# Bump to a release candidate version
poetry version 0.13.2rc1

# Commit and push
git commit -am "release: v0.13.2rc1"
git push origin main

# Create a pre-release (triggers TestPyPI publish)
gh release create v0.13.2rc1 --prerelease --generate-notes --title "v0.13.2rc1"
```

Verify at: https://test.pypi.org/project/aws-healthomics-tools/

## Production Release (PyPI)

```bash
# Bump to the final version
poetry version 0.13.2

# Commit and push
git commit -am "release: v0.13.2"
git push origin main

# Create the release (triggers PyPI publish)
gh release create v0.13.2 --generate-notes
```

Verify at: https://pypi.org/project/aws-healthomics-tools/

## Version Bumping

Use `poetry version` to bump:

```bash
poetry version patch   # 0.13.1 → 0.13.2
poetry version minor   # 0.13.1 → 0.14.0
poetry version major   # 0.13.1 → 1.0.0
```

## What Happens Automatically

1. The GitHub Release event triggers `.github/workflows/release.yml`.
2. CI runs the full lint, test, type-check, and dependency audit suite.
3. The workflow verifies `pyproject.toml` version matches the git tag.
4. The package is built and published:
   - **Pre-release** (tag contains `rc`, `alpha`, `beta`) → TestPyPI
   - **Full release** → PyPI

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "not allowed to deploy due to environment protection rules" | Ensure the GitHub environment (`pypi` or `testpypi`) allows tag deployments (Settings → Environments → Deployment branches and tags → add `v*` pattern) |
| "version mismatch" error | Ensure `pyproject.toml` version matches the tag (without the `v` prefix) |
| CI fails | Fix the issue, delete the release and tag, then recreate after pushing the fix |

## Deleting a Failed Release

```bash
gh release delete v0.13.2 --yes
git push --delete origin v0.13.2
```
