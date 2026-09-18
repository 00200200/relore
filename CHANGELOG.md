# Changelog

Notable changes to `relore`, using [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow the milestone/patch policy in [AGENTS.md](AGENTS.md#one-version-both-ends).
The client and daemon must run the same version.

## [Unreleased]

### Added

- PyPI release workflow: release branches run checks and validate distributions;
  version tags publish through GitHub OIDC trusted publishing.
- Package metadata, `make build-release`, and a release guide.

### Improved

- Moved the README quick start into the CLI guide; the README links to deployment
  and usage instructions.

Earlier versions were distributed from Git and deployed as container images. This
changelog starts with the PyPI release setup; no historical PyPI releases are implied.
