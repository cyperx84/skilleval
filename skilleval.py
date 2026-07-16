#!/usr/bin/env python3
"""
skilleval — contention eval for AgentSkills.

Every other skill-eval tool (SkillSpector, run_eval.py, rubric judges) scores a
skill alone in an empty room. A skill alone always fires. This one drops the
candidate into the real installed roster and measures whether it steals triggers
from — or loses triggers to — the skills already there.

Pipeline: lint -> scan -> contend -> judge
  lint    deterministic structural checks (frontmatter, name/dir match, length)
  scan    deterministic injection/overbroad-trigger pattern scan, exit-gates
  contend the new piece: hijack rate + shadow rate against the real roster
  judge   LLM rubric pass — not run inline (no LLM inside this CLI); delegates
          to the `skill-eval` skill for a human-facing score

No network calls, no LLM calls, stdlib only. Deterministic in, deterministic out.

Contention metrics, all fractions of *routable* queries:
  shadow_rate       my queries won by someone else — am I redundant?
  hijack_rate       all other skills' queries won by me — do I pollute the roster?
  worst_victim_rate the single worst-hit skill's queries won by me — do I destroy
                    anyone? hijack_rate divides by the whole roster, so it fades
                    as the roster grows; this one does not. It is the gate that
                    matters when vetting one candidate against many incumbents.

Metric honesty: queries are generated from each skill's own description and
routed against vectors built from those same descriptions, so every skill has
home-field advantage on its own queries. That biases shadow_rate LOW. A shadow
hit is therefore strong evidence of a real collision; a zero shadow_rate is
weak evidence of safety. Use --queries to supply held-out query sets when you
need the stronger claim.
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT_ROSTER_DIRS = [
    Path.home() / ".agents" / "skills",
    Path.home() / ".openclaw" / "skills",
    Path.home() / ".claude" / "skills",
]

# Closing delimiter may be followed by a newline or EOF.
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
KEY_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*):\s*(.*)$")
BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?\d*\s*$")

STOPWORDS = set(
    """a an the of to for and or use uses using used when this that these those
    your you it its is are was were be been being with without on in at from into over under
    any all each other than then also do does did not no so if it's can may will would should
    whenever whatever s""".split()
)

INJECTION_PATTERNS = [
    (r"\bany\s+(file|task|request|question)\b", "overbroad trigger claim: 'any X'"),
    (r"\balways\s+use\s+this\b", "overbroad trigger claim: 'always use this'"),
    (r"ignore\s+(previous|prior|other)\s+instructions", "prompt-injection phrasing"),
    (r"do\s+not\s+tell\s+the\s+user", "hidden-behavior instruction"),
    (r"(password|api[_-]?key|secret|token)\b.{0,30}(send|post|exfiltrat|upload)", "credential exfiltration pattern"),
    (r"curl\s+[^\n]*\|\s*(sh|bash|zsh)", "pipe-to-shell pattern"),
    (r"\bexfiltrat", "explicit exfiltration language"),
    (r"\bbypass\s+(permission|approval|safety|sandbox)", "safety-bypass language"),
    (r"\bfor\s+(any|every)\s+(coding|programming|dev)\s+task\b", "overbroad trigger claim: catch-all dev task"),
]

# Query-extraction markers, matched case-insensitively.
#
# The colon is optional for every marker but "example", because real skills
# overwhelmingly write the trigger clause as prose: "Use this skill whenever the
# user wants to search the web, find articles, ...". Requiring the colon missed
# 37 of 47 skills on a real roster, dropping them to the sentence fallback,
# which caps at 20 words and so harvested *one* query from a 734-char
# description. That starvation is what makes a per-victim rate swing on a single
# query, so it is fixed at the source rather than by tuning the gate.
# "example" keeps its colon: unanchored, the bare word matches in open prose.
#
# The optional "the user wants to" tail is consumed, not matched, so the
# harvested fragment starts at the verb ("search the web") rather than carrying
# a subject clause that is identical across every skill and would dominate the
# TF-IDF vector with noise common to the whole roster.
QUERY_MARKER_RE = re.compile(
    r"(?:"
    # '(?:\s*:)?' not '\s*:?': the latter's \s* eats the space before "the user"
    # even when no colon follows, so the tail below can never match its \s+.
    r"(?:triggers?\s+on|activates?\s+on|also\s+use\s+when"
    r"|use\s+(?:this\s+skill\s+)?when(?:ever)?)(?:\s*:)?"
    r"|examples?\s*:"
    r")"
    r"(?:\s+(?:a|the)\s+user\s+"
    r"(?:wants\s+to|asks\s+to|asks|says|provides|needs\s+to|needs|mentions)"
    r")?",
    re.IGNORECASE,
)
# Ends the fragment harvested from a positive marker. Must cover every phrasing
# NEGATED_MARKER_RE knows: skipping a negated marker is not enough on its own,
# because the *previous* positive marker's fragment runs on into the negation.
NEGATIVE_CLAUSE_RE = re.compile(
    r"NOT\s+for:|Do\s+NOT|Never\s+use\b|Don'?t\s+use\b|SKIP\s+only|SKIP\b", re.IGNORECASE
)
# "Do NOT use when: X" contains a positive marker. Harvesting it would score the
# skill as if it should win the queries it explicitly disclaims.
NEGATED_MARKER_RE = re.compile(r"\b(?:not|never|don'?t|avoid|skip|except)\b[\s\w'-]{0,12}$", re.IGNORECASE)

SHADOW_GATE = 0.3
HIJACK_GATE = 0.15
# Per-victim gate. hijack_rate divides by *every* other skill's queries, so it
# shrinks as the roster grows — a skill that steals 100% of one victim's
# triggers scores 0.167 against a 7-skill roster and 0.022 against a 47-skill
# one. Diluting the signal with roster size is backwards for a tool whose whole
# claim is that big rosters are where collisions happen. This metric is the
# worst single victim's loss, which no roster size can wash out.
VICTIM_GATE = 0.3
# A rate over a handful of queries cannot be gated on: at 3 queries the smallest
# non-zero worst_victim_rate is 0.333, which clears VICTIM_GATE on a *single*
# stolen query, so the gate fires on quantisation noise rather than on evidence.
# Below this many routable queries the rate is still reported — it is a lead —
# but it does not decide the exit code. Fixing the query generator (see
# QUERY_MARKER_RE) is what actually shrinks the set this applies to; the guard
# is the floor for descriptions too terse to harvest either way.
MIN_GATE_QUERIES = 5


class SkillError(Exception):
    """Unusable input — a parse failure or an unscorable skill."""


def roster_dirs():
    override = os.environ.get("SKILLEVAL_ROSTER")
    if override:
        return [Path(p).expanduser() for p in override.split(os.pathsep) if p]
    return list(DEFAULT_ROSTER_DIRS)


def tokenize(text):
    """Stemmed, so a trigger matches its own morphology.

    Descriptions and queries disagree on surface form constantly — a skill says
    "Use when building scenes" and lists "Triggers on: build scene". Unstemmed,
    'building' and 'build' are unrelated tokens, so a skill scores *poorly on its
    own trigger* and a sibling sharing the noun wins it. That is a tokenisation
    artifact reported as a collision, and the real router (an LLM) has no such
    blind spot. Stemming here also makes this the single definition of "same
    words" for both routing and query dedup.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [_stem(w) for w in words if w not in STOPWORDS and len(w) > 1]


