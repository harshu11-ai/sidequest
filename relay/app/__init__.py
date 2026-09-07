"""Sidequest multiplayer chess relay.

A small, deliberately stateless-to-disk FastAPI service that lets two
`sidequest` clients on different networks play chess against each other. It
is the source of truth for board state: every move is re-validated here,
independent of what either client believes is legal, because this service is
reachable by anyone who installs `sidequest`, not just a trusted pair of
players.
"""
