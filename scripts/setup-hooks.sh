#!/usr/bin/env bash
# Git does not install hooks on clone. Run this once after cloning.
set -e
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true
echo "hooks installed: $(git config core.hooksPath)"