def _find_close_quote(s, q):
    """Index of the closing quote, honouring YAML escaping ('' in single, \\" in double)."""
    i = 1
    while i < len(s):
        if s[i] == q:
            if q == "'" and s[i + 1:i + 2] == "'":
                i += 2
                continue
            if q == '"' and s[i - 1] == "\\":
                i += 1
                continue
            return i
        i += 1
    return -1


def _unquote(val):
    """Unwrap a quoted scalar, or strip the trailing comment from a plain one.

    ` #` opens a comment in a plain YAML scalar, so `name: cmt # note` is the
    name `cmt`. Keeping the comment produced a roster key nothing could match.
    Inside quotes a `#` is literal, so quoted scalars are unwrapped first.
    """
    val = val.strip()
    q = val[:1]
    if q in ('"', "'"):
        end = _find_close_quote(val, q)
        if end != -1:
            body = val[1:end]
            return (body.replace("''", "'") if q == "'" else body.replace('\\"', '"')).strip()
        return val
    return re.sub(r"(?:(?<=\s)|^)#[^\n]*", "", val).strip()


def _dedent(block):
    body = [l for l in block if l.strip()]
    if not body:
        return []
    indent = min(len(l) - len(l.lstrip()) for l in body)
    return [l[indent:] if len(l) >= indent else l.strip() for l in block]


