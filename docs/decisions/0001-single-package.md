# 0001: One package, subpackage per concern

**Status:** accepted, 2026-10-02

The design left open whether to split `zordon-core` and `zordon-web`. There is
one consumer (the `zordon` CLI) and one process. A split would add a second
`pyproject.toml`, a version coupling, and nothing a user can see. The web client
is static files inside `zordon/web/`, served through `importlib.resources`.

If a second transport (the hosted relay) ever exists, `zordon.transport` is the
only package that knows about WebSockets, so the split can happen then.
