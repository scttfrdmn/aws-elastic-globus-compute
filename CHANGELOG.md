# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (0.x until further notice).

## [Unreleased]

### Added
- **Project scaffold:** the `aegc` package and CLI stub, CI (ruff, mypy, pytest), the documentation
  site, and project tracking (#1).
- **Protocol v1:** JSON task and result messages for SQS. Bodies over 700 KB go to S3 with sha256
  verification. The worker runs only allow-listed calls, and walltime is capped below SQS's 12-hour claim
  limit (#6).
