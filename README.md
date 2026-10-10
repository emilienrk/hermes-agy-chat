# hermes-agy-chat

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin to talk to
[Antigravity](https://antigravity.google/) (`agy`, Google's agent CLI) from Telegram, as an
alternative to Hermes' own LLM. Messages routed to agy are taken before Hermes' dispatch:
they cost no Hermes LLM tokens, only your Antigravity subscription.

agy is not a bare CLI behind a bot: before every turn the plugin hands it what Hermes knows —
Hermes' personality, memory and recent Telegram thread, its skills, its MCP servers — and a
bundled MCP bridge gives agy a few Hermes tools (shared memory, session search, skill writing,
cron jobs, text-to-speech, Telegram messages). Each of these can be turned off.

It also brings what makes an agent more than a chat: scheduled tasks that agy runs instead of
Hermes' paid LLM, voice replies, and handing a conversation over from Hermes to agy and back.

## What reaches agy

Only from the one Telegram chat you configure:

| You send | Goes to |
|---|---|
| a reply to one of agy's messages | agy |
| anything, while agy mode is on (`/agy_on`) | agy, except `/` commands and messages starting with "Hermes" |
| `/agy <message>` | agy |
| anything else | Hermes, as usual |

Photos, files and voice notes ride along. Voice notes go to agy as audio (default) or are
transcribed locally by Hermes' Whisper first (`/agy_voice local`). Messages sent while agy works
are queued and handed over together.

## Commands

| Command | |
|---|---|
| `/agy [message]` | Status card, or a message for agy |
| `/agy_on`, `/agy_off` | agy mode on (pinned banner) / back to Hermes |
| `/agy_new [message]` | New agy conversation |
| `/agy_stop` | Stop the running turn and drop the queue (`/stop` too, in agy mode) |
| `/agy_model [n or name]` | Pick agy's model |
| `/agy_verbose [on\|off]` | Live status of agy's actions, or just its answer |
| `/agy_voice [direct\|local]` | Voice notes as audio for agy, or transcribed locally |
| `/agy_voice reply [off\|mirror\|always]` | agy's answer as a voice note too (mirror: when you sent one) |
| `/agy_conv [n]` | Resume a past conversation |
| `/agy_retry` | Send your last message again |
| `/agy_handoff [message]` | Hand the Hermes conversation over to agy (agy mode on, seeded with the thread) |
| `/agy_back [message]` | Back to Hermes, which gets a recap of the agy conversation |
| `/agy_task` | List agy's scheduled tasks; `add <when> \| <prompt>`, `rm <n>`, `run <n>` |
| `/agy_usage` | Turns, tokens and time: today, 7 and 30 days, by model |
| `/agy_doctor` | Check every link of the chain |

## Scheduled tasks

`/agy_task add every day at 8am | summarize my unread mails` schedules a prompt that agy runs
unattended; its answer arrives in Telegram. agy can also schedule its own tasks with the
`agy_task` tool ("every Monday, check …").

Hermes keeps the schedule: each task is a Hermes cron job in `no_agent` mode whose script, generated
under `<HERMES_HOME>/scripts/agy-tasks/`, runs agy. So it costs no Hermes LLM tokens, it shows up in
`hermes cron list`, and a failed run is reported like any cron failure. `when` takes Hermes' schedule
forms: `every day at 8am`, `weekdays at 7am`, `0 7 * * 1-5`, `every 6h`, `in 2h` (once).

## Requirements

- Hermes Agent ≥ 0.21.6 running its gateway with the Telegram platform.
- The [Antigravity CLI](https://antigravity.google/) installed for the user running the gateway, and
  logged in once (run `agy` interactively on the machine).

## Install

```bash
hermes plugins install emilienrk/hermes-agy-chat --enable
hermes config set plugins.entries.agy-chat.settings.chat_id <your Telegram chat id>
hermes config set plugins.entries.agy-chat.settings.user_name <your name>
```

Then:

1. Add agy's workspace (`~/agy-chat` by default) to `trustedWorkspaces` in
   `~/.gemini/antigravity-cli/settings.json`, so agy loads the generated rules and MCP servers.
2. Optionally, put the commands in Telegram's `/` menu: add `agy`, `agy_on`, `agy_off`, … to
   `platforms.telegram.extra.command_menu.priority` in `config.yaml`.
3. Restart the gateway, then send `/agy_doctor` in Telegram.

Update with `hermes plugins update agy-chat`, then restart the gateway.

## Settings

Under `plugins.entries.agy-chat.settings` in `config.yaml` (also editable in the Desktop app):

| Key | Default | |
|---|---|---|
| `chat_id` | — | **Required.** The only Telegram chat routed to agy. Nothing is routed while it's empty. |
| `user_name` | `the user` | How agy's context refers to you. |
| `agy_path` | `agy` on `PATH`, else `~/.local/bin/agy` | The agy CLI. |
| `workspace` | `~/agy-chat` | agy's working directory: `inbox/` (your files), `out/` (its files), `.agents/` (generated). |
| `default_model` | `gemini-3.8-flash-high` | Model until `/agy_model` picks another. |
| `machine_notes` | — | Free text about the machine added to agy's context. |
| `skip_skills` | `[]` | Hermes skill names hidden from agy. |
| `timeout_minutes` | `30` | Longest agy turn. |
| **Display** | | |
| `mark` | `🛸` | Emoji in the footer, status and banner (`""` for none). |
| `react_working`, `react_queued` | `👀`, `✍` | Reactions on your messages while agy works / while they wait (`""` for none). |
| `footer` | `true` | `— 🛸 agy · 2 min` under answers. |
| `verbose` | `false` | Live status of agy's actions by default. |
| **Voice** | | |
| `voice_input` | `direct` | `direct`: the audio goes to agy (Google); `local`: Hermes' Whisper transcribes it first. |
| `voice_reply` | `off` | `off`, `mirror` (voice answer when you sent a voice note), `always`. |
| `tts_provider` | `""` | `""`: Hermes' TTS settings; `edge`: Edge TTS directly (free, no key), whatever Hermes uses. |
| `tts_voice` | Hermes' `tts.edge.voice` | Edge voice, e.g. `fr-FR-DeniseNeural`. |
| **What agy gets from Hermes** | | |
| `share_soul`, `share_memory`, `share_thread` | `true` | Personality, memory, recent Telegram thread, in agy's context. |
| `share_skills`, `share_mcp_servers` | `true` | Hermes' skills and MCP servers. |
| `bridge_tools` | all | Tools of the bundled bridge: `memory`, `session_search`, `skill_manage`, `cronjob_manage`, `text_to_speech`, `telegram_send`, `hermes_thread`, `agy_task`. `[]` turns it off. |

`/agy_verbose` and `/agy_voice` override the display and voice defaults from Telegram.

agy's context is written in English; agy answers in your language.

## Security

agy runs as the gateway's user with `--dangerously-skip-permissions`: **whoever can write in the
configured chat can run anything that user can**. Set `chat_id` to your own DM with the bot, and
think twice before running the gateway as a user with `sudo`. API keys from the gateway's
environment are not passed to agy, but agy's MCP config holds the bearer tokens of Hermes' MCP
servers (written with mode 600).

## Data

Plugin state (mode, model, conversation, tasks) and the turn log live in
`<HERMES_HOME>/plugin-data/agy-chat/`, outside the install directory, so updates keep them.

## Development

The tests drive whole turns against a fake agy (`tests/fake_agy.py`) and a fake Telegram adapter.
They import Hermes' gateway modules, so run them with Hermes' interpreter:

```bash
~/.hermes/hermes-agent/venv/bin/python tests/test_agy_chat.py
hermes plugins validate .
```

## License

MIT