def parse_frontmatter(fm_text):
    """Parse the YAML subset AgentSkills frontmatter actually uses.

    Not a general YAML parser, but it must handle what real skills ship: flat
    scalars, quoted scalars, plain scalars folded over several lines, block
    scalars ('|' literal, '>' folded), and sequences. Roughly a fifth of a real
    roster uses block scalars for `description`; rejecting them would blind the
    eval to the skills most likely to collide.
    """
    lines = fm_text.split("\n")
    data = {}
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = KEY_RE.match(line)  # anchored at column 0, so nested keys are not top-level
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()
        i += 1
        block = []
        while i < n and not (lines[i].strip() and KEY_RE.match(lines[i])):
            block.append(lines[i])
            i += 1

        if BLOCK_SCALAR_RE.match(rest):
            content = _dedent(block)
            if rest[0] == "|":
                data[key] = "\n".join(content).strip("\n")
            else:  # '>' folds newlines into spaces
                data[key] = " ".join(l.strip() for l in content if l.strip())
            continue

        items = [l.strip()[2:].strip() for l in block if l.strip().startswith("- ")]
        if not rest and items:
            data[key] = [_unquote(x) for x in items]
            continue

        # Plain or quoted scalar, possibly folded across continuation lines.
        parts = [rest] + [l.strip() for l in block if l.strip()]
        data[key] = _unquote(" ".join(p for p in parts if p))
    return data


def load_skill_file(path_str):
    p = Path(path_str).expanduser()
    if p.is_dir():
        p = p / "SKILL.md"
    if not p.exists():
        return None
    text = p.read_text(errors="ignore")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise SkillError(f"{p}: no YAML frontmatter block found")
    data = parse_frontmatter(m.group(1))
    for key in ("name", "description"):
        if key in data and not isinstance(data[key], str):
            raise SkillError(f"{p}: frontmatter '{key}' must be a string, got {type(data[key]).__name__}")
    return {
        # Routing needs *some* key, so an absent name falls back to the directory.
        # `declared_name` keeps the distinction lint depends on: with only `name`,
        # a skill missing the field entirely looked identical to a correct one.
        "name": data.get("name") or p.parent.name,
        "declared_name": data.get("name") or "",
        "desc": data.get("description", ""),
        "path": str(p),
        "real_path": str(_resolve(p)),
        "body": text[m.end():],
        "raw_frontmatter": data,
        "dir_name": p.parent.name,
    }


def _resolve(p):
    try:
        return p.resolve()
    except OSError:
        return p


def discover_skills(strict=False, report=None):
    """Roster keyed by skill name, deduped by real path (symlink farms are common).

    Two *distinct* files claiming one name is a real problem: one of them is
    invisible to the router, and keying by name would drop it silently. First
    one wins (roster_dirs order is precedence), but it is always reported rather
    than swallowed. Skills that fail to parse are reported too — a skill the
    router cannot read is a finding, not a rounding error.
    """
    seen_real = set()
    skills = {}
    origins = {}
    collisions = []
    unparsable = []
    for base in roster_dirs():
        if not base.exists():
            continue
        for entry in sorted(base.iterdir()):
            md = entry / "SKILL.md"
            if not md.exists():
                continue
            real = str(_resolve(md))
            if real in seen_real:
                continue
            seen_real.add(real)
            try:
                sk = load_skill_file(md)
            except SkillError as e:
                unparsable.append(str(e))
                continue
            if not sk:
                continue
            name = sk["name"]
            if name in skills:
                collisions.append({"name": name, "kept": origins[name], "ignored": real})
                continue
            origins[name] = real
            skills[name] = sk

    if report is not None:
        report["collisions"] = collisions
        report["unparsable"] = unparsable
    for c in collisions:
        print(f"warning: name collision '{c['name']}': using {c['kept']}, ignoring {c['ignored']}",
              file=sys.stderr)
    for u in unparsable:
        print(f"warning: skill unreadable by the router (excluded): {u}", file=sys.stderr)
    if strict and (collisions or unparsable):
        raise SkillError("--strict: roster has name collisions or unparsable skills (see warnings)")
    return skills


def build_idf(skills):
    df = Counter()
    for s in skills.values():
        for t in set(tokenize(s["desc"])):
            df[t] += 1
    n = max(len(skills), 1)
    return {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}


