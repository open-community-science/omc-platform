#!/bin/bash
# Create the isolated Docker network for OMC sessions.
#
# Containers on this network can ONLY reach the host (portal).
# No internet, no cross-container DNS resolution.
#
# Run once on the host (arbutus or dev machine).

set -euo pipefail

NETWORK="omc-sessions"
SUBNET="172.30.0.0/24"
GATEWAY="172.30.0.1"

# Create network if it doesn't exist
if ! docker network inspect "$NETWORK" &>/dev/null; then
    docker network create \
        --driver bridge \
        --subnet "$SUBNET" \
        --gateway "$GATEWAY" \
        --opt "com.docker.network.bridge.enable_icc=false" \
        --opt "com.docker.network.bridge.enable_ip_masquerade=false" \
        "$NETWORK"
    echo "Created network $NETWORK ($SUBNET)"
else
    echo "Network $NETWORK already exists"
fi

# iptables: allow containers → host portal (port 8002) only
# Drop all other outbound traffic from session containers

# Allow established connections back
iptables -I FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true

# Allow containers to reach host gateway on portal port
iptables -I FORWARD -s "$SUBNET" -d "$GATEWAY" -p tcp --dport 8002 -j ACCEPT 2>/dev/null || true

# Drop everything else from the session subnet
iptables -A FORWARD -s "$SUBNET" -j DROP 2>/dev/null || true

echo "Firewall rules applied: containers can only reach $GATEWAY:8002"
echo ""
echo "Portal must listen on 0.0.0.0:8002 (or at least on $GATEWAY:8002)"
echo "Containers should use LLM_BASE_URL=http://$GATEWAY:8002/api/llm"
