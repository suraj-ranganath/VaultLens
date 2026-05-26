#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_ROOT="$REPO_ROOT/config/obsidian"
OBSIDIAN_DIR="$REPO_ROOT/.obsidian"

mkdir -p "$OBSIDIAN_DIR/snippets"

cp "$CONFIG_ROOT/app.json" "$OBSIDIAN_DIR/app.json"
cp "$CONFIG_ROOT/appearance.json" "$OBSIDIAN_DIR/appearance.json"
cp "$CONFIG_ROOT/core-plugins.json" "$OBSIDIAN_DIR/core-plugins.json"
cp "$CONFIG_ROOT/community-plugins.json" "$OBSIDIAN_DIR/community-plugins.json"
cp "$CONFIG_ROOT/graph.json" "$OBSIDIAN_DIR/graph.json"
cp "$CONFIG_ROOT/snippets/vault-colors.css" "$OBSIDIAN_DIR/snippets/vault-colors.css"

echo "Obsidian vault defaults installed into $OBSIDIAN_DIR"
echo "Next steps:"
echo "  1. Open this folder as a vault in Obsidian"
echo "  2. Enable community plugins if you use them"
echo "  3. Run: bun run rebuild:dashboards"
echo "  4. Open dashboards/dashboard.base in Obsidian Bases"
