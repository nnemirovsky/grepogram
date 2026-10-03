#!/bin/bash
# PreToolUse(Bash) consent gate: asks for a permission prompt on every call of the three
# grepogram commands that confirm a consent summary — `research approve`, `accounts rm` and
# `leave` — the first, summary-printing call as well as the `--confirm` one. The first call
# prints the confirming `command` with the token in it, so asking only on `--confirm` would let
# `… --json | jq -r .command | sh` (or an `eval` of it) confirm in one call nobody saw.
#
# It asks, ignoring case, on a payload naming grepogram and then --confirm, `research approve`,
# `accounts rm`, or `leave` as the subcommand. Between the two words of a command, and between
# grepogram and `leave`, it accepts what an honest call puts there: whitespace, quotes and JSON
# escapes (a tab arrives as \t, a line continuation as \\\n, so no word boundary before rm),
# and dash-led tokens, since click takes `--` (and the root takes -v / --verbose) there:
# `grepogram research -- approve` and `grepogram -v -- leave` are valid calls. Anything may sit
# between grepogram and the group word, which covers `grepogram -- research approve`. It
# matches the raw payload, so it over-asks (a query or a cwd naming the words) rather than
# under-asks; the words must be separated by nothing but those, so `accounts form` or a search
# for the word leave stays silent.
#
# A regex over the command text is defence in depth, not a sandbox: deliberate obfuscation
# (`grepogra""m`, `--con""firm`, a variable, a script file run by name) still gets past it, and
# such a call falls back to the normal permission rules.
payload=$(cat)

shopt -s nocasematch
sep='([[:space:]'\''"]|\\[tnr"\\])*'
opt="(${sep}-[-[:alnum:]]*)*"
pattern="grepogram.*(--confirm|research${opt}${sep}approve|accounts${opt}${sep}rm)|grepogram${opt}${sep}leave"
if [[ "$payload" =~ $pattern ]]; then
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"grepogram: this approves research, removes an account or leaves a chat, or prints the command that does; check it matches what you agreed to"}}'
fi
exit 0
