"""Guessing how big a request is, and then correcting the guess.

Compaction has to fire *before* the request is sent, so it needs a number the
server has not produced yet.  Every local estimate is wrong; what matters is
how wrong, and in which direction.

Measured against gpt-4o-mini on 2026-08-10 (`probe_tokens.py`), comparing
`chars // 4` with the `prompt_tokens` the server reported:

    case              chars     est  actual   est/actual
    prose only           37       9      16       0.56x
    long prose         4200    1050    1008       1.04x
    agent history       158      64     127       0.50x
    shell output       2281     577    1185       0.49x
    json blob          1420     355     807       0.44x
    cjk                 560     140     327       0.43x
    source code        1760     440     767       0.57x

English prose really does run at about 4.2 characters per token.  Nothing else
does: JSON runs at 1.8, CJK at 1.7, `ls -la` output at 1.9.  **There is no
divisor that works**, because the spread between the extremes is 2.4x, and an
agent's history is mostly the dense end -- command output, source code, JSON
arguments.  A tuned constant would be tuned for whatever happened to be in the
sample.

Worse, every error above is in the same direction: the estimate is *low*.  A
low estimate means compaction fires late, and "late" here means the request is
already over the limit.

So this module does three things:

1. counts the inputs `chars // 4` forgets -- the tool schemas, which are re-sent
   on every single turn, and a per-message framing cost;
2. **corrects itself from the number the server already told us**;
3. refuses to guess about content it does not model, rather than returning a
   confidently small number for it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

# The prose figure, used as the starting point.  It is deliberately the
# optimistic end: `Calibration` moves it, and a starting point that is too
# pessimistic wastes the whole window on turn one, before any observation
# exists to correct it.
CHARS_PER_TOKEN = 4.0

# Every message costs a few tokens beyond its content: the role, the
# separators, and whatever framing the server wraps it in.  Measured: a
# 37-character user message reports 16 prompt tokens, where the content alone
# accounts for 9.
PER_MESSAGE_TOKENS = 4


class UncountableContent(RuntimeError):
    """A message this module cannot size, and will not pretend to.

    The multi-modal shape -- `content` as a list of parts rather than a string
    -- is the case that matters.  `len()` of a two-element list is 2, which
    divided by four is 0, so an image would be counted as free.  A budget that
    silently values its largest item at zero is worse than no budget: it fires
    late *and* reports that everything is fine.

    Per-modality estimation is F06-10 and is not implemented here -- an image's
    token cost depends on its dimensions and the provider's tiling rules, and
    this project has no image path to measure against.  Raising is the honest
    version of not having done it.
    """


def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    raise UncountableContent(
        f"cannot size message content of type {type(content).__name__}; "
        "only text is modelled (F06-10). Add a per-modality estimator before "
        "sending this."
    )


def estimate_messages(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] = (),
    *,
    chars_per_token: float = CHARS_PER_TOKEN,
) -> int:
    """The size of one request, in tokens, before it is sent.

    `tools` is not optional in practice.  It was left out of the first version
    on the grounds that it is "not part of the conversation", which is true and
    irrelevant: it is part of every request, it does not shrink when the
    history is compacted, and at 53 tokens for two toy schemas it is the entire
    budget of a short session.  Chapter 9 makes this the dominant term.
    """
    chars = 0
    for message in messages:
        chars += _content_chars(message.get("content"))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            chars += len(function.get("name") or "")
            chars += len(function.get("arguments") or "")
    total = chars / chars_per_token + PER_MESSAGE_TOKENS * len(messages)
    if tools:
        total += len(json.dumps(list(tools))) / chars_per_token
    return int(total)


class Calibration:
    """The correction factor, learned from what the server charged.

    The server reports `prompt_tokens` for the request it just answered.  That
    request is one turn older than the one being sized now, and within a
    session the content mix barely changes, so the ratio carries over.

    Measured over an eight-turn session (`probe_calibration.py`), error against
    the reported figure:

        turn   actual   raw estimate   corrected
           0       77           +62%        +62%
           1      116           +30%        -20%
           2      443           -31%        -47%
           3     1007           -33%         -4%
           4     1299           -36%         -4%
           5     1518           -38%         -4%
           6     1942           -36%         +3%
           7     2690           -34%         +3%

    Two things to read out of that.  The correction converges -- from turn 3 on
    it is within 4%, against a raw estimate stuck at -35%.  And it is useless
    for the first two turns, which is fine, because a two-message history is
    not what overflows a window.

    It is not a general tokeniser and does not try to be.  It is a running
    answer to one question: for *this* conversation, how wrong am I?
    """

    def __init__(self) -> None:
        self._ratio: float | None = None
        self.observations = 0

    @property
    def ratio(self) -> float:
        """1.0 until the server has said something.  Never a guess dressed up
        as a measurement."""
        return self._ratio if self._ratio is not None else 1.0

    @property
    def calibrated(self) -> bool:
        return self._ratio is not None

    def observe(self, *, estimated: int, actual: int) -> None:
        """Record one (guess, truth) pair.

        Guarded rather than trusted: `actual` arrives from a parsed HTTP
        response, and a provider that omits usage sends 0, which would set the
        ratio to 0 and make every future estimate free.
        """
        if estimated <= 0 or actual <= 0:
            return
        self._ratio = actual / estimated
        self.observations += 1

    def correct(self, estimated: int) -> int:
        return int(estimated * self.ratio)

    def describe(self) -> str:
        if not self.calibrated:
            return "uncalibrated (no usage reported yet)"
        return f"x{self.ratio:.2f} from {self.observations} observation(s)"
