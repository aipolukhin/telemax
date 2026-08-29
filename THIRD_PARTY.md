# Third-party projects

Telemax depends on the following upstream projects at runtime. Version
constraints in `pyproject.toml` are authoritative; this document records the
project homes, roles, and upstream license identifiers for attribution.

No source code or media assets from these projects are vendored in the Telemax
repository. Installing Telemax resolves them as separate Python distributions,
and each project remains governed by its own license.

| Distribution | Upstream project | Used for | License |
|---|---|---|---|
| `maxapi-python` | [MaxApiTeam/PyMax](https://github.com/MaxApiTeam/PyMax) | MAX client, session, events and domain API | MIT |
| `aiogram` | [aiogram/aiogram](https://github.com/aiogram/aiogram) | Telegram Bot API | MIT |
| `telethon` | [Lonami/Telethon](https://codeberg.org/Lonami/Telethon) | Telegram owner session and MTProto | MIT |
| `aiohttp` | [aio-libs/aiohttp](https://github.com/aio-libs/aiohttp) | Async HTTP | Apache-2.0 AND MIT |
| `aiosqlite` | [omnilib/aiosqlite](https://github.com/omnilib/aiosqlite) | Async SQLite access | MIT |
| `pillow` | [python-pillow/Pillow](https://github.com/python-pillow/Pillow) | Image conversion | MIT-CMU |
| `av` | [PyAV-Org/PyAV](https://github.com/PyAV-Org/PyAV) | Audio and video decoding | BSD-3-Clause |
| `rlottie-python` | [laggykiller/rlottie-python](https://github.com/laggykiller/rlottie-python) | Telegram animated-sticker rendering | LGPL-2.1 |
| `pydantic` | [pydantic/pydantic](https://github.com/pydantic/pydantic) | Configuration models and validation | MIT |
| `pyyaml` | [yaml/pyyaml](https://github.com/yaml/pyyaml) | YAML configuration | MIT |
| `python-dotenv` | [theskumar/python-dotenv](https://github.com/theskumar/python-dotenv) | Local environment files | BSD-3-Clause |
| `qrcode` | [lincolnloop/python-qrcode](https://github.com/lincolnloop/python-qrcode) | Terminal login QR codes | BSD |
| `questionary` | [tmbo/questionary](https://github.com/tmbo/questionary) | Interactive terminal setup | MIT |
| `rich` | [Textualize/rich](https://github.com/Textualize/rich) | Terminal presentation | MIT |
| `tzlocal` | [regebro/tzlocal](https://github.com/regebro/tzlocal) | Host timezone detection | MIT |

The dependency list is generated independently by package installers and may
also include transitive projects. Consult the installed distributions for their
complete license texts and notices.
