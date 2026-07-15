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
QUERY_MARKER_RE = re.compile(
    r"(?:triggers?\s+on|use\s+(?:this\s+skill\s+)?when|also\s+use\s+when|examples?)\s*:",
    re.IGNORECASE,
)
NEGATIVE_CLAUSE_RE = re.compile(r"NOT\s+for:|Do\s+NOT|SKIP\s+only|SKIP\b", re.IGNORECASE)

SHADOW_GATE = 0.3
HIJACK_GATE = 0.15


class SkillError(Exception):
    """Unusable input — a parse failure or an unscorable skill."""


def roster_dirs():
    override = os.environ.get("SKILLEVAL_ROSTER")
    if override:
        return [Path(p).expanduser() for p in override.split(os.pathsep) if p]
    return list(DEFAULT_ROSTER_DIRS)


def tokenize(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


def _unquote(val):
    val = val.strip()
    for pat in (r'^"(.*)"$', r"^'(.*)'$"):
        m = re.match(pat, val, flags=re.DOTALL)
        if m:
            return m.group(1).strip()
    return val


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
        "name": data.get("name", p.parent.name),
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


def generate_queries(desc, max_q=8):
    queries = []
    for m in QUERY_MARKER_RE.finditer(desc):
        frag = desc[m.end():]
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
            if 3 <= len(s.split()) <= 20:
                queries.append(s)
    out = []
    seen_lower = set()
    for q in queries:
        low = q.lower()
        if low not in seen_lower:
            seen_lower.add(low)
            out.append(q)
    return out[:max_q]


def load_query_overrides(path):
    """{"skill-name": ["query", ...]} — hand-written sets beat generated ones."""
    if not path:
        return {}
    data = json.loads(Path(path).expanduser().read_text())
    if not isinstance(data, dict):
        raise SkillError(f"{path}: expected a JSON object mapping skill name -> [queries]")
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
    if not sk["name"]:
        findings.append(("fail", "missing name in frontmatter"))
    elif sk["name"] != sk["dir_name"]:
        findings.append(("warn", f"name '{sk['name']}' != directory '{sk['dir_name']}'"))
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
    query_sets = build_query_sets(skills, overrides or {})

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

    hijack_hits, other_scored, other_unroutable = [], 0, 0
    for name, qs in query_sets.items():
        if name == target:
            continue
        for q in qs:
            ranked = route(q)
            if not ranked:
                other_unroutable += 1
                continue
            other_scored += 1
            if ranked[0][1] == target:
                hijack_hits.append({"query": q, "victim": name, "score": round(ranked[0][0], 3)})
    hijack_rate = len(hijack_hits) / other_scored if other_scored else 0.0

    return {
        "target": target,
        "roster_size": len(skills),
        "own_query_count": len(own),
        "own_unroutable": own_unroutable,
        "shadow_rate": round(shadow_rate, 3),
        "shadow_hits": shadow_hits,
        "other_query_count": other_scored,
        "other_unroutable": other_unroutable,
        "hijack_rate": round(hijack_rate, 3),
        "hijack_hits": sorted(hijack_hits, key=lambda h: -h["score"]),
        "gate": "fail" if (shadow_rate > SHADOW_GATE or hijack_rate > HIJACK_GATE) else "pass",
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
    result = check_contend(target, skills, load_query_overrides(args.queries))
    result["notes"] = notes
    print(json.dumps(result, indent=2))
    return 1 if result["gate"] == "fail" else 0


def cmd_roster(args):
    report = {}
    skills = discover_skills(strict=args.strict, report=report)
    route = build_router(skills)
    query_sets = build_query_sets(skills, load_query_overrides(args.queries))

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
    contend_result = check_contend(target, skills, load_query_overrides(args.queries))
    print(json.dumps(contend_result, indent=2))

    print(f"\n=== judge: {target} ===")
    print(f'delegate: "use skill-eval skill to score {target}"')

    print("\n=== summary ===")
    print(f"lint: {lint_result['gate']}  scan: {scan_result['gate']}  "
          f"shadow_rate: {contend_result['shadow_rate']:.2f}  "
          f"hijack_rate: {contend_result['hijack_rate']:.2f}")
    if contend_result["gate"] == "fail":
        print(f"contention gate: FAIL (shadow>{SHADOW_GATE} or hijack>{HIJACK_GATE})")
        return 1
    print("contention gate: pass")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="skilleval", description="Contention eval for AgentSkills.")
    ap.add_argument("--version", action="version", version="skilleval 0.1.0")
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
    try:
        sys.exit(args.func(args))
    except SkillError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
