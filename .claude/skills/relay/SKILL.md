---
name: relay
description: Send or check messages via the OMC agent relay (local <-> HPC communication)
allowed-tools: Bash
argument-hint: "[message to send, or empty to check messages]"
---

# Agent Relay

Communicate with Claude instances on fir (HPC) via the relay at https://microbial.opencommunity.science/relay/

The relay key is at `~/.config/omc/relay-key`. Always use role `"local"` when sending.

## Behavior

If the user provides a message argument, send it. If no argument (just `/relay`), poll for recent messages.

### Check messages

```bash
RELAY_KEY="$(cat ~/.config/omc/relay-key)"
curl -s "https://microbial.opencommunity.science/relay/api/chat?last=5" \
  -H "Authorization: Bearer $RELAY_KEY" | python3 -m json.tool
```

### Send a message

```bash
RELAY_KEY="$(cat ~/.config/omc/relay-key)"
curl -s -X POST "https://microbial.opencommunity.science/relay/api/chat" \
  -H "Authorization: Bearer $RELAY_KEY" \
  -H "Content-Type: application/json" \
  --data-raw '{"role":"local","content":"MESSAGE_HERE"}'
```

Replace `MESSAGE_HERE` with the user's message. If the message contains quotes or special characters, escape them properly for JSON.

Summarize the response concisely for the user.
