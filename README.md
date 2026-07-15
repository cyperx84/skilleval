# skilleval

Contention eval for [AgentSkills](https://agentskills.dev) — the eval nobody else runs.

Every other skill-eval tool scores a skill **alone in an empty room**. `run_eval.py` drops
the candidate in a temp dir by itself and fires queries at it. SkillSpector regexes the
description text. Rubric judges read prose.

A skill alone always fires. None of them answer the question that actually bites you at
40 skills installed:

**Does this skill steal triggers from the skills you already have?**

`skilleval` drops the candidate into your **real installed roster** and measures both
directions:

- **hijack rate** — candidate fires on queries that belong to other skills
- **shadow rate** — candidate loses its own queries to incumbents

It collapses the security axis into the same mechanic for free. Description injection
isn't "regex hit on a suspicious phrase" — it's a *measured* hijack rate. A skill whose
description says "use this for ANY file operation" scores malicious behaviourally, with
no pattern list required. Static scanners can't prove behaviour; this can.

And it gives you **roster regression**, which no other tool does: installing skill N
silently breaks skill M's triggering. Run `skilleval roster` after every install to catch
the collision before it ships.

## Install

```sh
brew install cyperx84/tap/skilleval
```

Or just drop it on your PATH — it's one stdlib-only file, no dependencies:

```sh
curl -o ~/.local/bin/skilleval \
  https://raw.githubusercontent.com/cyperx84/skilleval/main/skilleval.py
chmod +x ~/.local/bin/skilleval
```

Requires Python 3.9+. No network calls, no LLM calls, nothing to configure.

## Use

```
skilleval lint <skill>      structural checks (frontmatter, name/dir match, length)
skilleval scan <skill>      injection / overbroad-trigger pattern scan, exit-gates
skilleval contend <skill>   hijack rate + shadow rate against the real roster
skilleval roster            roster-wide shadow-rate matrix — catches install regressions
skilleval judge <skill>     prints delegation instructions for an LLM rubric pass
skilleval all <skill>       lint -> scan -> contend -> judge, gates on fail
```

Stages 1–2 are deterministic and run in milliseconds; they exit non-zero so you can gate
CI on them. Stage 3 is the new piece. Stage 4 deliberately does nothing but tell your
agent what to do — **no LLM runs inside this CLI**, so it stays fast, free, and scriptable
from any harness.

```sh
$ skilleval roster
roster: 47 skills merged from ~/.agents/skills, ~/.openclaw/skills, ~/.claude/skills

skill                            queries  shadow_rate
godot-scene-builder                    7         0.57
workers-best-practices                 2         0.50

top collisions (victim -> thief : count):
  godot-scene-builder -> godot-scene-doctor : 4
  wrangler -> cloudflare : 1
```

`godot-scene-builder` loses 4 of its 7 own trigger queries to `godot-scene-doctor`. That
collision was live in a real roster and invisible to every other tool.

## Roster

Merged and symlink-deduped, in precedence order:

1. `~/.agents/skills` — the cross-harness canonical dir
2. `~/.openclaw/skills`
3. `~/.claude/skills`

Override with `SKILLEVAL_ROSTER` (an `os.pathsep`-separated list) — used by the tests so
they never touch your real skills:

```sh
SKILLEVAL_ROSTER=/tmp/my-roster skilleval roster
```

`roster` also reports two things it finds along the way, which are findings in their own
right rather than noise to swallow:

- **unreadable skills** — no frontmatter means no description, which means the router can
  never trigger them. They are installed and dead.
- **name collisions** — two *different* files claiming one name. One wins; the other is
  invisible. Pass `--strict` to make either condition fail the run.

## Queries

Query sets are generated from each skill's own description (`Use when:` / `Triggers on:`
clauses, with a sentence fallback) and scored by TF-IDF cosine over the roster corpus. No
live router, no model, so results are deterministic and diffable.

Hand-written sets beat generated ones. Supply them with `--queries`:

```json
{ "my-skill": ["scrape this page", "pull the content from example.com"] }
```

```sh
skilleval contend ./my-skill --queries queries.json
```

## What the numbers do and don't prove

Read this before trusting a green result.

Queries are generated from a skill's own description and routed against vectors built from
those same descriptions, so **every skill has home-field advantage on its own queries**. A
skill whose description *is* its query scores 1.0 against itself and cannot be hijacked on
it. Hijacks surface when the victim's description covers several concerns and is therefore
diluted relative to any single one of its triggers.

The bias runs one way, which makes the tool useful but asymmetric:

- A shadow or hijack hit is **strong evidence of a real collision**. Trust it.
- A zero shadow rate is **weak evidence of safety**. It is a floor, not a clean bill.

For the stronger claim, pass held-out query sets via `--queries` so the routing text and
the evaluation text aren't the same string. TF-IDF is also a lexical proxy for a real
model router: it catches vocabulary overlap, not semantic overlap. Two skills that collide
in meaning while sharing no words will read clean here.

## Gates

`contend` and `all` exit non-zero when `shadow_rate > 0.3` or `hijack_rate > 0.15`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Every test builds a throwaway roster and points `SKILLEVAL_ROSTER` at it. Nothing reads or
writes your real skills.

## License

MIT
