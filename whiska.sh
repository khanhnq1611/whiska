#!/bin/bash
# Quick launcher for Whiska
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/venv/bin/activate" 2>/dev/null
exec python3 -m whiskacli "$@"
