# Security policy

## Supported version

Security fixes are applied to the current `main` branch. Until the first stable
release, older snapshots are not supported separately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Do not open a
public issue for a vulnerability and do not include credentials, session files,
real message content, phone numbers, account identifiers, or private logs in a
report.

Include the affected revision, impact, minimal reproduction steps, and a
sanitised example. You should receive an acknowledgement within seven days.

## Secret exposure

If a Telegram bot token, API hash, MAX session, Telegram user session, or other
credential was exposed, revoke or rotate it first. Removing it from a commit is
not sufficient because Git objects and forks may retain the old value.
