# Single-Bot Voice WS Resume — Design

- **Date:** 2026-06-06
- **Status:** Approved (design); pending implementation plan
- **Component:** `bot.py` voice connection layer
- **Supersedes:** [`2026-05-29-single-bot-voice-presence-design.md`](2026-05-29-single-bot-voice-presence-design.md) (PR #9, closed unmerged)

## Problem

Why2K exists to hold a single guild voice channel open 24/7 so the channel's
continuous-call timer never resets. When the bot is the **sole** occupant, a
~2-second voice "blip" empties the channel, ends the call session, and resets
the timer. Observed cadence: ~2 transient voice-WebSocket closes per 16 h,
codes `1001` ("server going away" — voice migration) and `1006` ("abnormal
closure" — transport drop).

## What we tried first (and why it didn't work)

PR #9 ([2026-05-29 spec](2026-05-29-single-bot-voice-presence-design.md))
attempted to suppress the gateway "leave" by intercepting
`disconnect(cleanup=False)` in `VoiceConnectionState` and skipping the
`change_voice_state(channel=None)` emission. Static reasoning was sound:
discord.py's reconnect flow calls `disconnect(cleanup=False)` followed by
`_connect()`, and `_connect` re-asserts the same channel via
`change_voice_state(channel=channel)` — so suppressing the intermediate leave
should keep the bot continuously present.

**Live validation on 2026-06-06 falsified the static reasoning.** On a natural
`1006`:

1. The suppression branch ran correctly — no `channel=None` emitted.
2. `_inner_connect` re-asserted `change_voice_state(channel=channel)` and got
   back `VOICE_STATE_UPDATE` from Discord acknowledging the bot was still in
   the channel.
3. **Discord never sent a new `VOICE_SERVER_UPDATE`** — because from its point
   of view we never left, and the previous voice server was presumed alive, so
   no migration was needed.
4. `_inner_connect`'s `await self._wait_for_state(got_both_voice_updates)` hit
   its 30 s timeout.
5. `_poll_voice_ws` caught the `TimeoutError`, `continue`d the loop, called
   `await self.ws.poll_event()` on the already-closed WS, got close code
   `1000`, and the 1000 branch invoked `disconnect()` with `cleanup=True` —
   the genuine-disconnect path that our override deliberately did *not*
   suppress. The real leave fired ~30 s after the original close.

Compare with the May 27 production `1001` log: the old (pre-fix) leave-rejoin
got a fresh `VOICE_SERVER_UPDATE` in ~400 ms because the bot explicitly left
and rejoined — Discord allocates a new endpoint when the bot performs a join.
The smoking gun in the 2026-06-06 log is the endpoint switching from
`c-lhr10-49cdd465.discord.media:8443` to `c-lhr13-83e82067.discord.media:2053`
*only after* the bot left.

**Conclusion:** the leave is not just discord.py's choice of reconnect path —
it's the trigger that gets Discord to allocate a fresh voice server. You can't
"re-assert the same channel" your way out of a media reconnect.

## The new approach: OP 7 RESUME instead of fresh handshake

Discord's voice gateway has a `RESUME` opcode (`OP 7`). discord.py uses it for
close code `4015` ("voice server crashed") via `_connect(resume=True)`. The
mechanism: open a fresh WSS connection to the **same cached endpoint** and send
`OP 7` with the cached `session_id` and `token`. If Discord still has the
session, it replies `OP 9 RESUMED` and the session continues without needing a
new `VOICE_SERVER_UPDATE`. If the session is gone, it closes with `4006`
"session no longer valid" and we fall back.

discord.py's `4015` path still goes through `_inner_connect`, which means it
also sends `change_voice_state(channel=channel)` and waits for
`got_both_voice_updates` — so it suffers from the same potential timeout
problem if Discord doesn't send a fresh `VOICE_SERVER_UPDATE`. The `4015` case
works in practice because Discord *does* send one (voice server crash is a
server-initiated migration). For our `1001`/`1006` case where we want to
resume to the **same** endpoint, we need to bypass that wait.

## Goals

- On `1001`/`1006`, attempt to resume the voice WS to the cached endpoint
  **without** waiting for a new `VOICE_SERVER_UPDATE` and **without** emitting
  any gateway voice-state change. If resume succeeds, the bot stays in the
  channel from Discord's gateway view → participant count never hits 0 → timer
  never resets.
- On resume failure (Discord rejects, network error, timeout), **fall through
  to discord.py's upstream leave-rejoin behaviour** — the same ~2 s blip the
  bot has had all along. No worse than baseline.
- Stay on a single bot, single token, single droplet.

## Non-goals

- Bulletproof presence against every failure. If Discord doesn't accept
  resume, we accept the baseline blip.
- Audio/media. Bot is self-deaf/self-mute; never encodes or decodes audio.
- Deployment topology changes.
- Layering on top of PR #9's suppression code. PR #9 is replaced wholesale.

## Design

### Component 1 — `_try_in_place_resume` helper

A new method on `ResilientVoiceConnectionState` that attempts the resume and
returns `True` only when Discord has positively acknowledged the resume,
`False` otherwise. Responsibilities:

1. Validate the cached session is present (`endpoint`, `session_id`, `token`).
   If any are missing, return `False` immediately — there is nothing to
   resume.
2. Open a fresh WSS to the cached endpoint and send `OP 7 RESUME`, wrapped in
   a ~10 s outer timeout so a hung negotiation can never stall the loop.
   `_connect_websocket(resume=True)` is the discord.py primitive for this.
3. **Positively confirm resume success** before declaring it — for example by
   reading the first opcode and requiring `OP 9 RESUMED`. If Discord instead
   closes with `4006` ("session no longer valid") or anything else, treat
   that as failure. The exact confirmation mechanism (a hook on
   `DiscordVoiceWebSocket`, a one-shot `poll_event`, etc.) is an
   implementation choice settled in the plan.
4. On confirmed success: install the new WS on `self.ws`, set
   `self.state = ConnectionFlowState.connected`, close the previous WS in a
   `finally`, return `True`. Crucially: **do not call `_handshake_websocket`**.
   On a successful resume Discord does not re-send `OP 4 SESSION DESCRIPTION`,
   so `_handshake_websocket` would hang on `while self.ws.secret_key is None`.
   The bot is self-deaf/self-mute and never encrypts/decrypts audio, so
   `secret_key` is unused.
5. On any failure (network error, timeout, non-RESUMED first opcode, exception
   from the helper itself): close the new WS if it was created, return
   `False`. The helper must **never** call `disconnect()` — that would emit
   the gateway leave we are trying to avoid. Cleanup of the failed resume is
   strictly local to the helper.

Invariants the caller can rely on:

- Returns `True` ⇒ `self.ws` is a fresh live WS that received `OP 9 RESUMED`,
  and `self.state is ConnectionFlowState.connected`.
- Returns `False` ⇒ `self.ws` is whatever the caller passed in (possibly the
  closed-old WS); the helper has not mutated `self.state` or any gateway
  voice state. Upstream's existing leave-rejoin can run cleanly.

### Component 2 — `_poll_voice_ws` override with resume branch

Override `_poll_voice_ws` with a near-verbatim copy of discord.py 2.7.1's
implementation, with one addition: a transient-code branch checked **before**
the generic fall-through. Pseudocode of the new branch:

```
if exc.code in (1001, 1006):
    _log.info("Attempting in-place voice WS resume (code %d)", exc.code)
    if await self._try_in_place_resume():
        _log.info("Voice WS resumed in place; no channel leave.")
        continue
    _log.info("In-place resume failed; falling back to leave-rejoin.")
# fall through to upstream generic reconnect (unchanged)
```

All other branches (`1000`, `4014`/`4022`, `4021`, `4015`, the generic
fall-through) are preserved verbatim from upstream. This is heavy coupling —
~75 lines copied — guarded by the existing pinned-version test
(`test_targets_pinned_discord_version`) so any discord.py bump fails CI until
re-audited.

### Component 3 — `ResilientVoiceClient` + `bot.py` wiring

A `ResilientVoiceClient(VoiceClient)` whose `create_connection_state()` returns
the new state subclass. `bot.py` calls `channel.connect(cls=ResilientVoiceClient,
self_deaf=True, self_mute=True)` — identical hook to PR #9. The existing
`on_voice_state_update` and `on_ready` handlers in `bot.py` continue to act as
the safety net for any path that does end up leaving the channel.

### What stays unchanged

- `connect_to_voice()` retry/backoff and its fatal-vs-retryable classification.
- The short-session **circuit breaker** in `bot.py`.
- The health-check server and readiness semantics.
- Deployment.

### What is explicitly removed compared to PR #9

- The `_suppress_gateway_leave` flag and `disconnect`/`_voice_disconnect`
  overrides — gone. The new design does not suppress the leave; it tries to
  avoid emitting one by resuming, and accepts the leave as the fallback.
- The `voice_resilience` logger level bump in `bot.py` stays valuable (the new
  helper also logs at INFO).

## Close-code routing summary

| Close code | Path |
|---|---|
| `1000` clean close | Upstream default (disconnect, no auto-reconnect) |
| `1001` server going away | **Try resume → on failure, upstream leave-rejoin** |
| `1006` abnormal closure | **Try resume → on failure, upstream leave-rejoin** |
| `4014`/`4022` kicked / moved | Upstream default (`_potential_reconnect` or genuine leave) |
| `4015` voice server crashed | Upstream default (`_connect(resume=True)`) |
| `4021` ratelimit | Upstream default (disconnect) |
| Other unhandled | Upstream default (generic reconnect — same baseline behaviour) |

## Failure modes and how the design handles each

| Resume outcome | What discord.py / our code does | Net effect |
|---|---|---|
| `OP 9 RESUMED` received fast | State set to connected; loop continues on new WS | **No leave, timer holds** ✅ |
| Discord closes new WS with `4006` "session invalid" | `ConnectionClosed` caught, helper returns False | Fall through to upstream leave-rejoin — baseline ~2 s blip |
| WS open succeeds but `_handshake_websocket` hangs (Discord didn't re-send `OP 4`) | 10 s `wait_for` timeout fires, helper returns False | Fall through to upstream leave-rejoin |
| Network error opening new WS | Exception caught, helper returns False | Fall through to upstream leave-rejoin |
| Resume succeeds but voice media is broken (no audio) | We don't care — bot is self-deaf/self-mute | ✅ |

There is no scenario where this design is worse than the pre-PR-#9 baseline.

## Validation

### Unit tests

The decision logic is pure code with mockable seams:

- `_try_in_place_resume` returns `False` when `endpoint`/`session_id`/`token`
  is unset.
- Returns `True` when `_connect_websocket(resume=True)` succeeds (mocked).
- Returns `False` when `_connect_websocket` raises.
- Returns `False` when `_handshake_websocket` hangs past the 10 s timeout
  (use a very short test timeout via patching).
- `_poll_voice_ws` override routes `1001`/`1006` through `_try_in_place_resume`
  before fall-through (verifiable via a mocked helper returning True/False).
- `_poll_voice_ws` override leaves other close codes' behaviour unchanged
  (smoke-check the copied branches against upstream behaviour).

The pinned-version tripwire (`test_targets_pinned_discord_version`) stays in.

### Live force-test

`ss -K dst <voice-ip> dport = 8443` now becomes a valid test because the
fallback is graceful. Two informative outcomes:

- Journal shows `Voice WS resumed in place; no channel leave.` and no `Voice
  state changed: channel … -> None` → resume works for hard kills. Strong
  signal the fix will help in production.
- Journal shows `In-place resume failed; falling back to leave-rejoin.`
  followed by the normal leave-rejoin sequence → resume doesn't work for hard
  kills (Discord cleaned up the session). We'd then wait for a natural drop to
  see if resume works under softer failure modes.

### Production observation

After deploy, watch the journal for natural `1001`/`1006` events. Success on
even some fraction of them is a net improvement over baseline. Failure on all
of them means the resume mechanism doesn't help in practice — at which point
we have a known, well-defined baseline to fall back to (close this PR like
PR #9, accept the original blip).

## Risks & mitigations

- **discord.py internal coupling** (`_poll_voice_ws` copied, `_connect_websocket`
  and `_handshake_websocket` called). Mitigation: discord.py is pinned at
  `2.7.1`; the version-tripwire test fails on any bump.
- **Discord may not support `OP 7 RESUME` for the failure modes we hit.** This
  is the central uncertainty. Mitigation: graceful fallback. Worst case is
  baseline.
- **`_handshake_websocket` hanging post-resume.** Mitigation: 10 s outer
  timeout. The bot doesn't need `secret_key` (mute/deaf), so we don't lose
  functionality if we set state to `connected` without completing handshake.
- **Cascading leave on resume failure.** If `_try_in_place_resume` itself
  leaks some state that confuses upstream (e.g. closes the old WS while the
  fall-through expects it open), the upstream reconnect might behave oddly.
  Mitigation: close `previous_ws` in a `finally` regardless, and ensure
  upstream's path starts from a clean `disconnect(cleanup=False)`.

## Rollback

Revert to stock `VoiceClient` by removing the `cls=ResilientVoiceClient`
override in `bot.py`. Behaviour returns to pre-fix baseline (~2 s leave-rejoin
on 1001/1006). No data or schema migration. The voice_resilience module can
be left in the tree for future iteration.
