#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_HOME="$HOME/Library/Application Support/WeChatAgent"
PID_FILE="$AGENT_HOME/voice-transcript-sidecar.pid"
SIDECAR_BIN="$ROOT_DIR/tools/voice_transcript_sidecar/voice-transcript-sidecar"
PID="$(cat "$PID_FILE" 2>/dev/null || true)"

if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  CMD="$(ps -p "$PID" -o command= 2>/dev/null || true)"
  if [[ "$CMD" == *"$SIDECAR_BIN"* ]]; then
    kill "$PID" 2>/dev/null || true
    echo "Stopped voice transcript sidecar PID=$PID"
  else
    echo "PID file points to a non-sidecar process; refusing to stop it" >&2
    exit 1
  fi
fi
rm -f "$PID_FILE"
