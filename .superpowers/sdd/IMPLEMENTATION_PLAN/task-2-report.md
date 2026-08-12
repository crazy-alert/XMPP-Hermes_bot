# Task 2 report: XMPP policy

## Status

PASS. Implemented pure authorization, routing, and session-key policy without
network, state, or adapter changes.

## Files

- `hermes-xmpp/xmpp_bridge/policy.py`
- `tests/test_policy.py`

## TDD evidence

RED commit: `34d77258942d842d1fa7e6dfd5284c749afb3280`

RED command:

```text
.\\.venv\\Scripts\\python.exe -m pytest -q tests/test_policy.py
```

RED output (expected):

```text
ERROR tests/test_policy.py
ModuleNotFoundError: No module named 'xmpp_bridge.policy'
1 error in 0.64s
```

GREEN commit: `6b2d7d4b4e32c15ac2365dc47b7cbf5df0738ad2`

GREEN command:

```text
.\\.venv\\Scripts\\python.exe -m pytest -q tests/test_policy.py
```

GREEN output:

```text
30 passed, 1 warning in 0.37s
```

The warning was from a pre-existing inaccessible `.pytest_cache`. Verification
without pytest cache provider was clean:

```text
.\\.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider
33 passed in 0.45s
```

## Self-review

- Authorization is based only on normalized bare JIDs; MUC nicknames never
  authorize a sender.
- Denied MUC users are rejected before mention or reply activation.
- Mention recognition is case-insensitive, token-boundary limited, leading-only,
  and rejects an empty remaining body.
- Reply activation requires a cached bot message ID.
- Session keys normalize every JID and reject invalid event shapes.
- `git show --check` reported no whitespace errors in the GREEN commit.

## Concerns

- `slixmpp` is not installed in the local test venv, so the safe fallback JID
  parser was exercised. The optional Slixmpp path remains deliberately lazy.
- Git's normal sandbox could not create `.git/index.lock`; the scoped GREEN
  commit was created with approved escalation. Only the two Task 2 files were
  staged.
