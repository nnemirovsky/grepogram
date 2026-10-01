"""Confirmation tokens bound to the exact summary a confirmation was asked with.

``research approve``, ``accounts rm`` and ``leave`` confirm in one of two ways: a code typed back
on the controlling terminal, or — for an agent, behind its own permission prompt — two steps.
The first run prints the summary and a :func:`token`; the same command with ``--confirm <token>``
acts. The token is a short hash of what the command was asked to do and the summary text, so it
is valid only while the summary rebuilt at confirm time is byte-identical: a candidate probed
again, a source added, other items or another chat give another summary and another token, and
the stale one is refused (:func:`problem`).

A token is not a secret and not proof of a human — anyone who runs the first step reads it. What
it guarantees is that a confirmation names exactly the text that was shown; the human gate is
the agent harness's permission prompt on the confirming command, or the MCP client's dialog.
"""

import hashlib
import hmac
import re
from collections.abc import Sequence

TOKEN_LENGTH = 12
"""Hex characters a token keeps of its SHA-256: 48 bits, plenty to tell two summaries apart."""

_SHAPE = re.compile(rf"[0-9a-f]{{{TOKEN_LENGTH}}}")


def token(scope: str, request: Sequence[str], summary: str) -> str:
    """The confirmation token of ``summary`` shown for ``request`` of the command ``scope``.

    ``request`` is the normalized request (a session id and its approval items, an account and a
    target): two commands whose summaries happened to read alike still get different tokens.
    The parts are joined with a unit separator (``\\x1f``).
    """
    material = "\x1f".join([scope, *request, summary])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:TOKEN_LENGTH]


def problem(given: str, expected: str) -> str | None:
    """Why ``given`` does not confirm the summary ``expected`` was made from; ``None`` when it
    does. Surrounding whitespace and letter case are forgiven, nothing else."""
    candidate = given.strip().casefold()
    if not _SHAPE.fullmatch(candidate):
        return (
            f"{given!r} is not a confirmation token: a token is {TOKEN_LENGTH} hex characters, "
            "as the run without --confirm printed it; nothing was changed"
        )
    if not hmac.compare_digest(candidate, expected):
        return (
            "the confirmation token does not match what this command would do now — what it "
            "covers changed since the summary was shown, or the command names something else; "
            "nothing was changed"
        )
    return None
