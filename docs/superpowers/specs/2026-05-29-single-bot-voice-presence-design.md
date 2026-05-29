# Single-Bot Continuous Voice Presence — Design

- **Date:** 2026-05-29
- **Status:** Implemented on branch `feat/voice-in-place-reconnect` (PR #9); pending live validation on the droplet
- **Component:** `bot.py` voice connection layer

## Problem

Why2K exists to hold a guild voice channel open 24/7 so the channel's
continuous-call timer never resets. When the bot is the **sole** occupant
(unattended hours), a ~2-second voice "blip" empties the channel, ends the call
session, and resets the timer — defeating the bot's entire purpose.

Observed in production (`journalctl -u why2k`): voice WebSocket closes with
codes `1001` ("going away" — Discord migrating its voice server) and `1006`
("abnormal closure" — transient transport drop), roughly twice per 16 hours.
Each one currently produces a brief leave/rejoin.

## Root cause

The timer keys on the bot's **gateway voice state** (the OP4 Voice State
Update that determines channel membership / participant count), *not* on the
voice **media** connection.

In discord.py 2.7.1, when the voice media WebSocket closes with an "unhandled"
code (`1001`, `1006`, and other standard `1xxx` codes), the reconnect path is a
**full teardown**:

```
_poll_voice_ws  ->  disconnect()  ->  _voice_disconnect()
                ->  guild.change_voice_state(channel=None)
```

That `change_voice_state(channel=None)` is the leave: it emits an OP4 with
`channel_id=null`, so Discord removes the bot from the channel. With the bot
alone, participant count hits 0 and the call ends.