def vectorize(tokens, idf):
    tf = Counter(tokens)
    vec = {t: tf[t] * idf.get(t, 1.0) for t in tf}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return vec, norm


def cosine(vec_a, norm_a, vec_b, norm_b):
    if not norm_a or not norm_b:
        return 0.0
    dot = sum(v * vec_b.get(k, 0.0) for k, v in vec_a.items())
    return dot / (norm_a * norm_b)


def build_router(skills):
    """Return route(query) -> ranked [(score, name)], or [] when unroutable.

    A query with no in-vocabulary tokens, or one that scores 0.0 against every
    skill, has no winner. Returning the alphabetically-first skill for those
    would manufacture phantom shadow/hijack hits.
    """
    idf = build_idf(skills)
    vectors = {name: vectorize(tokenize(s["desc"]), idf) for name, s in skills.items()}
    cache = {}

    def route(query):
        if query in cache:
            return cache[query]
        vec, norm = vectorize(tokenize(query), idf)
        if not vec:
            cache[query] = []
            return []
        scores = [(cosine(vec, norm, v2, n2), name) for name, (v2, n2) in vectors.items()]
        scores.sort(key=lambda x: (-x[0], x[1]))
        result = [] if not scores or scores[0][0] <= 0.0 else scores
        cache[query] = result
        return result

    return route


def _stem(w):
    """Crude suffix-stripper, enough to make 'building scenes' == 'build scene'.

    Not linguistics: the only job is collapsing the gerund/plural pair that one
    description states both ways ("Use when building scenes... Triggers on: build
    scene"). Over-collapsing two genuinely different triggers costs one query;
    under-collapsing double-counts one trigger in the denominator of every rate.

    Order matters and is load-bearing: the plural must come off before the
    gerund. Stripping '-ing' first makes 'settings' -> 'setting' (the '-ing'
    branch never fires, the word ends in 's') while bare 'setting' -> 'sett',
    so a word and its own plural stem apart — the exact collision this is meant
    to catch, inverted.
    """
    w = w.lower()
    if w.endswith("es") and len(w) > 3:
        w = w[:-2]
    elif w.endswith("s") and len(w) > 3:
        w = w[:-1]
    if w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    return w


def _dedup_key(q):
    return " ".join(_stem(w) for w in re.findall(r"[\w'-]+", q.lower()))


def generate_queries(desc, max_q=8):
    queries = []
    # A marker's fragment ends at the next marker, not at the end of the string.
    # Descriptions chain clauses ("Use when building scenes... Triggers on: build
    # scene"), and an unbounded fragment swallows the following marker's literal
    # text into a query. The next marker's own fragment is harvested on its own
    # iteration, so nothing is lost by stopping here. Negated markers still bound
    # the fragment even though they are not harvested themselves.
    marks = list(QUERY_MARKER_RE.finditer(desc))
    for i, m in enumerate(marks):
        if NEGATED_MARKER_RE.search(desc[max(0, m.start() - 24):m.start()]):
            continue
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(desc)
        frag = desc[m.end():stop]
        frag = NEGATIVE_CLAUSE_RE.split(frag)[0]
        # Newlines are clause boundaries too — literal block scalars ('|') keep
        # them, and without this a whole block collapses into one junk query.
        for part in re.split(r"[,;\n]|\bor\b", frag, flags=re.IGNORECASE):
            part = part.strip(" .\"'()")
            if 2 <= len(part.split()) <= 14:
                queries.append(part)
    if not queries:
        for s in re.split(r"(?<=[.!?])\s+", desc):
            s = s.strip()
            if NEGATIVE_CLAUSE_RE.search(s):
                continue
            if 3 <= len(s.split()) <= 20:
                queries.append(s)
    out = []
    seen = set()
    for q in queries:
        key = _dedup_key(q)
        if key not in seen:
            seen.add(key)
            out.append(q)
    return out[:max_q]


