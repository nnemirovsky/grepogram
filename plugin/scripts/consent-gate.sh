#!/bin/bash
# PreToolUse(Bash) consent gate: forces a permission prompt on the call that confirms a
# grepogram consent summary (research approval, account removal, leaving a chat).
# Matches the raw hook payload, so it over-asks slightly and never under-asks.
payload=$(cat)

case "$payload" in
  *grepogram*) ;;
  *) exit 0 ;;
esac

pattern='grepogram.*(research[[:space:]]+approve|accounts[[:space:]]+rm|leave).*--confirm'
if [[ "$payload" =~ $pattern ]]; then
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"grepogram: this confirms a consent summary (research approval, account removal or leaving a chat); check it matches what you agreed to"}}'
fi
exit 0
