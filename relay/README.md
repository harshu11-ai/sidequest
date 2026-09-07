# sidequest chess relay

A small FastAPI service that lets two `sidequest` clients on different
networks play multiplayer chess against each other. It's the authoritative
source of truth for board state — every move is re-validated here with
[Chessnut](https://pypi.org/project/chessnut/), independent of what either
client believes is legal, since this service is reachable by anyone who
installs `sidequest`, not just a trusted pair of players.

Room state lives entirely in this process's memory (`app/store.py`). Games
are short-lived, so losing one on a restart/redeploy is an accepted v1
tradeoff — this deliberately avoids a database. The corollary: **this
service must run as a single instance.** Two instances behind a load
balancer would each hold a different dict of rooms and diverge.

## API

| Route | Method | Purpose |
| --- | --- | --- |
| `/rooms` | POST | Create a room. Returns `{code, token, you: "white"}`. |
| `/rooms/{code}/join` | POST | Take the open black seat. Returns `{code, token, you: "black"}`. |
| `/rooms/{code}/state?token=...` | GET | Poll the current board snapshot (also a presence heartbeat). |
| `/rooms/{code}/move` | POST | `{token, move, expected_move_count}` — validated server-side. |
| `/healthz` | GET | Liveness check. |

`expected_move_count` is optimistic concurrency: it must equal the room's
current move count, or the move is rejected with 409 so a stale client
doesn't silently clobber a move it didn't see yet.

## Running locally

```sh
cd relay
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8811
```

## Tests

```sh
.venv/bin/python -m pytest tests -q
```

## Deploying

`render.yaml` at the repo root is a Render Blueprint targeting this
directory (`rootDir: relay`). It defaults to Render's free plan, which
sleeps after ~15 minutes idle and takes 30-60s to wake back up on the next
request — fine for occasional/personal use, worth upgrading to a paid plan
once this sees regular traffic to avoid that lag. Whatever plan you pick,
do not enable autoscaling/multiple instances (see above).
