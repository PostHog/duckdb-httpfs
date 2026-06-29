#!/usr/bin/env bash

PORT="${EXPIRED_TOKEN_S3_TEST_PORT:-9002}"
HOST="${EXPIRED_TOKEN_S3_TEST_HOST:-127.0.0.1}"
STATE_FILE="${EXPIRED_TOKEN_S3_STATE_FILE:-/tmp/duckdb-httpfs-expired-token-s3-state.json}"
LOG_FILE="${EXPIRED_TOKEN_S3_LOG_FILE:-/tmp/duckdb-httpfs-expired-token-s3.log}"
SERVER_ID="${EXPIRED_TOKEN_S3_SERVER_ID:-expired-token-s3-${BASHPID:-$$}-${RANDOM:-0}}"
WAIT_TIMEOUT="${EXPIRED_TOKEN_S3_WAIT_TIMEOUT:-30.0}"

rm -f "$STATE_FILE" "$LOG_FILE"
python3 scripts/expired_token_s3_server.py \
    --host "$HOST" \
    --port "$PORT" \
    --state-file "$STATE_FILE" \
    --server-id "$SERVER_ID" \
    --wait-timeout "$WAIT_TIMEOUT" \
    >"$LOG_FILE" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        cat "$LOG_FILE" >&2 || true
        echo "ExpiredToken S3 test server exited before becoming ready" >&2
        return 1 2>/dev/null || exit 1
    fi
    HEALTH="$(curl -fsS "http://$HOST:$PORT/__health" 2>/dev/null || true)"
    if [ "$HEALTH" = "$SERVER_ID" ]; then
        export EXPIRED_TOKEN_S3_TEST_SERVER_AVAILABLE=1
        export EXPIRED_TOKEN_S3_ENDPOINT="$HOST:$PORT"
        export EXPIRED_TOKEN_S3_STATE_FILE="$STATE_FILE"
        export EXPIRED_TOKEN_S3_SERVER_PID="$SERVER_PID"
        return 0 2>/dev/null || exit 0
    fi
    sleep 0.1
done

cat "$LOG_FILE" >&2 || true
kill "$SERVER_PID" 2>/dev/null || true
echo "ExpiredToken S3 test server did not become ready" >&2
return 1 2>/dev/null || exit 1
