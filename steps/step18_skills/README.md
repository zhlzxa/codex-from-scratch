# minicodex — chapter 18: skills (a name, one sentence and a path; the rest on demand)

A skill is a directory with a `SKILL.md` in it: two frontmatter fields and a
markdown body. Only the **name, the one-sentence description and the path** go
into the request; the body is read by the model, when the model decides a task
matches, by opening that path. codex calls this *progressive disclosure*.

For the two real skills in `skills.example/`, that is **104 tokens instead of
143** on every request of every turn — and the gap widens with every skill
added, because the catalog is one line each and capped, while the bodies are
not: at 50 skills the uncapped catalog would be 2185 tokens and what actually
ships is 411.

```bash
uv sync --all-extras
uv run pytest                                   # 1719 tests, all offline
uv run python probe_mutations_ch18.py           # 32 mutations
uv run python probe_skills.py cost              # offline, F18-05

uv run minicodex ask "how do I run the linter here?" \
    --skills --skills-dir skills.example        # off unless you ask for it

uv run python probe_skills.py reach --samples 6 # network: OPENAI_API_KEY, a few cents
```

## This chapter was corrected after chapter 16's rewrite

Chapter 16 was rebuilt against codex's real memory read path, and one of its
findings (F16-12) turned out to apply here too. This chapter had read codex's
source and got the mechanism right; it still missed one sentence in the file it
was reading from.

| | was | now |
|---|---|---|
| reaching a skill body | `read_skill`, the only way | `read_file` on the path the catalog prints (`ext/skills/src/catalog_prompt.rs:7`) |
| catalog line | `- name: description` | `- name: description (file: /abs/path/SKILL.md)` (`ext/skills/src/render.rs:244`) |
| delivery | `Wiring.agent(preamble=...)`, a system note | `SkillsWatcher` on `on_turn_start`, `role: "developer"` (`ext/skills/src/fragments.rs:39-41`) |
| `read_skill` | the mechanism | opt-in `--skill-tool`, kept as a demonstration |

codex's `skills.list`/`skills.read` do exist — for `environment resource` and
`orchestrator resource` entries (`ext/skills/src/catalog_prompt.rs:3`), skills
whose bodies are not on a filesystem at all. Every skill this program can
discover is a `file` entry, and codex's own instruction for those is *"open the
listed path"*.

## The headline: five arms, and the tool lost its reason to exist

Two tasks whose correct behaviour is written **only** inside a skill body, two
runs pooled (8 + 12 = 20 per arm), `gpt-4o-mini`:

| arm | followed the skill | body fetched | turns (avg) |
|---|---|---|---|
| off | 0/20 | — | 2.0 |
| catalog only | 0/20 | 40, every one refused | 6.5 |
| **catalog + `read_file`** (ships) | **18/20** | **20/20** | 4.2 |
| catalog + `read_skill` (`--skill-tool`) | 17/20 | 20/20 | 4.8 |
| every body resident | 11/20 | — | 3.0 |

- **18 against 17 is not a difference.** The first run had it 8/8 against 6/8;
  the second reversed it, 10/12 against 11/12. That is a coin flipped eight
  times. `read_skill` was kept in the first version of this chapter *because it
  measured better* — against a catalog with no read path at all, which is not
  the choice anyone faces. Against the real alternative it ties, and the tie
  goes to what codex ships.
- **Chapter 16's F16-09 still does not reproduce.** Both arms fetch the body in
  20 runs out of 20.
- **The catalog-only arm makes 40 refused `read_file` calls.** The model reaches
  for the printed path in every run, unprompted. What costs it the arm is not
  unwillingness, it is not being *allowed* — the skills directory is outside the
  workspace and that arm gets no `extra_read_roots`. The behaviour progressive
  disclosure depends on is already there; the plumbing is what has to exist.
