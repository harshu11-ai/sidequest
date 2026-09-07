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
- optional, resumable chess breaks while an agent turn is running
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

To play chess while Codex or Claude works:

```bash
sidequest --chess codex
sidequest --chess claude
```

Submitting a prompt opens a separate chess window. The game is saved locally,
closes when the agent finishes or requests approval, and resumes after your next
prompt. You play White against a built-in practice opponent by default. Use the
in-game difficulty menu to choose Easy, Medium, or Hard; the choice is saved with
your game. The built-in opponent ranges from basic legal moves to a short
look-ahead. With Stockfish, the same setting controls its skill level and thinking
time.

For a stronger opponent, install [Stockfish](https://stockfishchess.org/) and
make sure `stockfish` is on `PATH`. On macOS with Homebrew:

```bash
brew install stockfish
sidequest --chess codex
```

You can also provide the executable explicitly:

```bash
sidequest --chess --stockfish /path/to/stockfish claude
```

The companion binds only to the loopback interface and protects its game API
with a random per-session token. Chrome, Brave, Edge, or Chromium is used for a
separate app-style window when available; otherwise it opens in the default
browser.

To play against another `sidequest` user instead of the built-in opponent:

```bash
sidequest --chess --multiplayer codex     # hosts a game and prints a code to share
sidequest --chess --join ABC123 claude    # joins with the code you were given
```

Sidequest's own flags (`--chess`, `--multiplayer`, `--join`, `--relay-url`,
`--stockfish`, ...) always go *before* `claude`/`codex` -- anything after the
application name is passed straight through to it unchanged, so
`sidequest --chess codex --multiplayer` sends `--multiplayer` to Codex, not to
sidequest.

While it's not your turn, the game window offers a "play vs bot while you
wait" toggle so you're not stuck watching an empty board -- your room stays
tracked in the background and you can switch back once your opponent moves.
Games are hosted through a small relay service so the two of you don't need
to be on the same network; pass `--relay-url` to use a self-hosted one
instead of the default.

Wrapper options such as `--config` and `--no-corrections` must come before the
application name. Everything after `claude` or `codex` belongs to that app.

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
Chess mode stores only the current board position beneath
`~/.local/state/sidequest/` (or `XDG_STATE_HOME`) so games can resume.

## License

[MIT](LICENSE)
