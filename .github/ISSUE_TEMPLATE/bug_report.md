---
name: Bug report
about: Something grepogram does wrong, or refuses to do
title: ''
labels: bug
assignees: ''
---

## What happened

<!-- What you ran and what it did. Paste the command and the output; use `--json` where the
     command offers it. Redact message text and chat names freely — the shapes matter, not the
     content. -->

## What you expected instead

## Environment

- **grepogram version**: <!-- `grepogram --version` -->
- **macOS version**: <!-- Apple menu → About This Mac, or `sw_vers -productVersion` — and say
      whether it is Apple Silicon or Intel -->
- **Optional extras installed**: <!-- `dense` (torch, sentence-transformers), `media` (pypdf,
      python-docx, Vision OCR), both, or neither. `uv tool install` / `uv sync` shows which you
      asked for; `grepogram search` warns when the dense side is unavailable. -->
- **Installed how**: <!-- `uv tool install`, `uv run` from a checkout, or something else -->
- **Python**: <!-- any command that opens the index fails with `ExtensionsUnsupported` when
      the interpreter cannot load SQLite extensions; if you hit that, say which python3.12 uv
      picked -->

## Log

<!-- `grepogram config path` prints the log location. The tail around the failure is usually
     enough. Message text is redacted above DEBUG, but check before pasting. -->
