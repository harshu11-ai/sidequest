# Sidequest

[![CI](https://github.com/harshu11-ai/sidequest/actions/workflows/ci.yml/badge.svg)](https://github.com/harshu11-ai/sidequest/actions/workflows/ci.yml)

Sidequest is a local companion for [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
and the [Codex CLI](https://github.com/openai/codex). It fixes high-confidence
typos while you type and can open a resumable chess game while your agent works.
It wraps the existing CLI, so you keep using the normal Claude or Codex interface.

```text
can you fix teh fucntion
                ↓
can you fix the function
```

Corrections happen after Space or Enter. The wrapped application receives
ordinary backspace and replacement keystrokes, as if you corrected the word
yourself.

## Features

- local English correction with no network requests or telemetry
- built specifically for Claude Code and Codex CLI
- common typo, transposition, extra-character, and high-confidence spelling fixes
- protection for paths, URLs, flags, identifiers, and mixed alphanumeric terms
- pasted text passed through unchanged
- immediate Backspace to undo the last correction
- optional personal corrections in a small JSON config file
- optional `--breaks` control panel: toggle resumable chess and/or educational-video breaks live while an agent turn is running, no relaunch needed
- transparent `--no-corrections` mode for terminal troubleshooting

## Install

For a clean, isolated command-line installation, use
[pipx](https://pipx.pypa.io/):

```bash
pipx install git+https://github.com/harshu11-ai/sidequest.git
```

Or install it in a virtual environment with pip:

```bash
git clone https://github.com/harshu11-ai/sidequest.git
cd sidequest
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Verify the installation:

```bash
sidequest --doctor
```

The doctor reports the Python and platform versions, local dictionary status,
config path, terminal status, and whether `claude` and `codex` are on `PATH`.

## Usage

Launch either supported application through the wrapper:

```bash
sidequest claude
sidequest codex
```

Arguments after the application name are passed through unchanged:

```bash
sidequest codex --model MODEL_NAME
```

To test the PTY wrapper without changing any input:

```bash
sidequest --no-corrections codex
```

To open the breaks control panel while Codex or Claude works:

```bash
sidequest --breaks codex
sidequest --breaks claude
```

A small app-style panel opens as soon as `sidequest --breaks` launches --
before you've even typed a prompt -- with Chess and Video as independent
toggles, both off by default. With nothing toggled on, the panel just sits
there quietly for as long as you leave it alone; there's no close button to
look for. Toggle Chess (or Video, or both) on and an "All set" button
appears -- click it and the panel closes so you can go type your prompt.
(If you toggle something on and just go type a prompt anyway without
clicking it, that works too -- the panel closes on its own once a real turn
starts.) If you close the panel yourself with nothing toggled on, that's
respected: it won't reopen for the rest of this session.

Once something's toggled on, breaks run their normal course: prompt -> the
chosen game opens (at random between Chess and Video if both are on) ->
closes for real when the agent finishes, resuming exactly where you left
off next time, in another session, or under a different agent entirely --
`--breaks` is agent-agnostic. A small toggle strip pinned to the top of the
break window itself is how you change what's on for next time.

In chess, you play White against a built-in practice opponent by default.
Use the panel's difficulty control to choose Easy, Medium, or Hard; the
choice is saved with your game. The built-in opponent ranges from basic
legal moves to a short look-ahead. With Stockfish, the same setting controls
its skill level and thinking time.

For a stronger opponent, install [Stockfish](https://stockfishchess.org/)
and make sure `stockfish` is on `PATH` -- it's picked up automatically. On
macOS with Homebrew:

```bash
brew install stockfish
sidequest --breaks codex
```

To use a Stockfish install that isn't on `PATH`, enter its path in the
panel's Chess settings instead of passing a flag.

The companion binds only to the loopback interface and protects its game API
with a random per-session token. Chrome, Brave, Edge, or Chromium is used for a
separate app-style window when available; otherwise it opens in the default
browser.

Video breaks play through a bundled catalog of math, science, and
engineering videos (3Blue1Brown, Veritasium, Kurzgesagt, and others), with
play/pause, skip, and go-back controls; unlike the local practice bot, this
streams from YouTube, so it needs a network connection and isn't covered by
the "no network requests" guarantee that applies to corrections and the
local chess bot.

To play against another `sidequest` user instead of the built-in opponent,
switch Chess to Multiplayer in the panel's Chess settings, then Host a game
(a room code appears to share) or Join with a code you were given -- same
settings block as difficulty and Stockfish, just its Multiplayer tab.

While it's not your turn, the game window offers a "play vs bot while you
wait" toggle so you're not stuck watching an empty board -- your room stays
tracked in the background and you can switch back once your opponent moves.
Games are hosted through a small relay service so the two of you don't need
to be on the same network; enter a different one in the panel's Relay URL
field to use a self-hosted relay instead of the default.

Each seat is remembered per machine (under `~/.local/state/sidequest/`), so
hosting and joining the *same* room from two windows on one laptop -- for
example, to try both sides yourself -- makes them collide on one shared
seat. Real opponents on their own machines never hit this. If you do want
to run two seats on one machine, give each a different Profile in the
panel.

Wrapper options such as `--config` and `--no-corrections` must come before
the application name. Everything after `claude` or `codex` belongs to that
app.

Update a pipx-managed installation from GitHub with:

```bash
sidequest update
```

The previous `cauto` command remains available as a backward-compatible alias.

## Personal corrections

Create `~/.config/sidequest/config.json` to add corrections specific to
your typing:

```json
{
  "corrections": {
    "awsome": "awesome",
    "reccomend": "recommend"
  }
}
```

Keys and values must be different lowercase words containing only ASCII
letters. Invalid configuration is reported clearly and prevents corrections
from starting.

Use another file for one session with:

```bash
sidequest --config /path/to/config.json claude
```

On systems that set `XDG_CONFIG_HOME`, the default file is stored beneath that
directory instead of `~/.config`. Existing `~/.config/cli-autocorrect/config.json`
files are still loaded automatically when the new path does not exist.

## Safety model

Sidequest prefers missing a typo over changing code or technical terms.
It does not correct:

- pasted content
- paths, URLs, email addresses, and command-line flags
- `camelCase`, `snake_case`, and `kebab-case`
- uppercase or mixed-alphanumeric tokens
- ambiguous words without a clearly preferred correction
- the rest of a line after cursor movement or an unknown terminal escape sequence

For example, `wnat` remains unchanged because both “want” and “what” are
plausible. Pressing Backspace immediately after a correction restores the
original word.

## Current boundaries

This release corrects completed, lowercase English words. It intentionally does
not rewrite grammar, split merged words such as `toteh`, repair misplaced spaces,
or modify text that was pasted. Those changes need stronger context and more
guardrails than ordinary spelling correction.

The PTY wrapper has been smoke-tested with Codex CLI 0.151.0 and Claude Code
2.1.252 on macOS. Compatibility is continuously tested on macOS and Linux, but
interactive terminal behavior can still differ between terminal emulators.

## Development

Install the project and its development tools:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the same checks used by CI:

```bash
python -m ruff check .
python -m unittest discover -s tests -v
python -m build
python -m twine check dist/*
```

The package uses a small correction-engine interface, so the spelling engine
can be replaced without changing the terminal input processor.

## Privacy

Prompts are processed in memory on the local machine. Sidequest does not
store prompts, terminal output, environment variables, or source code.
Chess breaks store only the current board position beneath
`~/.local/state/sidequest/` (or `XDG_STATE_HOME`) so games can resume.
Video breaks store only your queue position and playback position the same
way; unlike chess against the built-in bot, video streams from YouTube, so
YouTube receives normal video-playback requests while that break is open.

## License

[MIT](LICENSE)