def load_query_overrides(path, known=None):
    """{"skill-name": ["query", ...]} — hand-written sets beat generated ones.

    Validated up front: a bare string silently became a set of character-queries,
    and a typo'd skill name silently applied to nothing, so an override file that
    did nothing at all looked exactly like one that worked.
    """
    if not path:
        return {}
    p = Path(path).expanduser()
    try:
        data = json.loads(p.read_text())
    except OSError as e:
        raise SkillError(f"{path}: cannot read query file: {e}")
    except json.JSONDecodeError as e:
        raise SkillError(f"{path}: invalid JSON: {e}")
    if not isinstance(data, dict):
        raise SkillError(f"{path}: expected a JSON object mapping skill name -> [queries]")
    for name, qs in data.items():
        if not isinstance(qs, list) or not all(isinstance(q, str) and q.strip() for q in qs):
            raise SkillError(f"{path}: '{name}' must be a list of non-empty strings, got {qs!r}")
    if known is not None:
        for name in sorted(set(data) - set(known)):
            print(f"warning: --queries entry '{name}' matches no skill in the roster (ignored)",
                  file=sys.stderr)
    return data


def build_query_sets(skills, overrides):
    out = {}
    for name, s in skills.items():
        out[name] = list(overrides.get(name) or generate_queries(s["desc"]))
    return out


def resolve_target(skill_arg, skills):
    """Resolve to a roster name. A path always wins over a same-named incumbent.

    Vetting a candidate means scoring *that file*. If its name collides with an
    installed skill, scoring the incumbent instead would silently green-light
    the candidate — the exact case the tool exists to catch.
    """
    notes = []
    as_path = Path(skill_arg).expanduser()
    if as_path.exists():
        loaded = load_skill_file(skill_arg)
        if not loaded:
            return None, notes
        name = loaded["name"]
        incumbent = skills.get(name)
        if incumbent and incumbent["real_path"] != loaded["real_path"]:
            notes.append(
                f"candidate '{name}' shadows installed skill at {incumbent['path']}; "
                "scoring the candidate as an in-place replacement"
            )
        skills[name] = loaded
        return name, notes
    if skill_arg in skills:
        return skill_arg, notes
    return None, notes


def check_lint(sk):
    findings = []
    declared = sk.get("declared_name", "")
    if not declared:
        findings.append(("fail", "missing name in frontmatter"))
    elif declared != sk["dir_name"]:
        findings.append(("warn", f"name '{declared}' != directory '{sk['dir_name']}'"))
    if not sk["desc"]:
        findings.append(("fail", "missing description in frontmatter"))
    else:
        dlen = len(sk["desc"])
        if dlen < 40:
            findings.append(("warn", f"description very short ({dlen} chars) — likely won't trigger reliably"))
        if dlen > 1400:
            findings.append(("warn", f"description very long ({dlen} chars) — trim for token cost"))
        if not re.search(r"\buse\s+when\b|\btriggers?\s+on\b|\bwhenever\b", sk["desc"], re.IGNORECASE):
            findings.append(("warn", "no explicit trigger language ('Use when' / 'Triggers on') found"))
    if not sk["body"].strip():
        findings.append(("fail", "empty body after frontmatter"))
    if re.search(r"\bTODO\b|\bFIXME\b|\bplaceholder\b", sk["body"], re.IGNORECASE):
        findings.append(("warn", "TODO/FIXME/placeholder left in body"))
    return {
        "skill": sk["name"],
        "path": sk["path"],
        "findings": [{"level": lvl, "msg": m} for lvl, m in findings],
        "gate": "fail" if any(l == "fail" for l, _ in findings) else "pass",
    }


def check_scan(sk):
    text = sk["desc"] + "\n" + sk["body"]
    hits = []
    for pat, label in INJECTION_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            hits.append({"pattern": label, "match": m.group(0)[:80]})
    return {"skill": sk["name"], "hits": hits, "gate": "fail" if hits else "pass"}


