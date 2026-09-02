#!/bin/sh
set -eu

if /usr/bin/curl --fail --silent --show-error --max-time 10 \
    --output /dev/null http://127.0.0.1/api/v1/health; then
    exit 0
fi

/usr/bin/logger -t greenmind-healthcheck \
    "Local health check failed; restarting greenmind-gateway.service"
/usr/bin/systemctl restart greenmind-gateway.service
