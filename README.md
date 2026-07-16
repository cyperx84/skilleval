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

- **shadow rate** — candidate loses its own queries to incumbents. Am I redundant?
- **hijack rate** — candidate fires on queries belonging to *any* other skill. Do I
  pollute the roster?
- **worst victim rate** — the single worst-hit skill's queries taken by the candidate.
  Do I destroy anyone?

That third one is the gate that matters, because `hijack_rate` divides by every other
skill's queries and so **fades as the roster grows** — backwards for a tool whose whole
claim is that big rosters are where collisions happen. A skill taking 100% of one victim's
triggers scores 0.167 against a 7-skill roster and 0.022 against a 47-skill one, sailing
through the gate exactly where it should be caught. `worst_victim_rate` is roster-size
invariant, and it names the victim.

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
skilleval contend <skill>   shadow / hijack / worst-victim rates vs the real roster
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
firecrawl                              8         0.88
workers-best-practices                 2         0.50
wrangler                               2         0.50
firecrawl-scrape                       7         0.29

top collisions (victim -> thief : count):
  firecrawl -> firecrawl-search : 3
  firecrawl -> firecrawl-scrape : 2
  wrangler -> cloudflare : 1
  workers-best-practices -> cloudflare : 1
```

The umbrella `firecrawl` skill loses **7 of its own 8 trigger queries** to the specialised
siblings installed alongside it — `search the web` goes to `firecrawl-search`, `research a
topic` to `deep-research`. It is installed, and it is nearly unreachable. That collision
was live in a real roster and invisible to every other tool.

Seen from the candidate's side, which is what you get when vetting before install:

```sh
$ skilleval contend firecrawl
{
  "shadow_rate": 0.875,          # loses 7 of its own 8 triggers
  "hijack_rate": 0.004,          # diluted across 47 skills — reads clean
  "worst_victim": "firecrawl-scrape",
  "worst_victim_rate": 0.143,
  "gate": "fail"
}
```

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

Query sets are generated from each skill's own description (`Use when` / `Triggers on` /
`Activates on` clauses, with a sentence fallback) and scored by TF-IDF cosine over the
roster corpus. No live router, no model, so results are deterministic and diffable.

The colon is optional, because real descriptions state the trigger as prose — *"Use this
skill whenever the user wants to search the web, find articles…"*. A subject lead-in
(`the user wants to`, `the user asks to`) is consumed rather than harvested: it is
identical across every skill, so leaving it in the query pollutes every vector with
vocabulary common to the whole roster.

Text is **stemmed** on both sides, so `build scene` and `building scenes` are one trigger.
Unstemmed they are unrelated tokens, and a skill scores badly on *its own* trigger
whenever its prose and its trigger list disagree on grammatical form — handing the query
to whichever sibling merely repeats the shared noun more often. That is a tokenisation
artifact, and the real router has no such blind spot. The same stem is what dedups a
trigger a description states twice.

Negative clauses are excluded — `NOT for:`, `Do NOT use when:`, `SKIP only when:` and
friends. A skill saying "don't use me for video" must not be scored as though it should
win video queries.

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

The proxy cuts the other way too, and it has drawn blood. Because cosine weights term
frequency, a skill that merely **repeats a shared noun** more often than its neighbour can
appear to steal that neighbour's triggers without any semantic overlap at all. Before
v0.3.0 this tool reported that `godot-scene-doctor` (a scene *health checker*) ate 57% of
`godot-scene-builder`'s triggers — purely because "scene" appears more densely in the
doctor's description, and unstemmed tokens stopped the builder from matching its own
`build scene` trigger. Stemming removed that finding entirely. Read a hit as *"these
descriptions compete lexically"*, and confirm it reads as a real collision before acting.

Both `shadow_rate` and `hijack_rate` are fractions of *routable* queries. A skill that
yields no routable queries at all is reported **unscorable** and exits 2 — it is not
reported clean. Same for a candidate with no incumbents to contend against: no data is not
evidence of safety.

## Gates

`contend` and `all` exit non-zero when any of these trip:

| metric | gate | catches |
|---|---|---|
| `shadow_rate` | > 0.3 | the candidate is redundant against what's installed |
| `hijack_rate` | > 0.15 | the candidate pollutes the whole roster |
| `worst_victim_rate` | > 0.3 | the candidate destroys one specific skill |

Exit codes are a contract: **0** clean, **1** a gate failed, **2** could not be scored.

### Thin query sets don't gate

A rate needs at least **5 routable queries** under it to decide an exit code. At 3 queries
the smallest non-zero `worst_victim_rate` is 0.333 — already over the gate — so a single
stolen query would fail the run on quantisation noise rather than evidence.

Below that floor the rate is still computed and reported, and the reason it wasn't gated
is named in `advisory`. Not gating is **not** a clean bill:

```sh
$ skilleval contend cloudflare
{
  "worst_victim": "workers-best-practices",
  "worst_victim_rate": 0.5,          # reported in full
  "gated_victims": [],               # ...but nothing had 5+ queries behind it
  "worst_gated_victim_rate": 0.0,
  "gate": "pass",
  "advisory": [
    "worst_victim_rate 0.5 ('workers-best-practices') reported but not gated: that victim
     has only 2 routable queries (need 5)."
  ]
}
```

The floor applies to *generated* query sets, which are a proxy a terse description
starves. A hand-written `--queries` set is not a proxy — it is the author stating what the
skill must win — so overridden sets are gated however small they are. If a skill is too
terse to score, the fix is a wider description or a `--queries` file, not a lower gate.

When *no* rate clears the floor, the run is unscorable (exit 2) rather than a pass.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Every test builds a throwaway roster and points `SKILLEVAL_ROSTER` at it. Nothing reads or
writes your real skills.

## License

MIT