def check_contend(target, skills, overrides=None):
    route = build_router(skills)
    overrides = overrides or {}
    query_sets = build_query_sets(skills, overrides)
    # MIN_GATE_QUERIES guards against a *generated* query set being too thin to
    # trust — it is a proxy for the trigger surface, and a terse description
    # starves it. A hand-written override is not a proxy: it is the author
    # stating "these are the queries this skill must win". One of those losing is
    # evidence, not noise, so overridden sets are gated however small they are.
    exempt = {n for n, qs in overrides.items() if qs and n in query_sets}

    own = query_sets[target]
    if not own:
        raise SkillError(
            f"'{target}': no trigger queries could be generated from its description — "
            "unscorable, not clean. Add 'Use when:' / 'Triggers on:' clauses, or supply "
            "--queries with a hand-written set."
        )

    shadow_hits, own_unroutable = [], []
    for q in own:
        ranked = route(q)
        if not ranked:
            own_unroutable.append(q)
            continue
        if ranked[0][1] != target:
            shadow_hits.append({"query": q, "stolen_by": ranked[0][1], "score": round(ranked[0][0], 3)})
    own_scored = len(own) - len(own_unroutable)
    if own_scored == 0:
        raise SkillError(
            f"'{target}': every generated query was unroutable (no shared vocabulary with any "
            "skill description) — unscorable, not clean."
        )
    shadow_rate = len(shadow_hits) / own_scored

    hijack_hits, other_unroutable = [], 0
    victim_totals, victim_stolen = Counter(), Counter()
    for name, qs in query_sets.items():
        if name == target:
            continue
        for q in qs:
            ranked = route(q)
            if not ranked:
                other_unroutable += 1
                continue
            victim_totals[name] += 1
            if ranked[0][1] == target:
                victim_stolen[name] += 1
                hijack_hits.append({"query": q, "victim": name, "score": round(ranked[0][0], 3)})
    other_scored = sum(victim_totals.values())
    if other_scored == 0:
        raise SkillError(
            f"'{target}': no incumbent had a routable query to contend for "
            f"(roster of {len(skills)}) — unscorable, not clean. A skill cannot be "
            "shown safe against a roster it was never measured against."
        )
    hijack_rate = round(len(hijack_hits) / other_scored, 3)

    victim_rates = {v: round(victim_stolen[v] / victim_totals[v], 3) for v in sorted(victim_stolen)}
    worst_victim, worst_victim_rate = "", 0.0
    if victim_rates:
        worst_victim, worst_victim_rate = sorted(victim_rates.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    # Gate only on rates with enough queries under them to mean anything. The
    # rates above stay in the report either way — suppressing the gate is not the
    # same as calling the skill clean, so anything suppressed is named in
    # 'advisory' rather than dropped.
    advisory = []
    shadow_gateable = own_scored >= MIN_GATE_QUERIES or target in exempt
    if not shadow_gateable:
        advisory.append(
            f"shadow_rate {round(shadow_rate, 3)} reported but not gated: '{target}' has "
            f"{own_scored} routable quer{'y' if own_scored == 1 else 'ies'} "
            f"(need {MIN_GATE_QUERIES}). Widen its description or supply --queries."
        )
    # hijack_rate needs the same floor. Its denominator is every other skill's
    # queries, so it only goes thin on a small roster — but there it gates on one
    # or two queries, which is the quantisation noise this guard exists to stop.
    hijack_gateable = other_scored >= MIN_GATE_QUERIES or bool(exempt - {target})
    if not hijack_gateable:
        advisory.append(
            f"hijack_rate {hijack_rate} reported but not gated: the roster offered only "
            f"{other_scored} routable quer{'y' if other_scored == 1 else 'ies'} to contend "
            f"for (need {MIN_GATE_QUERIES})."
        )
    gateable_victims = {
        v: r for v, r in victim_rates.items()
        if victim_totals[v] >= MIN_GATE_QUERIES or v in exempt
    }
    worst_gated_victim, worst_gated_rate = "", 0.0
    if gateable_victims:
        worst_gated_victim, worst_gated_rate = sorted(
            gateable_victims.items(), key=lambda kv: (-kv[1], kv[0])
        )[0]
    if worst_victim and worst_victim not in gateable_victims:
        advisory.append(
            f"worst_victim_rate {worst_victim_rate} ('{worst_victim}') reported but not gated: "
            f"that victim has only {victim_totals[worst_victim]} routable "
            f"quer{'y' if victim_totals[worst_victim] == 1 else 'ies'} (need {MIN_GATE_QUERIES})."
        )
    if not shadow_gateable and not hijack_gateable and not gateable_victims:
        raise SkillError(
            f"'{target}': no rate had at least {MIN_GATE_QUERIES} routable queries behind it "
            f"(own: {own_scored}, roster: {other_scored}, best victim: "
            f"{max(victim_totals.values(), default=0)}) — unscorable, not clean."
        )

    return {
        "target": target,
        "roster_size": len(skills),
        "own_query_count": len(own),
        "own_unroutable": own_unroutable,
        "shadow_rate": round(shadow_rate, 3),
        "shadow_hits": shadow_hits,
        "other_query_count": other_scored,
        "other_unroutable": other_unroutable,
        "hijack_rate": hijack_rate,
        "worst_victim": worst_victim,
        "worst_victim_rate": worst_victim_rate,
        "victim_rates": victim_rates,
        "min_gate_queries": MIN_GATE_QUERIES,
        "gated_victims": sorted(gateable_victims),
        "worst_gated_victim": worst_gated_victim,
        "worst_gated_victim_rate": worst_gated_rate,
        "advisory": advisory,
        "hijack_hits": sorted(hijack_hits, key=lambda h: (-h["score"], h["victim"], h["query"])),
        "gate": "fail" if ((shadow_gateable and round(shadow_rate, 3) > SHADOW_GATE)
                           or (hijack_gateable and hijack_rate > HIJACK_GATE)
                           or worst_gated_rate > VICTIM_GATE) else "pass",
    }


def _load_or_die(skill_arg):
    sk = load_skill_file(skill_arg)
    if not sk:
        raise SkillError(f"no SKILL.md found at {skill_arg}")
    return sk


def cmd_lint(args):
    result = check_lint(_load_or_die(args.skill))
    print(json.dumps(result, indent=2))
    return 1 if result["gate"] == "fail" else 0


def cmd_scan(args):
    result = check_scan(_load_or_die(args.skill))
    print(json.dumps(result, indent=2))
    return 1 if result["gate"] == "fail" else 0


def cmd_contend(args):
    skills = discover_skills(strict=args.strict)
    target, notes = resolve_target(args.skill, skills)
    if not target:
        raise SkillError(f"skill not found: {args.skill}")
    result = check_contend(target, skills, load_query_overrides(args.queries, skills))
    result["notes"] = notes
    print(json.dumps(result, indent=2))
    return 1 if result["gate"] == "fail" else 0


def cmd_roster(args):
    report = {}
    skills = discover_skills(strict=args.strict, report=report)
    route = build_router(skills)
    query_sets = build_query_sets(skills, load_query_overrides(args.queries, skills))

    matrix, summary, unscorable = {}, {}, []
    for name, qs in query_sets.items():
        scored = 0
        shadow = 0
        for q in qs:
            ranked = route(q)
            if not ranked:
                continue
            scored += 1
            top = ranked[0][1]
            if top != name:
                shadow += 1
                matrix.setdefault(name, Counter())[top] += 1
        if scored == 0:
            unscorable.append(name)
            continue
        summary[name] = {"queries": scored, "shadow_rate": round(shadow / scored, 3)}

    if args.json:
        print(json.dumps({
            "roster_size": len(skills),
            "summary": summary,
            "unscorable": unscorable,
            "name_collisions": report.get("collisions", []),
            "unparsable": report.get("unparsable", []),
            "shadow_collisions": [
                {"victim": v, "thief": t, "count": c}
                for v, thieves in matrix.items() for t, c in thieves.items()
            ],
        }, indent=2))
        return 0

    worst = sorted(summary.items(), key=lambda kv: -kv[1]["shadow_rate"])
    print(f"roster: {len(skills)} skills merged from {', '.join(str(d) for d in roster_dirs())}\n")
    print(f"{'skill':<32} {'queries':>7} {'shadow_rate':>12}")
    for name, info in worst:
        if info["shadow_rate"] == 0.0 and not args.all:
            continue
        print(f"{name:<32} {info['queries']:>7} {info['shadow_rate']:>12.2f}")
    print("\ntop collisions (victim -> thief : count):")
    pairs = sorted(
        ((c, v, t) for v, thieves in matrix.items() for t, c in thieves.items()),
        reverse=True,
    )
    for count, victim, thief in pairs[:25]:
        print(f"  {victim} -> {thief} : {count}")
    if not pairs:
        print("  (none — clean roster)")
    if unscorable:
        print(f"\nunscorable (no routable queries generated): {', '.join(sorted(unscorable))}")
    if report.get("unparsable"):
        print(f"\nunreadable by the router — never trigger, excluded from roster ({len(report['unparsable'])}):")
        for u in report["unparsable"]:
            print(f"  {u}")
    if report.get("collisions"):
        print(f"\nname collisions — one file wins, the other is invisible ({len(report['collisions'])}):")
        for c in report["collisions"]:
            print(f"  {c['name']}: kept {c['kept']}\n    ignored {c['ignored']}")
    return 0


def cmd_judge(args):
    try:
        sk = load_skill_file(args.skill)
        name = sk["name"] if sk else args.skill
    except SkillError:
        name = args.skill
    print("judge stage is an LLM rubric pass — not run inline (no LLM inside this CLI).")
    print("Delegate to the skill-eval skill for a human-facing score:")
    print(f'  ask your agent: "use skill-eval skill to score {name}"')
    return 0


def cmd_all(args):
    skills = discover_skills(strict=args.strict)
    target, notes = resolve_target(args.skill, skills)
    if not target:
        raise SkillError(f"skill not found: {args.skill}")
    sk = skills[target]
    for n in notes:
        print(f"note: {n}\n")

    print(f"=== lint: {target} ===")
    lint_result = check_lint(sk)
    print(json.dumps(lint_result, indent=2))
    if lint_result["gate"] == "fail":
        print("\ngate: lint FAILED — stopping.")
        return 1

    print(f"\n=== scan: {target} ===")
    scan_result = check_scan(sk)
    print(json.dumps(scan_result, indent=2))
    if scan_result["gate"] == "fail":
        print("\ngate: scan FAILED — stopping.")
        return 1

    print(f"\n=== contend: {target} vs roster of {len(skills)} ===")
    contend_result = check_contend(target, skills, load_query_overrides(args.queries, skills))
    print(json.dumps(contend_result, indent=2))

    print(f"\n=== judge: {target} ===")
    print(f'delegate: "use skill-eval skill to score {target}"')

    worst = f" ({contend_result['worst_victim']})" if contend_result["worst_victim"] else ""
    print("\n=== summary ===")
    print(f"lint: {lint_result['gate']}  scan: {scan_result['gate']}  "
          f"shadow_rate: {contend_result['shadow_rate']:.2f}  "
          f"hijack_rate: {contend_result['hijack_rate']:.2f}  "
          f"worst_victim_rate: {contend_result['worst_victim_rate']:.2f}{worst}")
    if contend_result["gate"] == "fail":
        print(f"contention gate: FAIL (shadow>{SHADOW_GATE}, hijack>{HIJACK_GATE}, "
              f"or worst_victim>{VICTIM_GATE})")
        return 1
    print("contention gate: pass")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="skilleval", description="Contention eval for AgentSkills.")
    ap.add_argument("--version", action="version", version="skilleval 0.3.0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_roster_opts(p):
        p.add_argument("--queries", help="JSON file of hand-written query sets: {skill: [query, ...]}")
        p.add_argument("--strict", action="store_true",
                       help="fail if the roster has name collisions or unparsable skills")

    p_lint = sub.add_parser("lint", help="deterministic structural checks")
    p_lint.add_argument("skill")
    p_lint.set_defaults(func=cmd_lint)

    p_scan = sub.add_parser("scan", help="injection / overbroad-trigger pattern scan")
    p_scan.add_argument("skill")
    p_scan.set_defaults(func=cmd_scan)

    p_contend = sub.add_parser("contend", help="hijack rate + shadow rate against the real roster")
    p_contend.add_argument("skill")
    add_roster_opts(p_contend)
    p_contend.set_defaults(func=cmd_contend)

    p_roster = sub.add_parser("roster", help="roster-wide shadow-rate matrix")
    p_roster.add_argument("--all", action="store_true", help="include zero-shadow skills too")
    p_roster.add_argument("--json", action="store_true", help="machine-readable output")
    add_roster_opts(p_roster)
    p_roster.set_defaults(func=cmd_roster)

    p_judge = sub.add_parser("judge", help="print delegation instructions for the LLM rubric pass")
    p_judge.add_argument("skill")
    p_judge.set_defaults(func=cmd_judge)

    p_all = sub.add_parser("all", help="run lint -> scan -> contend -> judge in sequence")
    p_all.add_argument("skill")
    add_roster_opts(p_all)
    p_all.set_defaults(func=cmd_all)

    args = ap.parse_args()
    # Exit codes are a contract: 0 clean, 1 a gate failed, 2 could not be scored.
    # An unhandled OSError exiting 1 would read as "gate failed" to any caller.
    try:
        sys.exit(args.func(args))
    except BrokenPipeError:
        os._exit(0)  # `skilleval roster | head` — not an error
    except SkillError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
