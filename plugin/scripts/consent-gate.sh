#!/bin/bash
# PreToolUse(Bash) consent gate: asks for a permission prompt on the call that confirms a
# grepogram consent summary (research approval, account removal, leaving a chat).
#
# --confirm exists only on `research approve`, `accounts rm` and `leave`, so the gate asks on
# any payload naming grepogram and --confirm, in that order, ignoring case and whatever lies
# between them: line continuations, tabs (both arrive JSON-escaped), quoted words, a path or
# `uv run` in front. It matches the raw payload, so it over-asks (a query or a cwd naming the
# words) rather than under-asks.
#
# A regex over the command text is defence in depth, not a sandbox: deliberate obfuscation
# (`grepogra""m`, `--con""firm`, a variable, a script file run by name) still gets past it, and
# such a call falls back to the normal permission rules.
payload=$(cat)

shopt -s nocasematch
pattern='grepogram.*--confirm'
if [[ "$payload" =~ $pattern ]]; then
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"grepogram: this confirms a consent summary (research approval, account removal or leaving a chat); check it matches what you agreed to"}}'
fi
exit 0
