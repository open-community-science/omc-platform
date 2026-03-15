---
name: relay
description: Send or check messages via the OMC agent relay (local <-> HPC communication)
allowed-tools: Bash
argument-hint: "[message, or read/poll/watch/channels]"
---

# Agent Relay

Communicate with Claude instances on fir (HPC) via the relay at https://microbial.opencommunity.science/relay/

Uses the CLI at `~/.local/bin/relay`. Auth and JSON escaping handled automatically.

## Parse $ARGUMENTS

- **No args** or `read [N] [channel]`: show recent messages → `relay read 5`
- **`poll [channel]`**: block waiting for new messages → `relay poll`
- **`watch [channel]`**: continuous stream → `relay watch`
- **`channels`**: list active channels → `relay channels`
- **`send [-c channel] MESSAGE`**: send explicitly → `relay send "MESSAGE"`
- **`send -c debug MESSAGE`**: send to a specific channel → `relay send -c debug "MESSAGE"`
- **`that`**: summarize the last thing you told the user and send it over relay → `relay send "SUMMARY"`
- **Any other text**: treat as a message to send → `relay send "$ARGUMENTS"`

Default channel is `general`. Use `-c CHANNEL` with send or pass channel as last arg to read/poll/watch.

## Examples

```bash
# /relay              → relay read 5
# /relay poll         → relay poll
# /relay channels     → relay channels
# /relay hey fir!     → relay send "hey fir!"
# /relay read 10 debug → relay read 10 debug
# /relay that         → summarize last response, then relay send "SUMMARY"
```

## Special: `/relay that`

When the argument is `that`, do NOT send the literal word "that". Instead:
1. Recall the last substantive thing you told the user in this conversation
2. Write a concise summary (1-3 sentences) capturing the key finding, status, or action
3. Send that summary via `relay send "SUMMARY"`

This is useful for quickly forwarding findings or status to the other side without retyping.

Summarize the response concisely for the user.