discord.py does **not** always do this. For code `4015` ("voice server
crashed") it reconnects **in place** (`_connect(resume=True)`) — re-establishing
the media socket *without* sending `channel=None`, so the bot never leaves the
participant list. It also exposes `soft_disconnect()`, which closes the media
socket *without* the OP4 leave message.

**Conclusion:** the leave is discord.py's *choice of reconnect path* for these
codes, not a Discord requirement. Routing the transient transport codes through
the in-place path keeps the bot continuously present.

## Goals

- Eliminate the **observed** failure mode: `1001`/`1006` voice WS closes must no
  longer cause the bot to leave the channel.
- Keep the bot continuously in the channel's voice state across media
  reconnects, so participant count never drops to 0 and the timer never resets.
- Stay on a **single bot** (one Discord application/token).

## Non-goals

- Bulletproof redundancy against *every* failure (a hard, non-resumable
  gateway-level disconnect, or a host outage). Those require a second presence,
  which the operator has explicitly declined. We accept the rare residual blip.
- Audio/media functionality. The bot connects self-deaf and self-mute and
  transmits/receives nothing; only channel *presence* matters.
- Changing the deployment topology (single systemd unit on the droplet stays).

## Design

### Component 1 — In-place voice reconnect (the core fix)

Suppress the gateway "leave" for reconnect-intent teardowns so the media
connection is rebuilt without the bot ever leaving the channel.

- Provide a custom `VoiceClient` subclass (`ResilientVoiceClient`) whose
  `create_connection_state()` returns a custom
  `ResilientVoiceConnectionState`, and connect with
  `channel.connect(cls=ResilientVoiceClient, self_deaf=True, self_mute=True)`.
- In `ResilientVoiceConnectionState`, override `disconnect()`: discord.py calls
  `disconnect(cleanup=False)` **only** for reconnect-intent teardowns
  (`_poll_voice_ws` after an unhandled close code such as `1001`/`1006`, and
  `_inner_connect` retries). For those, set a flag so the gateway leave is
  suppressed; `cleanup=True` (genuine disconnects) is delegated unchanged.
- Override `_voice_disconnect()`: when the flag is set, set the connection state
  to `disconnected` but skip `change_voice_state(channel=None)`. The following
  `_connect()` re-sends `change_voice_state(channel=channel)`, re-handshaking
  the media while the gateway voice state never left the channel. The flag is
  reset in a `finally` so it cannot leak across calls. `_expecting_disconnect`
  is intentionally left untouched (mirrors discord.py's own `4015` resume path);
  Component 2 is the backstop if Discord ever emits a spurious `channel=None`.

**Scope:** This naturally covers `1001`, `1006`, and any other transient code
that falls through to discord.py's generic reconnect, while leaving genuine
disconnects untouched: normal close (`1000`), kick / channel-deleted
(`4014`/`4022` with the disconnect confirmed), and ratelimit (`4021`) all use
`cleanup=True` and still leave the channel as they should.

### Component 2 — Voice-state re-assert safety net (already partly present)

For any path that *does* result in a true leave (rare: hard gateway re-IDENTIFY,
admin move, a `4xxx` that tears down state), rejoin as fast as possible.

- Keep the existing `on_voice_state_update` handler in `bot.py` that
  re-spawns `connect_to_voice()` when the bot leaves the target channel.
- The existing `on_ready` handler in `bot.py` already re-spawns
  `connect_to_voice()` after a full gateway re-IDENTIFY — the case that drops
  voice state — so no new resume handler is required. A RESUME preserves voice
  state and needs nothing; the production log shows the gateway almost always
  RESUMEs. These existing handlers are the backstop, not the primary path.

This net accepts a possible brief blip in those rare cases; Component 1 removes
the blip for the common case.

### What stays unchanged

- `connect_to_voice()` retry/backoff and its fatal-vs-retryable classification.
- The short-session **circuit breaker** in `bot.py`. With in-place reconnect,
  healthy sessions stay long-lived, so it should effectively never trip; it
  remains as a signal for genuine UDP-egress failure.
- The health-check server and readiness semantics.
- Deployment: one `why2k.service`, one token, one droplet.

## Close-code routing summary

| Close code | Meaning | Path |
|---|---|---|
| `1000` | Clean close | Default (no auto-reconnect) |
| `1001` | Server going away (voice migration) | **In-place reconnect (new)** |
| `1006` | Abnormal closure (transport drop) | **In-place reconnect (new)** |
| other transient `1xxx` | Transport-level | **In-place reconnect (new)** |
| `4015` | Voice server crashed | In-place (discord.py default) |
| `4014` | Disconnected (kicked / channel gone) | discord.py default → Component 2 net |
| other `4xxx` | Discord voice protocol errors | discord.py default → Component 2 net |

## Validation (required before declaring success)

Static tests can't prove Discord keeps us in the channel — this must be
validated against the live channel:

1. With the bot alone in the channel and the timer running, **force a voice
   reconnect** (e.g. briefly block outbound traffic to the voice endpoint on the
   droplet to induce a `1006`, then restore).
2. From a separate observer account, confirm the bot **never disappears** from
   the channel member list during the reconnect.
3. Confirm the call timer **does not reset**.
4. Inspect `journalctl -u why2k` to confirm the new in-place path ran (reconnect
   without a `channel ... -> None` line).

## Risks & mitigations

- **Couples to discord.py internals** (`VoiceConnectionState._poll_voice_ws`,
  `soft_disconnect`). Mitigation: discord.py is pinned `==2.7.1`; this matches
  the project's existing internal-coupling posture (see the retryable-exception
  string matching in `bot.py` (the `_RETRYABLE_CLIENT_EXCEPTION_PHRASES` list)
  with its "revisit on version bump" comment).
  Add a comment tying the override to the pinned version, and a re-check item on
  any discord.py bump.
- **Residual hard-disconnect blip.** A non-resumable gateway disconnect can
  still cause a brief leave. Mitigation: Component 2 minimises the gap; full
  immunity would need a second presence (out of scope by operator choice).
- **Discord behaviour change.** If a future Discord change tears down voice
  state on media drop regardless, the fix degrades to Component 2 behaviour
  (rejoin) — no worse than today. The validation step is what confirms current
  behaviour.

## Testing approach

- **Unit:** the implementation keys on `cleanup`, not on close codes, so the
  tests exercise the suppression mechanism directly: `_voice_disconnect()` skips
  the gateway leave when the suppress flag is set and emits it otherwise;
  `disconnect(cleanup=False)` suppresses the leave while `cleanup=True` leaves
  normally; the suppress flag resets via `finally` even when the teardown
  raises; and `ResilientVoiceClient.create_connection_state()` returns the
  resilient state. (Mapping specific close codes to these paths is a property of
  discord.py's `_poll_voice_ws`, not of this module, so it is covered by live
  validation rather than unit tests.)
- **Integration / live:** the Validation section above.

## Rollback

Revert to the stock `VoiceClient` (remove the `cls=` override). Behaviour
returns to today's leave/rejoin. No data or schema migration involved.
