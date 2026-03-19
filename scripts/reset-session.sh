#!/bin/bash
# Hard-reset a session container: kill, unmount, clear portal state, relaunch.
# Usage: ./reset-session.sh <slug>

set -e
SLUG="${1:?Usage: $0 <slug>}"

echo "=== Resetting session $SLUG ==="

# 1. Kill container
echo "Removing container..."
docker rm -f "omc-session-${SLUG}" 2>/dev/null || true

# 2. Unmount squashfuse
echo "Unmounting squashfuse..."
fusermount -u "/mnt/omc-sessions/${SLUG}" 2>/dev/null || true
rmdir "/mnt/omc-sessions/${SLUG}" 2>/dev/null || true

# 3. Restart portal (clears in-memory session registry)
echo "Restarting portal..."
# SIGKILL the old process — graceful shutdown hangs on open websockets
PID=$(systemctl show omc-portal -p MainPID --value 2>/dev/null)
if [ "$PID" != "0" ] && [ -n "$PID" ]; then
    sudo kill -9 "$PID" 2>/dev/null || true
    sleep 1
fi
sudo systemctl start omc-portal

# 4. Wait for portal to be ready
echo -n "Waiting for portal"
for i in $(seq 1 30); do
    if curl -s -o /dev/null -w '' http://localhost:8002/ 2>/dev/null; then
        echo " ready"
        break
    fi
    echo -n "."
    sleep 1
done

# 5. Launch session
echo "Launching session..."
RESULT=$(curl -s -m 60 -X POST "http://localhost:8002/sessions/dev/launch/${SLUG}")
echo "$RESULT" | python3 -m json.tool 2>/dev/null || echo "$RESULT"

# 6. Wait for container to stabilize
sleep 3

# 7. Verify
if docker ps --filter "name=omc-session-${SLUG}" --format '{{.Names}} {{.Status}}' | grep -q Up; then
    echo "=== Session $SLUG is running ==="
    docker exec "omc-session-${SLUG}" ls /data/ 2>/dev/null | head -3 && echo "(data mounted)" || echo "(no data)"
else
    echo "=== FAILED: container not running ==="
    docker logs "omc-session-${SLUG}" 2>&1 | tail -10
    exit 1
fi