- **The "upper bound" arm is the worst one.** Every body resident scores 11/20,
  below both fetch arms, consistently across both runs (4/8, then 7/12) — while
  being the cheapest in turns. A model handed the instruction for free follows
  it *less* often than one that had to go and get it. That inverts the framing
  the arm was built for and is not something this chapter set out to find.
- The model does **not** invent the body from the description: the catalog-only
  arm never produced a marker it had not been shown.

## What was measured, and what broke while measuring it

Three of this chapter's faults are in its own measuring tools:

1. The compliance metric asked the trajectory for a tool called `shell`; the
   tool is `run_shell`. All 24 runs scored "did not follow the skill" —
   **including the arm where the instruction was fully resident**, which is the
   arm that cannot fail. The tell was a 0 in the control.
2. The release fixture asked for a note in a file that did not exist. The model
   read the skill, wrote the correct line, and then tried to deliver it as an
   `apply_patch` edit against an empty target — chapter 4's tool cannot create
   files. The probe was about to report that as "the model ignored the skill".
3. One run gave 4/6 against 6/6 with a tidy explanation (the extra round trip
   ate the turn budget). The next gave 5/6 against 4/6 with nothing near the
   turn limit. Six samples cannot tell those apart; the explanation was fitted
   to noise. **The same trap caught the F18-14 re-measurement**, which is why
   the numbers above are pooled over two runs rather than reported from the
   first one — where the gap looked like 8/8 against 6/8 and meant nothing.

The mutation run found six real holes in a suite that had been green on the
first try. The one worth naming: `test_..._a_path_shaped_name_cannot_read_a_
different_file` passed against an implementation that **did** rebuild
`directory / name / SKILL.md` and open it — because the rebuilt path did not
happen to exist. A security test that passes against the insecure version is
not testing anything.

## Deliberately not done

- **One directory, not five.** codex discovers skills across a project root,
  a user root, a bundled system cache, an admin root and any plugin roots
  (`ext/skills/src/host_roots.rs:87-145`), merging them by scope. The hard part
  is not the walk, it is collisions across layers and being able to say which
  layer a skill came from. That is a chapter of its own.
- **No `$name` mention sigil.** codex parses `$SkillName` and
  `[$SkillName](skill://…)` out of the user's message
  (`skills/src/mentions.rs:41,57-60,81-146`) and injects that skill's body for
  the turn, with no round trip (`ext/skills/src/extension.rs:440-498`). The
  4.2-against-3.0 turn difference above is roughly what that buys.
- **The catalog is delivered once, not re-rendered per turn.** codex really does
  rebuild it every turn (`ext/skills/src/extension.rs:342-435`), because what it
  rebuilds is marker-delimited and *replaces* the previous copy
  (`ext/skills/src/fragments.rs:43-49`). This program's `History` is append-only
  by chapter 7's design and has no replace operation, so "every turn" here would
  mean N copies by turn N. See `SkillsWatcher`.
- **No recursion.** One level of subdirectory. `MAX_SKILLS = 200` is the same
  argument as codex's `MAX_SCAN_DEPTH = 6` / `MAX_SKILLS_DIRS_PER_ROOT = 2000`
  (`core-skills/src/loader.rs:109-110`), at a smaller scale.
- **Not a YAML parser.** Two fields, both one-line strings. codex's repairs
  malformed scalars (`skills/src/parser.rs:98-181`) because a real `SKILL.md`
  nests lists under `metadata`; anything that is not the shape above is skipped
  here rather than guessed at.
- **The skills directory did not move to `~/.minicodex/` when memory did.**
  codex keeps them apart too: its skill roots do not include the memory root,
  and a `skills/<name>/SKILL.md` written by memory consolidation is an ordinary
  file in the memory store, never in the skill catalog.
- **The skill set is read once, at startup.** A directory that changed
  mid-session would make two turns of one conversation disagree about which
  skills exist, with nothing in the transcript explaining why.
- **No second provider.** `gpt-4o-mini` only, as in chapters 9–17.
