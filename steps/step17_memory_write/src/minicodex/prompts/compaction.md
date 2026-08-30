You are compressing the middle of an agent's working transcript so the session
can continue past its context limit. The result replaces the messages below
permanently. Nothing that is not in your output can be recovered.

Write these six sections, in this order, with these exact headings. Write
`(none)` under a heading rather than omitting it -- a missing heading reads as
"this was not discussed", and the reader cannot tell that apart from "this was
lost".

## Goal
What the user asked for, in their terms. **Take this from the `<context>`
block, which holds the opening of the conversation. Do not infer it from the
transcript.** The transcript is the middle of a session: its last few steps look
like the goal and are not. If `<context>` states the task, restate that task.

## Done
Work that is **finished and must not be repeated**. Name the concrete artefact
for each one -- the file that now exists, the command that now passes, the
value that was found. A step with no artefact named is not done.

## Decisions
Choices that were made and are now settled, each with the reason. These are the
ones that get silently re-litigated after compaction if they are not written
down.

## Constraints
Requirements stated by the user, and limits discovered by running things:
versions, paths, things that failed and why, things that must not be touched.

## Open
What is still unfinished, and the immediate next action.

## Key data
Exact strings that would cost a tool call to obtain again: paths, identifiers,
error messages, versions, command output that was hard to get. Quote them.

You are given up to three blocks. Only one of them is being replaced:

- `<context>` -- the opening of the conversation. **It is not being deleted**
  and does not need compressing. It is here so your summary agrees with it.
- `<established>` -- the surviving record of an earlier compaction, if any.
- `<transcript>` -- the messages that are about to be destroyed. This is the
  only thing you are summarising.

Rules:

- Facts only. No advice, no summary of your own reasoning, no "the agent then
  decided to". If it is not something the next turn needs, leave it out.
- Preserve exact strings exactly. A path retyped from memory is a bug.
- If an **established** block appears below, it is the surviving record of an
  earlier compaction. Its content has already outlived the transcript it came
  from. Carry it forward into the sections above, unchanged in meaning and
  unchanged in its exact strings. Do not compress it further and do not drop an
  item because it looks old.
