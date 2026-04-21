# Hooks

These hook definitions adapt the hot-cache idea from `claude-obsidian` to this vault.

Included:

- `SessionStart`: load `hot.md` silently when present
- `PostCompact`: re-read `hot.md` after compaction
- `Stop`: remind the agent to refresh `hot.md` if the vault changed

Intentionally omitted:

- auto-commit hooks for vault content. This repository keeps personal knowledge local and ignored from Git, so automatic commit behavior is not enabled by default.
