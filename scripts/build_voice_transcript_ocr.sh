#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OCR_DIR="$ROOT_DIR/tools/voice_transcript_ocr"

/usr/bin/clang -fobjc-arc -framework AppKit -framework Vision -framework CoreGraphics \
  -o "$OCR_DIR/voice-transcript-ocr" "$OCR_DIR/main.m"
echo "Built window OCR observer: $OCR_DIR/voice-transcript-ocr"
