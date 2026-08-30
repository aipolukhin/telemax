# Changelog

All notable changes to Telemax will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- bot-first setup: one Telegram QR creates the canonical owner session and
  Guardian, then MAX login continues in Telegram;
- no-sudo host installer with a pinned `uv`/Python environment;
- optional hardened Docker Compose deployment with persistent host state;
- supervisor heartbeat and local healthcheck that distinguish liveness from an
  external MAX/Telegram outage.

### Changed

- normal setup no longer asks for a Telegram phone/code, owner ID, manual
  Guardian token, or a second owner session;
- fresh Guardian names are derived from Telegram owner identity alone so the
  bot can exist before MAX is connected; existing names remain unchanged;
- competitor provenance refreshed with reviewed SHAs and explicit adoption/
  deferral decisions.

## [0.1.0] - 2026-08-29

### Added

- initial public release;
- durable two-way text and media delivery;
- one Telegram bot per MAX contact;
- interactive setup, validation, systemd installation, and guardian controls;
- presence, read receipts, reactions, edits, replies, forwards, and history import.

[Unreleased]: https://github.com/aipolukhin/telemax/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/aipolukhin/telemax/releases/tag/v0.1.0
