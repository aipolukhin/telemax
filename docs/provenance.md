# Telemax provenance and competitor review

This page records where Telemax's dependencies and architectural ideas came
from. It is a review log, not a claim that source code from every listed project
is part of Telemax.

## Runtime foundations

| Project | Role in Telemax | License |
|---|---|---|
| [PyMax](https://github.com/MaxApiTeam/PyMax) / `maxapi-python==2.3.1` | MAX session, events and account API behind the Telemax adapter | MIT |
| [aiogram](https://github.com/aiogram/aiogram) | Telegram Bot API and Guardian/contact bots | MIT |
| [Telethon](https://codeberg.org/Lonami/Telethon) `1.44.0` | QR-authenticated Telegram owner session and BotFather operations | MIT |
| [aiosqlite](https://github.com/omnilib/aiosqlite) | durable inbox/outbox and bridge state | MIT |

The complete distribution list is in [`THIRD_PARTY.md`](../THIRD_PARTY.md).

## Direct MAX ↔ Telegram peers reviewed on 2026-08-30

| Project | Reviewed commit | License/status | What Telemax learned |
|---|---|---|---|
| [maxgram](https://github.com/eiler2005/maxgram) | `98d8025262fad4235c1d303b49e7142d182953e5` | MIT | supervisor heartbeat, account-migration registry, readonly bridge mode, quiet periodic status |
| [Max2TG](https://github.com/MinRussianDeveloper/Max2TG) | `15edfd404b4cb106ccea4abff11b3c9293be36bf` | MIT via `LICENSE-PROTOCOL` | MAX process isolation and controlled history replay |
| [BEARlogin/max-telegram-bridge-bot](https://github.com/BEARlogin/max-telegram-bridge-bot) | `0ee0263f24e68ac5a8cade7e6629f4b7ec6e54b0` | CC BY-NC 4.0 | local Bot API option, circuit-breaker UX, addon boundary; concepts only |
| [telegram-to-max-reliable-pipeline](https://github.com/NataAntro/telegram-to-max-reliable-pipeline) | `3cf82099a5643f2bb052e9f5738aaf4c840eaf2c` | no license found | source fingerprints, resource cooldown and checkpoint planning; concepts only |
| [MariaShsv/tg-max-bridge](https://github.com/MariaShsv/tg-max-bridge) | `a614765adc13b2670cdce6756a48b0766c4124c2` | MIT | explicit media degradation and reply fallback UX |

No source from the CC BY-NC or unlicensed projects was copied. Their entries are
behavioral references only.

## Adopted now

Telemax independently implemented one operational pattern from this pass:

```text
Guardian-first outer runtime
  → writes local event-loop heartbeat
  → service/container healthcheck reads heartbeat freshness
  → external MAX or Telegram outage remains degraded, not dead
```

The implementation is [`bridge/service/heartbeat.py`](../bridge/service/heartbeat.py).
It keeps the existing Telemax architecture: one process, one MAX session, a
Guardian control plane that stays alive while the bridge worker is onboarding or
restarting, and durable SQLite delivery state.

## Already stronger or already present

Telemax already had the useful reliability properties found across these peers:

- durable intake before acknowledgement;
- leased outbox with bounded retry, TTL and explicit `failed`/`ambiguous` states;
- keyed ordering and durable recovery after restart;
- stable message mappings for replies, edits and deletes;
- visible placeholders instead of silently dropped media;
- one Guardian that survives worker restarts.

Replacing these with another project's simpler queue would be a regression.

## Planned candidates

1. Privacy-safe MAX account migration registry: bridge/contact metadata and
   snapshot freshness, without message bodies or a full address book.
2. Per-bridge readonly mode and a quiet periodic owner summary.
3. Optional local Telegram Bot API adapter for files above hosted Bot API limits.
4. Source fingerprint anomaly detection: one source ID resolving to different
   normalized content should require owner attention.
5. A user-visible circuit breaker only if live failure data shows that current
   queue/backoff status is insufficient.

## Deliberately deferred

- Telegram topics conflict with Telemax's one-contact-one-bot UX.
- PostgreSQL/FastAPI/S3 are disproportionate for a personal single-host bridge.
- Splitting MAX into multiple processes is justified only after a native crash,
  event-loop corruption or memory leak is reproduced.
- Ansible remains deferred. Docker is available only as an option; the default
  install is a user systemd service in the owner's home directory.
