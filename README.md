# PairDown

Pair Markdown with Hermes.

PairDown is a Hermes Desktop plugin for live-preview editing of the Markdown notes in a local vault (Obsidian-compatible), side by side with a Hermes conversation.

## Status

Early development. Nothing is released yet.

## Planned architecture

- A native Hermes Desktop pane (`@hermes/plugin-sdk`) for navigation and editing.
- A CodeMirror 6 editor with live preview.
- A Python backend (`plugin_api.py`) for vault file access through `pathlib`, so the plugin behaves the same on Linux, macOS and Windows without shell commands.

## License

MIT
