"""Generate a draw.io-style command flow diagram from a CLI-CR workflow YAML.

    python gen_command_diagram.py <workflow.yaml> <output.html> [mermaid.min.js]

One flowchart per phase. Every command in the workflow is a box, wired in
execution order. Where a step has a validation, a decision diamond follows it
and the failure branch shows what it costs: continue, stop the run, or arm the
rollback. Conditional steps get a diamond in front showing the `when`.

Bundles mermaid inline when a path to mermaid.min.js is given, so the page opens
offline with no CDN.
"""

import html
import json
import os
import re
import sys
from collections import OrderedDict

import yaml

ROLLBACK_GLOBALS = {}

SHORT_NODE = {
    "niam_dpa_node1": "node1",
    "niam_dpa_node2": "node2",
    "repo_server": "repo",
    "gitlab_api": "gitlab",
    "local": "OM server",
    "${STANDBY_NODE}": "STANDBY",
    "${ACTIVE_NODE}": "ACTIVE",
}


def clean(text, limit=None):
    """Make a string safe inside a mermaid quoted label."""
    if text is None:
        return ""
    s = " ".join(str(text).split())
    # conditions are written ${...} in the YAML; show just the expression
    if s.startswith("${") and s.endswith("}"):
        s = s[2:-1].strip()
    s = s.replace("\\", "/").replace('"', "'")
    s = s.replace("{", "(").replace("}", ")")
    # spell the boolean operators out - "&&" / "||" are unreadable in a small
    # shape, and escaping the pipes used to turn "||" into " / / ".
    s = s.replace("&&", " AND ").replace("||", " OR ")
    s = " ".join(s.split())
    s = s.replace("<", "").replace(">", "")
    if limit and len(s) > limit:
        s = s[:limit - 1] + "…"
    return html.escape(s, quote=False)


def cond_label(expr, width=34, maxlines=6):
    """A condition wrapped over several lines so it fits inside its shape.

    Truncating long expressions hid the interesting half of the condition and
    pushed the text outside the diamond, so wrap instead.
    """
    return wrap(clean(expr), width=width, maxlines=maxlines)


def wrap(text, width=46, maxlines=3):
    """Hard-wrap a command onto <br/> lines for a mermaid box."""
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
            if len(lines) == maxlines:
                break
        else:
            cur = f"{cur} {w}".strip()
    if cur and len(lines) < maxlines:
        lines.append(cur)
    out = "<br/>".join(lines)
    if len(" ".join(words)) > len(" ".join(l for l in lines)):
        out += " …"
    return out


def flatten(steps, depth=0, loop=None):
    """Unroll loops once, carrying the LOOP's own when / skip_when down onto its
    sub-steps. A loop body usually has no conditions of its own - the gate lives
    on the loop - so dropping it makes those steps look unconditional."""
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        if st.get("type") == "loop":
            meta = {"when": st.get("when"), "skip_when": st.get("skip_when"),
                    "label": f"loop over {st.get('for_each', '')}"}
            for sub in flatten(st.get("steps"), depth + 1, meta):
                yield sub
        else:
            yield st, loop


def and3(a, b):
    if a is False or b is False:
        return False
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return True


def or3(a, b):
    if a is True or b is True:
        return True
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return False


def gate_of(st, loop, state):
    """(when, skip_when) for a step, combined with its enclosing loop's gates."""
    w = eval_expr(st.get("when"), state) if st.get("when") else True
    sk = eval_expr(st.get("skip_when"), state) if st.get("skip_when") else False
    if loop:
        if loop.get("when"):
            w = and3(w, eval_expr(loop["when"], state))
        if loop.get("skip_when"):
            sk = or3(sk, eval_expr(loop["skip_when"], state))
    return w, sk


def consequence(st, default_on_failure="stop"):
    """What a failed validation costs: (kind, text).

    A step without its own `on_failure` inherits globals.defaults.on_failure -
    the engine does exactly that (ExecutionOrchestrator: step.getOn_failure()
    else globals.defaults). Reading only the step key drew inherited-stop steps
    as harmless "run continues", which is the opposite of what happens.
    """
    val = st.get("validation") or {}
    fail = val.get("failure") if isinstance(val, dict) else None
    vs = (fail or {}).get("vars") or {}
    if "ROLLBACK_REQUIRED" in vs and str(vs["ROLLBACK_REQUIRED"]).lower() == "true":
        return "rb", "ROLLBACK_REQUIRED = true"
    effective = st.get("on_failure") or default_on_failure
    if effective == "stop":
        inherited = "" if st.get("on_failure") else " (inherited)"
        return "stop", f"run stops here{inherited}"
    bits = [f"{k} = {v}" for k, v in vs.items()]
    return "warn", (bits[0] if bits else "recorded, run continues")


def gate_label(st):
    """Label for the diamond in front of a step.

    A step may carry BOTH `when` and `skip_when` - it runs only when the first
    is true AND the second is false. Showing just one of them hides half the
    gate, so both go in the diamond when both are present.
    """
    w, sw = st.get("when"), st.get("skip_when")
    if w and sw:
        return (f"<b>when</b><br/>{cond_label(w)}"
                f"<br/><b>and not skip when</b><br/>{cond_label(sw)}")
    if w:
        return f"<b>when</b><br/>{cond_label(w)}"
    return f"<b>skip when</b><br/>{cond_label(sw)}"


def raw_command(st):
    cmd = st.get("send")
    if cmd is None and isinstance(st.get("rest"), dict):
        r = st["rest"]
        cmd = f'{r.get("method","GET")} {r.get("path","")}'
    return str(cmd).strip() if cmd is not None else "(plugin step)"


def step_info(rows, pid, default_on_failure="stop"):
    """Per-box detail for the click-through drawer.

    The boxes are deliberately short - the full command, both gate conditions and
    the pass criteria live here instead, one click away, so nothing has to be
    truncated on the canvas to stay readable.
    """
    info = {}
    for i, (st, in_loop) in enumerate(rows, 1):
        val = st.get("validation") or {}
        crit = ""
        succ = val.get("success") if isinstance(val, dict) else None
        if isinstance(succ, dict) and isinstance(succ.get("criteria"), dict):
            c = succ["criteria"]
            crit = c.get("expr") or next(
                (f"{k} of {len(c[k])} checks" for k in ("all", "any") if k in c), "")
        fail = val.get("failure") if isinstance(val, dict) else None
        sets = [f"{k} = {v}" for k, v in ((fail or {}).get("vars") or {}).items()]
        kind, text = consequence(st, default_on_failure)
        info[f"{pid}_{i}"] = {
            "n": i,
            "node": str(st.get("node", "local")),
            "desc": st.get("command_description") or "",
            "cmd": raw_command(st),
            "when": st.get("when") or "",
            "skip": st.get("skip_when") or "",
            "crit": crit,
            "sets": sets,
            "fail": kind,
            "failtext": text,
            "loop": bool(in_loop),
        }
    return info


def phase_diagram(phase_name, rows, pid, default_on_failure="stop"):
    """Mermaid source for one phase."""
    L = []
    A = L.append
    # edgeLabelBackground + the explicit text colours stop the "pass"/"fail" edge
    # captions being drawn as dark-on-dark once a theme is applied.
    A('%%{init:{"theme":"base","themeVariables":{'
      '"fontFamily":"Segoe UI, system-ui, sans-serif","fontSize":"13px",'
      '"primaryColor":"#eaf0f7","primaryTextColor":"#10243a",'
      '"primaryBorderColor":"#2a6099","lineColor":"#8095ab",'
      '"textColor":"#10243a","nodeTextColor":"#10243a",'
      '"edgeLabelBackground":"#ffffff","labelBackground":"#ffffff",'
      '"tertiaryColor":"#ffffff","mainBkg":"#eaf0f7","clusterBkg":"#ffffff"'
      '}}}%%')
    A("flowchart TD")
    A(f'  START(["{clean(phase_name)} begins"])')

    ok_class, gate_class, bad_class, rb_class, stop_class = [], [], [], [], []

    def entry_of(n):
        """Where flow enters step n (its gate if it has one, else the box)."""
        if n > len(rows):
            return "END"
        st_n = rows[n - 1][0]
        return f"{pid}_{n}g" if (st_n.get("when") or st_n.get("skip_when")) else f"{pid}_{n}"

    A(f"  START --> {entry_of(1)}")

    for i, (st, in_loop) in enumerate(rows, 1):
        sid = f"{pid}_{i}"
        node = SHORT_NODE.get(str(st.get("node", "local")), str(st.get("node", "local")))
        cmd = st.get("send")
        if cmd is None and isinstance(st.get("rest"), dict):
            r = st["rest"]
            cmd = f'{r.get("method","GET")} {r.get("path","")}'
        cmd = clean(cmd if cmd is not None else "(plugin step)")
        label = f"<b>{i}. {clean(node)}</b><br/>{wrap(cmd)}"
        if in_loop:
            label += "<br/><i>retry loop</i>"

        # conditional gate in front of the step
        cond = st.get("when") or st.get("skip_when")
        if cond:
            gid = f"{sid}g"
            A(f'  {gid}{{"{gate_label(st)}"}}')
            gate_class.append(gid)
            A(f'  {gid} -- "no" --> {sid}skip["step skipped"]')
            ok_class.append(f"{sid}skip")
            A(f'  {gid} -- "yes" --> {sid}')

        A(f'  {sid}["{label}"]')

        val = st.get("validation") or {}
        if isinstance(val, dict) and val.get("enabled"):
            vid = f"{sid}v"
            crit = ""
            succ = val.get("success")
            if isinstance(succ, dict) and isinstance(succ.get("criteria"), dict):
                c = succ["criteria"]
                crit = c.get("expr") or next(
                    (f"{k} of {len(c[k])} checks" for k in ("all", "any") if k in c), "")
            A(f'  {vid}{{"{cond_label(crit) or "validation"}"}}')
            gate_class.append(vid)
            A(f"  {sid} --> {vid}")
            kind, text = consequence(st, default_on_failure)
            fid = f"{sid}f"
            A(f'  {fid}["{clean(text, 60)}"]')
            if kind == "rb":
                rb_class.append(fid)
            elif kind == "stop":
                stop_class.append(fid)
            else:
                bad_class.append(fid)
            A(f'  {vid} -- "fail" --> {fid}')
            tail, fail_tail, fail_kind = vid, fid, kind
        else:
            tail, fail_tail, fail_kind = sid, None, None

        nxt_entry = entry_of(i + 1)
        if tail == sid:
            A(f"  {sid} --> {nxt_entry}")
        else:
            A(f'  {tail} -- "pass" --> {nxt_entry}')
        # a non-fatal failure still carries on to the next step
        if fail_tail and fail_kind == "warn":
            A(f"  {fail_tail} --> {nxt_entry}")
        if cond:
            A(f"  {sid}skip --> {nxt_entry}")

    A('  END(["phase complete"])')
    A("  classDef cmd fill:#eaf0f7,stroke:#1a3a5c,color:#12293f;")
    A("  classDef gate fill:#fdf3d8,stroke:#b7770d,color:#6b4708;")
    A("  classDef warn fill:#eef1f4,stroke:#7d92a8,color:#3f4d5a;")
    A("  classDef rb fill:#fbf0dd,stroke:#b45309,color:#7a3a06;")
    A("  classDef stopn fill:#fbe0de,stroke:#a93226,color:#6d1f18;")
    A("  classDef term fill:#e4f4ea,stroke:#1e7e45,color:#0e5227;")
    cmds = [f"{pid}_{i}" for i in range(1, len(rows) + 1)]
    if cmds:
        A("  class " + ",".join(cmds) + " cmd;")
    for names, cls in ((gate_class, "gate"), (ok_class, "warn"), (bad_class, "warn"),
                       (rb_class, "rb"), (stop_class, "stopn")):
        if names:
            A("  class " + ",".join(names) + f" {cls};")
    A("  class START,END term;")
    return "\n".join(L)



# ============================================================================ #
#  Auto-rollback explorer
#
#  For any ACTIVITY command that can arm a rollback, work out which
#  ROLLBACK_CONFIGURATION steps would actually execute. The rollback steps are
#  gated by `when` / `skip_when` on variables the earlier phases set, so the
#  answer genuinely differs per failure point - it has to be computed, not drawn.
#
#  Conditions are evaluated with THREE-VALUED logic. Anything that depends on a
#  value only the real node can produce (a captured version string, a checksum
#  comparison) resolves to UNKNOWN and is reported as "depends", never guessed.
# ============================================================================ #

UNKNOWN = None
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CMP = re.compile(r"^\s*(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")


# Discovered from the workflow, never hardcoded:
#   WRITTEN  every variable the YAML assigns anywhere. A name outside this set is
#            supplied from outside the workflow (CIQ / arglist), so its value is
#            genuinely unknowable here.
#   NOT_EQ /
#   KNOWN    facts implied by the phases that DID run. If ACTIVITY is gated on
#            ${ROLLBACK_ONLY != "true"} and ACTIVITY ran, then ROLLBACK_ONLY is
#            not "true" - enough to settle any test against that literal, without
#            this file ever naming the variable.
#   EXTERNAL a name the workflow never writes but DOES interpolate into command
#            text must be supplied from outside (CIQ / arglist), so it is
#            unknowable. A name that is neither written nor used in any command
#            is simply empty at runtime - which is a definite answer, and is how
#            a typo'd condition variable behaves.
WRITTEN = set()
EXTERNAL = set()
NOT_EQ = {}
KNOWN = {}


def collect_written(all_rows, gvars):
    """Every variable name the workflow assigns, in any phase."""
    out = set(gvars or {})
    for rows in all_rows:
        for st, _loop in rows:
            for r in st.get("register") or []:
                if not isinstance(r, dict):
                    continue
                if r.get("name"):
                    out.add(r["name"])
                for m in re.finditer(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>",
                                     str(r.get("regex", ""))):
                    out.add(m.group(1))
            val = st.get("validation")
            if isinstance(val, dict):
                for branch in ("success", "warning", "failure"):
                    b = val.get(branch)
                    if isinstance(b, dict):
                        out.update((b.get("vars") or {}).keys())
    return out


def collect_external(all_rows):
    """Names interpolated into command text - they must come from outside."""
    out = set()
    for rows in all_rows:
        for st, _loop in rows:
            blob = " ".join(str(st.get(k, "")) for k in
                            ("send", "command_description", "var_meta", "rest"))
            val = st.get("validation")
            if isinstance(val, dict):
                blob += " " + str(val.get("description", ""))
            out.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", blob))
    return out


def phase_facts(when_exprs):
    """Turn the gates of the phases that DID run into facts."""
    NOT_EQ.clear()
    KNOWN.clear()
    for expr in when_exprs:
        if not expr:
            continue
        s = str(expr).strip()
        if s.startswith("${") and s.endswith("}"):
            s = s[2:-1].strip()
        if "||" in s or "(" in s:
            continue                      # only unambiguous single facts
        for atom in s.split("&&"):
            m = _CMP.match(atom)
            if not m:
                continue
            lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()
            if not _IDENT.match(lhs):
                continue
            if not (len(rhs) >= 2 and rhs[0] in "\"'" and rhs[-1] == rhs[0]):
                continue
            lit = rhs[1:-1]
            if op == "!=":
                NOT_EQ.setdefault(lhs, set()).add(lit)
            elif op == "==":
                KNOWN[lhs] = lit


def _resolve(tok, state):
    """(known, value) for one side of a comparison.

    A variable the workflow assigns somewhere but not on this path is NOT
    unknown - the engine interpolates it as empty, which is a definite answer.
    Only live command output and values from outside the workflow are unknowable.
    """
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] in "\"'" and tok[-1] == tok[0]:
        return True, tok[1:-1]
    if _IDENT.match(tok):
        if tok in state:
            return (state[tok] is not None), state[tok]
        if tok in KNOWN:
            return True, KNOWN[tok]
        if tok in WRITTEN:
            return True, ""      # assigned somewhere, just not on this path
        if tok in EXTERNAL:
            return False, None   # supplied from outside the workflow
        return True, ""          # nothing ever sets it -> empty at runtime
    return False, None


def eval_atom(atom, state):
    m = _CMP.match(atom)
    if not m:
        return UNKNOWN
    lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()

    # a fact from an executed phase's gate settles the test even when the exact
    # value is unknown ("not 'true'" answers both == 'true' and != 'true')
    if (op in ("==", "!=") and _IDENT.match(lhs) and lhs not in state
            and len(rhs) >= 2 and rhs[0] in "\"'" and rhs[-1] == rhs[0]
            and rhs[1:-1] in NOT_EQ.get(lhs, ())):
        return op == "!="

    lk, lv = _resolve(lhs, state)
    rk, rv = _resolve(m.group(3), state)
    if not lk or not rk:
        return UNKNOWN
    if op == "==":
        return lv == rv
    if op == "!=":
        return lv != rv
    if op == ">":
        return lv > rv
    if op == "<":
        return lv < rv
    if op == ">=":
        return lv >= rv
    if op == "<=":
        return lv <= rv
    return UNKNOWN


def eval_expr(expr, state):
    """True / False / UNKNOWN for a workflow condition."""
    if expr is None or str(expr).strip() == "":
        return True
    s = str(expr).strip()
    if s.startswith("${") and s.endswith("}"):
        s = s[2:-1].strip()
    if "(" in s or ")" in s:          # no grouping in these workflows; be honest
        return UNKNOWN
    any_unknown = False
    for clause in s.split("||"):
        c_false = False
        c_unknown = False
        for atom in clause.split("&&"):
            v = eval_atom(atom, state)
            if v is False:
                c_false = True
                break
            if v is UNKNOWN:
                c_unknown = True
        if c_false:
            continue
        if c_unknown:
            any_unknown = True
        else:
            return True
    return UNKNOWN if any_unknown else False


def _subst(value, state):
    """Resolve ${VAR} inside a register value; UNKNOWN if it cannot be resolved."""
    v = str(value)
    if "${" not in v:
        return v
    out = v
    for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", v):
        name = m.group(1)
        if name in state and state[name] is not None:
            out = out.replace(m.group(0), state[name])
        else:
            return None
    return out


def apply_step(st, state, branch, uncertain=False):
    """Apply one step's effects to the variable state.

    `uncertain` means we do not know whether the step ran at all, so everything
    it would write becomes UNKNOWN rather than its concrete value.
    """
    for r in st.get("register") or []:
        if not isinstance(r, dict):
            continue
        if r.get("name"):
            cond = r.get("when")
            v = eval_expr(cond, state) if cond else True
            if uncertain:
                state[r["name"]] = None
            elif v is True:
                state[r["name"]] = _subst(r.get("value", ""), state)
            elif v is UNKNOWN:
                state[r["name"]] = None
        else:
            # captured from live command output - unknowable statically
            for m in re.finditer(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>", str(r.get("regex", ""))):
                state[m.group(1)] = None
    val = st.get("validation")
    if isinstance(val, dict):
        b = val.get(branch)
        if isinstance(b, dict):
            for k, v in (b.get("vars") or {}).items():
                state[k] = None if uncertain else str(v)


def arming_points(act_rows):
    """Activity step numbers whose failure branch sets ROLLBACK_REQUIRED=true."""
    out = []
    for i, (st, _loop) in enumerate(act_rows, 1):
        val = st.get("validation")
        fail = val.get("failure") if isinstance(val, dict) else None
        vs = (fail or {}).get("vars") or {}
        if str(vs.get("ROLLBACK_REQUIRED", "")).lower() == "true":
            out.append(i)
    return out


def simulate(phase_rows, act_rows, fail_idx, gvars, phase_default_on_failure=None):
    """State after the earlier phases succeed and ACTIVITY fails at `fail_idx`.

    If the failing step is `on_failure: continue` the run does not stop there,
    so the remaining steps still execute and still write their variables.
    """
    state = {}
    for k, v in (gvars or {}).items():
        state[k] = "" if v is None else str(v)
    def ran(st, loop):
        """False only when a gate definitely rules the step out."""
        w, sk = gate_of(st, loop, state)
        return not (w is False or sk is True)

    for rows in phase_rows:
        for st, loop in rows:
            if ran(st, loop):
                apply_step(st, state, "success")

    keep_going = False
    for i, (st, loop) in enumerate(act_rows, 1):
        if i < fail_idx:
            if ran(st, loop):
                apply_step(st, state, "success")
        elif i == fail_idx:
            apply_step(st, state, "failure")
            on_fail = st.get("on_failure", phase_default_on_failure)
            keep_going = (on_fail == "continue")
            if not keep_going:
                break
        else:
            if ran(st, loop):
                apply_step(st, state, "success")
    return state


def rollback_plan(rb_rows, state):
    """[(idx, step, verdict)] with verdict RUN / SKIP / DEPENDS."""
    plan = []
    st_state = dict(state)
    # In an auto-rollback the rollback phase is entered because this is true, and
    # it stays true for the whole phase - the gates that matter are the per-step
    # when / skip_when, evaluated against what ACTIVITY managed to write.
    rb_flag = next((k for k in st_state if k.upper() == "ROLLBACK_REQUIRED"),
                   "ROLLBACK_REQUIRED")
    st_state[rb_flag] = "true"
    for i, (st, loop) in enumerate(rb_rows, 1):
        w, sk = gate_of(st, loop, st_state)
        if w is False or sk is True:
            verdict = "SKIP"
        elif w is UNKNOWN or sk is UNKNOWN:
            verdict = "DEPENDS"
        else:
            verdict = "RUN"
        plan.append((i, st, verdict))
        if verdict == "RUN":
            apply_step(st, st_state, "success")
        elif verdict == "DEPENDS":
            # we do not know whether it ran, so neither do we know what it wrote
            apply_step(st, st_state, "success", uncertain=True)
        st_state[rb_flag] = "true"          # stays true for the whole phase
    return plan


def cmd_text(st):
    """The command a step sends, as written in the YAML."""
    cmd = st.get("send")
    if cmd is None and isinstance(st.get("rest"), dict):
        r = st["rest"]
        cmd = f'{r.get("method","GET")} {r.get("path","")}'
    return " ".join(str(cmd if cmd is not None else "(plugin step)").split())


def plain(text, limit=None):
    """HTML-escaped text. Unlike clean() this keeps ${VAR} intact - it is only
    mermaid labels that cannot carry braces."""
    s = " ".join(str(text).split())
    if limit and len(s) > limit:
        s = s[:limit - 1] + "…"
    return html.escape(s)


def _box(st, i, prefix):
    node = SHORT_NODE.get(str(st.get("node", "local")), str(st.get("node", "local")))
    cmd = st.get("send")
    if cmd is None and isinstance(st.get("rest"), dict):
        cmd = f'{st["rest"].get("method","GET")} {st["rest"].get("path","")}'
    return f'<b>{prefix}{i}. {clean(node)}</b><br/>{wrap(clean(cmd if cmd is not None else "(plugin step)"), 42, 2)}'


def mixed_diagram(act_rows, fail_idx, rb_rows, plan, key):
    """Activity path up to the failure, then the rollback that really runs."""
    L = []
    A = L.append
    A('%%{init:{"theme":"base","themeVariables":{'
      '"fontFamily":"Segoe UI, system-ui, sans-serif","fontSize":"13px",'
      '"primaryColor":"#eaf0f7","primaryTextColor":"#10243a",'
      '"primaryBorderColor":"#2a6099","lineColor":"#8095ab",'
      '"textColor":"#10243a","nodeTextColor":"#10243a",'
      '"edgeLabelBackground":"#ffffff","labelBackground":"#ffffff",'
      '"tertiaryColor":"#ffffff","mainBkg":"#eaf0f7"}}}%%')
    A("flowchart TD")
    A('  S(["ACTIVITY_CONFIGURATION"])')

    done, prev = [], "S"
    for i in range(1, fail_idx):
        nid = f"{key}a{i}"
        A(f'  {nid}["{_box(act_rows[i-1][0], i, "")}"]')
        A(f"  {prev} --> {nid}")
        done.append(nid)
        prev = nid

    fid = f"{key}fail"
    A(f'  {fid}["{_box(act_rows[fail_idx-1][0], fail_idx, "")}"]')
    A(f"  {prev} --> {fid}")
    vid = f"{key}v"
    A(f'  {vid}{{"validation fails here"}}')
    A(f"  {fid} --> {vid}")
    rid = f"{key}rb"
    A(f'  {rid}["ROLLBACK_REQUIRED = true"]')
    A(f'  {vid} -- "fail" --> {rid}')

    # The rollback runs as a horizontal lane off the failure, so you can read
    # "this activity command failed -> these rollback commands, on these nodes"
    # left to right. Only the steps that can execute are drawn; the ones a gate
    # rules out are summarised, otherwise the lane is 27 boxes wide.
    live = [(i, st, v) for i, st, v in plan if v != "SKIP"]
    dead = [i for i, _st, v in plan if v == "SKIP"]

    runs, deps = [], []
    A(f'  subgraph {key}RB["ROLLBACK_CONFIGURATION &#183; '
      f'{len(live)} of {len(plan)} steps execute"]')
    A("    direction LR")
    prev = None
    for i, st, verdict in live:
        nid = f"{key}r{i}"
        suffix = "<br/><i>depends on node</i>" if verdict == "DEPENDS" else ""
        A(f'    {nid}["{_box(st, i, "R")}{suffix}"]')
        if prev:
            A(f"    {prev} --> {nid}")
        (runs if verdict == "RUN" else deps).append(nid)
        prev = nid
    A("  end")
    A(f"  {rid} --> {key}RB")

    if dead:
        nums = ", ".join(f"R{n}" for n in dead)
        A(f'  {key}sk["{len(dead)} steps skipped by a gate<br/>{clean(nums, 150)}"]')
        A(f"  {rid} -.-> {key}sk")

    A('  P(["POST_NODE_HEALTH_CHECK"])')
    A(f"  {key}RB --> P")

    A("  classDef ok fill:#e4f4ea,stroke:#1e7e45,color:#0e5227;")
    A("  classDef failn fill:#fbe0de,stroke:#a93226,color:#6d1f18,stroke-width:2px;")
    A("  classDef gate fill:#fdf3d8,stroke:#b7770d,color:#6b4708;")
    A("  classDef arm fill:#fbf0dd,stroke:#b45309,color:#7a3a06,stroke-width:2px;")
    A("  classDef run fill:#eaf0f7,stroke:#2a6099,color:#10243a;")
    A("  classDef skip fill:#f0f1f3,stroke:#b9c2cc,color:#9aa6b2,stroke-dasharray:4 3;")
    A("  classDef dep fill:#fdf7e8,stroke:#c9a24d,color:#6b5417,stroke-dasharray:4 3;")
    A("  classDef term fill:#e7effa,stroke:#2a6099,color:#1c4470;")
    if done:
        A("  class " + ",".join(done) + " ok;")
    A(f"  class {fid} failn;")
    A(f"  class {vid} gate;")
    A(f"  class {rid} arm;")
    for names, cls in ((runs, "run"), (deps, "dep")):
        if names:
            A("  class " + ",".join(names) + f" {cls};")
    if dead:
        A(f"  class {key}sk skip;")
    A("  class S,P term;")
    return "\n".join(L)


def comb_html(act, points, plans):
    """The activity as a dead-straight vertical column, with each rollback
    branch running horizontally off its command.

    Laid out directly rather than through mermaid: dagre gives every branch its
    own rank, which pushes the following activity command sideways and bends the
    spine. Here the spine is a fixed column and the branch is a row beside it.
    """
    out = []
    A = out.append
    A('<div class="spine">')
    for i, (st, _loop) in enumerate(act, 1):
        node = SHORT_NODE.get(str(st.get("node", "local")), str(st.get("node", "local")))
        armed = i in plans
        A(f'<div class="srow{" armed" if armed else ""}">')
        A(f'<div class="acard{" arm" if armed else ""}" title="{plain(cmd_text(st))}">'
          f'<span class="an">{i}</span>'
          f'<span class="anode">{plain(node)}</span>'
          f'<code>{plain(cmd_text(st), 92)}</code></div>')
        if armed:
            plan = plans[i]
            live = [p for p in plan if p[2] != "SKIP"]
            nskip = len(plan) - len(live)
            A('<div class="bwrap">')
            A(f'<div class="btag">fails &rarr; <b>{len(live)}</b> rollback '
              f'command{"s" if len(live) != 1 else ""}'
              f'{f" &middot; {nskip} gated out" if nskip else ""}</div>')
            A('<div class="branch">')
            for ri, rst, verdict in live:
                rnode = SHORT_NODE.get(str(rst.get("node", "local")),
                                       str(rst.get("node", "local")))
                cls = "run" if verdict == "RUN" else "dep"
                note = '<span class="dnote">depends on node</span>' if verdict == "DEPENDS" else ""
                A(f'<div class="rcard {cls}" title="{plain(cmd_text(rst))}">'
                  f'<span class="rh"><b>R{ri}</b> <span class="rnode2">{plain(rnode)}</span></span>'
                  f'<code>{plain(cmd_text(rst), 70)}</code>{note}</div>')
            A("</div></div>")
        A("</div>")
    A("</div>")
    return "\n".join(out)


def _wrap_px(text, chars, lines):
    """Split text into at most `lines` chunks of about `chars` characters."""
    words, out, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > chars:
            out.append(cur)
            cur = w
            if len(out) == lines:
                break
        else:
            cur = f"{cur} {w}".strip()
    if cur and len(out) < lines:
        out.append(cur)
    if not out:
        return [""]
    joined = len(" ".join(out))
    if joined < len(" ".join(words)):
        out[-1] = out[-1][:chars - 2] + "…"
    return out


def comb_svg(act, points, plans):
    """Draw the activity spine and its rollback branches as SVG directly.

    Mermaid's dagre layout gives every rollback branch its own rank, which
    shifts the following activity command sideways - there is no way to pin the
    spine straight from the diagram source. Computing the coordinates here makes
    the column exact by construction, and the result still drops into the same
    canvas (pan, zoom, find, minimap, SVG/PNG export all work on any SVG).
    """
    AW, AH, RW, RH = 340, 50, 210, 58        # box sizes
    GAP, ROW, PAD = 12, 78, 24               # spacing
    SPINE_X = PAD + 26                       # where the vertical rule sits

    maxlen = max((sum(1 for _i, _s, v in plans[i] if v != "SKIP") for i in points),
                 default=0)
    width = PAD + AW + 60 + maxlen * (RW + GAP) + PAD
    height = PAD * 2 + len(act) * ROW

    s = []
    A = s.append
    A(f'<svg xmlns="http://www.w3.org/2000/svg" class="drawn" '
      f'width="{width}" height="{height}" '
      f'viewBox="0 0 {width} {height}" font-family="Segoe UI, system-ui, sans-serif">')
    A('<rect width="100%" height="100%" fill="none"/>')

    # the spine: one straight line, top to bottom
    y0 = PAD + ROW // 2
    y1 = PAD + (len(act) - 1) * ROW + ROW // 2
    A(f'<line x1="{SPINE_X}" y1="{y0}" x2="{SPINE_X}" y2="{y1}" '
      'stroke="#c3d2e2" stroke-width="2"/>')

    for i, (st, _loop) in enumerate(act, 1):
        y = PAD + (i - 1) * ROW
        cy = y + ROW // 2
        armed = i in plans
        node = SHORT_NODE.get(str(st.get("node", "local")), str(st.get("node", "local")))

        A(f'<circle cx="{SPINE_X}" cy="{cy}" r="4" '
          f'fill="{"#a93226" if armed else "#8fa6bd"}"/>')
        A(f'<line x1="{SPINE_X}" y1="{cy}" x2="{PAD + 52}" y2="{cy}" '
          'stroke="#c3d2e2" stroke-width="2"/>')

        bx, by = PAD + 52, cy - AH // 2
        stroke, fill, sw = ("#a93226", "#fdeceb", 2) if armed else ("#2a6099", "#eef3f9", 1)
        A(f'<g class="node" id="act{i}"><rect x="{bx}" y="{by}" width="{AW}" height="{AH}" '
          f'rx="7" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
        A(f'<text x="{bx + 10}" y="{by + 17}" font-size="11" font-weight="700" '
          f'fill="{"#a93226" if armed else "#2a6099"}">{html.escape(str(i))}. '
          f'{html.escape(node)}</text>')
        for n, ln in enumerate(_wrap_px(cmd_text(st), 52, 2)):
            A(f'<text x="{bx + 10}" y="{by + 31 + n * 12}" font-size="10" '
              f'font-family="Consolas, monospace" fill="#16222e">{html.escape(ln)}</text>')
        A("</g>")

        if not armed:
            continue

        live = [p for p in plans[i] if p[2] != "SKIP"]
        nskip = len(plans[i]) - len(live)
        A(f'<line x1="{bx + AW}" y1="{cy}" x2="{bx + AW + 26}" y2="{cy}" '
          'stroke="#a93226" stroke-width="2"/>')
        A(f'<text x="{bx + AW + 6}" y="{cy - 8}" font-size="9" font-weight="700" '
          f'fill="#a93226">FAILS &#8594; {len(live)} run'
          f'{f" &#183; {nskip} gated out" if nskip else ""}</text>')

        rx = bx + AW + 26
        for k, (ri, rst, verdict) in enumerate(live):
            x = rx + k * (RW + GAP)
            ry = cy - RH // 2
            rnode = SHORT_NODE.get(str(rst.get("node", "local")),
                                   str(rst.get("node", "local")))
            if verdict == "RUN":
                rs, rf, dash = "#1e7e45", "#eaf5ee", ""
            else:
                rs, rf, dash = "#c9a24d", "#fdf8ec", ' stroke-dasharray="4 3"'
            if k:
                A(f'<line x1="{x - GAP}" y1="{cy}" x2="{x}" y2="{cy}" '
                  'stroke="#c3d2e2" stroke-width="1.5"/>')
            A(f'<g class="node" id="rb{i}_{ri}">'
              f'<rect x="{x}" y="{ry}" width="{RW}" height="{RH}" rx="6" '
              f'fill="{rf}" stroke="{rs}" stroke-width="1.5"{dash}/>')
            A(f'<text x="{x + 8}" y="{ry + 15}" font-size="10" font-weight="700" '
              f'fill="{rs}">R{ri} &#183; {html.escape(rnode)}</text>')
            for n, ln in enumerate(_wrap_px(cmd_text(rst), 31, 2)):
                A(f'<text x="{x + 8}" y="{ry + 29 + n * 11}" font-size="9" '
                  f'font-family="Consolas, monospace" fill="#16222e">{html.escape(ln)}</text>')
            if verdict == "DEPENDS":
                A(f'<text x="{x + 8}" y="{ry + 52}" font-size="8" font-style="italic" '
                  'fill="#b7770d">depends on node</text>')
            A("</g>")

    A("</svg>")
    return "\n".join(s)


def comb_diagram(act, rbk, points, plans):
    """The whole activity as ONE vertical spine, with a horizontal rollback
    branch hanging off every command that can set ROLLBACK_REQUIRED = true.

    Read down the spine for the normal run; read right off any red command for
    the rollback commands that failing it would trigger, and on which node.
    """
    L = []
    A = L.append
    A('%%{init:{"theme":"base","themeVariables":{'
      '"fontFamily":"Segoe UI, system-ui, sans-serif","fontSize":"12px",'
      '"primaryColor":"#eaf0f7","primaryTextColor":"#10243a",'
      '"primaryBorderColor":"#2a6099","lineColor":"#93a7bb",'
      '"textColor":"#10243a","nodeTextColor":"#10243a",'
      '"edgeLabelBackground":"#ffffff","mainBkg":"#eaf0f7",'
      '"clusterBkg":"#fdf6f5","clusterBorder":"#e0b3ad"}}}%%')
    A("flowchart TD")
    A('  S(["ACTIVITY_CONFIGURATION"])')

    armed = set(points)
    plain_a, arm_a, runs, deps = [], [], [], []

    prev = "S"
    for i, (st, _loop) in enumerate(act, 1):
        aid = f"a{i}"
        node = SHORT_NODE.get(str(st.get("node", "local")), str(st.get("node", "local")))
        A(f'  {aid}["<b>{i}. {clean(node)}</b><br/>{wrap(clean(cmd_text(st)), 36, 2)}"]')
        A(f"  {prev} --> {aid}")
        (arm_a if i in armed else plain_a).append(aid)
        prev = aid

        if i in armed:
            plan = plans[i]
            live = [p for p in plan if p[2] != "SKIP"]
            nskip = len(plan) - len(live)
            A(f'  subgraph RB{i}["if {i} fails &#183; {len(live)} rollback commands run'
              f'{f" &#183; {nskip} gated out" if nskip else ""}"]')
            A("    direction LR")
            rprev = None
            for ri, rst, verdict in live:
                rid = f"b{i}r{ri}"
                rnode = SHORT_NODE.get(str(rst.get("node", "local")),
                                       str(rst.get("node", "local")))
                tail = "<br/><i>depends on node</i>" if verdict == "DEPENDS" else ""
                A(f'    {rid}["<b>R{ri}. {clean(rnode)}</b><br/>'
                  f'{wrap(clean(cmd_text(rst)), 30, 2)}{tail}"]')
                if rprev:
                    A(f"    {rprev} --> {rid}")
                (runs if verdict == "RUN" else deps).append(rid)
                rprev = rid
            A("  end")
            A(f'  {aid} -- "fails" --> RB{i}')

    A('  P(["POST_NODE_HEALTH_CHECK"])')
    A(f"  {prev} --> P")

    A("  classDef step fill:#eaf0f7,stroke:#2a6099,color:#10243a;")
    A("  classDef arm fill:#fbe0de,stroke:#a93226,color:#6d1f18,stroke-width:2px;")
    A("  classDef run fill:#e8f2ea,stroke:#1e7e45,color:#0e5227;")
    A("  classDef dep fill:#fdf7e8,stroke:#c9a24d,color:#6b5417,stroke-dasharray:4 3;")
    A("  classDef term fill:#e7effa,stroke:#2a6099,color:#1c4470;")
    for names, cls in ((plain_a, "step"), (arm_a, "arm"), (runs, "run"), (deps, "dep")):
        if names:
            A("  class " + ",".join(names) + f" {cls};")
    A("  class S,P term;")
    return "\n".join(L)


def lanes_diagram(act, rbk, points, plans, show_skipped=True):
    """One self-contained lane per ACTIVITY command that can arm a rollback.

    Every lane repeats the whole rollback command list for itself, so no two
    lanes share a box and there is not a single crossing line. Read a lane top
    to bottom to get exactly which rollback commands that failure triggers, and
    compare lane heights to see the rollback grow the deeper the failure is.
    """
    L = []
    A = L.append
    A('%%{init:{"theme":"base","themeVariables":{'
      '"fontFamily":"Segoe UI, system-ui, sans-serif","fontSize":"12px",'
      '"primaryColor":"#eaf0f7","primaryTextColor":"#10243a",'
      '"primaryBorderColor":"#2a6099","lineColor":"#93a7bb",'
      '"textColor":"#10243a","nodeTextColor":"#10243a",'
      '"edgeLabelBackground":"#ffffff","mainBkg":"#eaf0f7",'
      '"clusterBkg":"#f7fafd","clusterBorder":"#c3d2e2"}}}%%')
    A("flowchart LR")

    runs, deps, skips, heads = [], [], [], []
    for idx in points:
        ast = act[idx - 1][0]
        anode = SHORT_NODE.get(str(ast.get("node", "local")), str(ast.get("node", "local")))
        plan = plans[idx]
        live = [p for p in plan if p[2] != "SKIP"]
        A(f'  subgraph L{idx}["FAIL AT ACTIVITY {idx} &#183; {clean(anode)} &#183; '
          f'{len(live)} of {len(plan)} rollback commands run"]')
        A("    direction TB")

        hid = f"h{idx}"
        A(f'    {hid}["<b>{idx}. {clean(anode)}</b><br/>{wrap(clean(cmd_text(ast)), 34, 2)}"]')
        heads.append(hid)

        prev = hid
        for i, rst, verdict in plan:
            if verdict == "SKIP" and not show_skipped:
                continue
            nid = f"f{idx}r{i}"
            rnode = SHORT_NODE.get(str(rst.get("node", "local")), str(rst.get("node", "local")))
            tail = ""
            if verdict == "SKIP":
                tail = "<br/><i>skipped</i>"
            elif verdict == "DEPENDS":
                tail = "<br/><i>depends on node</i>"
            A(f'    {nid}["<b>R{i}. {clean(rnode)}</b><br/>'
              f'{wrap(clean(cmd_text(rst)), 34, 2)}{tail}"]')
            A(f"    {prev} --> {nid}")
            (runs if verdict == "RUN" else deps if verdict == "DEPENDS" else skips).append(nid)
            prev = nid
        A("  end")

    A("  classDef head fill:#fbe0de,stroke:#a93226,color:#6d1f18,stroke-width:2px;")
    A("  classDef run fill:#eaf0f7,stroke:#2a6099,color:#10243a;")
    A("  classDef dep fill:#fdf7e8,stroke:#c9a24d,color:#6b5417,stroke-dasharray:4 3;")
    A("  classDef skip fill:#f2f3f5,stroke:#c8cfd7,color:#a7b0ba,stroke-dasharray:3 3;")
    if heads:
        A("  class " + ",".join(heads) + " head;")
    for names, cls in ((runs, "run"), (deps, "dep"), (skips, "skip")):
        if names:
            A("  class " + ",".join(names) + f" {cls};")
    return "\n".join(L)


def mesh_diagram(act, rbk, points, plans):
    """Complete mesh: every rollback-arming ACTIVITY command wired to every
    ROLLBACK step it would actually call.

    Returns (mermaid_source, index_map) where index_map tells the page which
    edge indices and rollback nodes belong to each activity command, so one
    can be isolated from the hairball on click.
    """
    L = []
    A = L.append
    A('%%{init:{"theme":"base","themeVariables":{'
      '"fontFamily":"Segoe UI, system-ui, sans-serif","fontSize":"12px",'
      '"primaryColor":"#eaf0f7","primaryTextColor":"#10243a",'
      '"primaryBorderColor":"#2a6099","lineColor":"#b3c2d1",'
      '"textColor":"#10243a","nodeTextColor":"#10243a",'
      '"edgeLabelBackground":"#ffffff","mainBkg":"#eaf0f7",'
      '"clusterBkg":"#f7fafd","clusterBorder":"#cdd9e6"}}}%%')
    A("flowchart LR")

    A('  subgraph MA["ACTIVITY commands that set ROLLBACK_REQUIRED = true"]')
    A("    direction TB")
    for idx in points:
        st = act[idx - 1][0]
        node = SHORT_NODE.get(str(st.get("node", "local")), str(st.get("node", "local")))
        A(f'    A{idx}["<b>{idx}. {clean(node)}</b><br/>{wrap(clean(cmd_text(st)), 40, 2)}"]')
    A("  end")

    A('  subgraph MR["ROLLBACK_CONFIGURATION steps"]')
    A("    direction TB")
    for i, st, _v in plans[points[0]]:
        node = SHORT_NODE.get(str(st.get("node", "local")), str(st.get("node", "local")))
        A(f'    R{i}["<b>R{i}. {clean(node)}</b><br/>{wrap(clean(cmd_text(st)), 40, 2)}"]')
    A("  end")

    index, e = {}, 0
    for idx in points:
        edges, rnodes = [], []
        for i, _st, v in plans[idx]:
            if v == "SKIP":
                continue
            A(f"  A{idx} {'-->' if v == 'RUN' else '-.->'} R{i}")
            edges.append(e)
            rnodes.append(f"R{i}")
            e += 1
        index[str(idx)] = {"edges": edges, "rnodes": rnodes}

    A("  classDef act fill:#fbe0de,stroke:#a93226,color:#6d1f18;")
    A("  classDef rbs fill:#eaf0f7,stroke:#2a6099,color:#10243a;")
    A("  class " + ",".join(f"A{i}" for i in points) + " act;")
    A("  class " + ",".join(f"R{i}" for i, _s, _v in plans[points[0]]) + " rbs;")
    return "\n".join(L), index


def rollback_section(phases):
    """HTML for the auto-rollback explorer."""
    def rows_of(*names):
        for pid, (p, rows) in phases.items():
            if p.get("name") in names:
                return rows
        return []

    pre = rows_of("PRE_NODE_HEALTH_CHECK")
    bkp = rows_of("BACKUP")
    act = rows_of("ACTIVITY_CONFIGURATION")
    rbk = rows_of("ROLLBACK_CONFIGURATION")
    if not act or not rbk:
        return ""

    gvars = ROLLBACK_GLOBALS.get("vars") or {}

    # discover, per workflow: which names the YAML writes anywhere, and what the
    # gates of the phases that actually ran imply about the names it does not.
    all_rows = [r for _pid, (_p, r) in phases.items()]
    WRITTEN.clear()
    WRITTEN.update(collect_written(all_rows, gvars))
    EXTERNAL.clear()
    EXTERNAL.update(collect_external(all_rows) - WRITTEN)

    ran = [p for _pid, (p, rows) in phases.items() if rows in (pre, bkp, act)]
    phase_facts([p.get("when") for p in ran])

    act_phase = next((p for _pid, (p, rows) in phases.items() if rows is act), {})
    act_default = (act_phase.get("on_failure")
                   or (ROLLBACK_GLOBALS.get("defaults") or {}).get("on_failure"))

    points = arming_points(act)
    scenarios, matrix, plans = [], [], {}

    for n, idx in enumerate(points):
        st = act[idx - 1][0]
        state = simulate((pre, bkp), act, idx, gvars, act_default)
        plan = rollback_plan(rbk, state)
        plans[idx] = plan
        key = f"sc{n}"
        desc = str(st.get("command_description", ""))
        cmd = cmd_text(st)
        fail = ((st.get("validation") or {}).get("failure") or {}).get("message", "")
        scenarios.append({
            "key": key,
            "idx": idx,
            "cmd": plain(cmd, 78),
            "cmd_full": plain(cmd),
            "node": plain(SHORT_NODE.get(str(st.get("node", "local")), str(st.get("node", "local")))),
            "desc": plain(desc, 74),
            "why": clean(fail, 150),
            "run": sum(1 for _i, _s, v in plan if v == "RUN"),
            "dep": sum(1 for _i, _s, v in plan if v == "DEPENDS"),
            "skip": sum(1 for _i, _s, v in plan if v == "SKIP"),
            "mermaid": mixed_diagram(act, idx, rbk, plan, key),
        })
        matrix.append((idx, cmd, plan))

    out = []
    A = out.append
    A('<section id="autorollback" class="phase">')
    A('<div class="phead" role="button" tabindex="0" aria-expanded="true">'
      '<span class="caret">&#9662;</span><h2>Auto-rollback explorer</h2>'
      f'<span class="pcount">{len(points)} failure points</span>'
      '<span class="phint">click to collapse</span></div>')
    A('<div class="pbody">')
    counts = [sum(1 for _i, _s, v in plans[i] if v != "SKIP") for i in points]
    A(f'<p class="pdesc">The whole ACTIVITY phase as one straight vertical run, top to bottom. '
      f"{len(points)} of its commands are outlined red — those are the ones whose failure sets "
      "<code>ROLLBACK_REQUIRED = true</code>. Each red command has a <b>horizontal branch</b> "
      "running off it: the rollback commands that failing <em>that</em> command triggers, in "
      "order, each on its own node. Which ones those are is computed from the workflow's own "
      "<code>when</code> / <code>skip_when</code> gates — including the gates that sit on a "
      "retry <code>loop</code> rather than on its steps — against the variable state at the "
      f"moment of failure. The branches lengthen further down: {min(counts)} rollback commands "
      f"at the earliest failure, {max(counts)} at the latest.</p>")

    A(comb_html(act, points, plans))

    A('<div class="legend" style="margin-top:14px">'
      '<span><i class="key k-stop"></i> command whose failure arms the rollback</span>'
      '<span><i class="key k-run"></i> rollback command that runs</span>'
      '<span><i class="key k-dep"></i> depends on what the node returns</span></div>')

    # ---- the same thing on a zoomable canvas, like the phase diagrams ----- #
    A('<h3 class="keyhead" style="margin-top:26px">The same flow on a zoomable canvas</h3>')
    A('<div class="canvas" data-name="Auto-rollback">')
    A('<div class="toolbar">')
    A('<button data-act="out" title="Zoom out (-)">&minus;</button>')
    A('<span class="zoom">100%</span>')
    A('<button data-act="in" title="Zoom in (+)">&plus;</button>')
    A('<button data-act="fit" title="Fit to window (f)">Fit</button>')
    A('<button data-act="reset" title="Actual size (0)">100%</button>')
    A('<input class="find" type="search" placeholder="Find a command  /" '
      'aria-label="Find a command">')
    A('<span class="found"></span>')
    A('<span class="spacer"></span>')
    A('<button data-act="map" class="on" title="Toggle minimap">Map</button>')
    A('<button data-act="svg" title="Download as SVG">SVG</button>')
    A('<button data-act="png" title="Download as PNG">PNG</button>')
    A('<button data-act="full" title="Fullscreen">Fullscreen</button>')
    A("</div>")
    A('<div class="viewport"><div class="stage">')
    A(comb_svg(act, points, plans))
    A("</div>")
    A('<div class="minimap"><div class="mmview"></div></div>')
    A('<aside class="drawer"><header><span class="n"></span>'
      '<span class="t"></span><button title="Close">&times;</button></header>'
      '<div class="body"></div></aside>')
    A("</div>")
    A('<p class="hint-row">drag to pan · scroll to zoom · <kbd>/</kbd> find · '
      "<kbd>f</kbd> fit · <kbd>0</kbd> 100% · SVG / PNG to export</p>")
    A("</div>")

    # ---- coverage matrix ------------------------------------------------- #
    A('<h2 style="margin-top:34px">Coverage matrix</h2>')
    A('<p class="pdesc">Every arming point against every rollback step. '
      "<b>&#9679;</b> runs &nbsp; <b>?</b> depends on the node &nbsp; "
      '<span class="c-skip">&middot;</span> skipped.</p>')
    A('<div class="matwrap"><table class="mat"><thead><tr>'
      '<th class="rowhead">Activity command that failed</th>')
    for i, rst, _v in matrix[0][2]:
        A(f'<th title="R{i} &#183; {plain(cmd_text(rst))}">R{i}</th>')
    A("</tr></thead><tbody>")
    for idx, cmd, plan in matrix:
        A(f'<tr><td class="rowhead" title="{plain(cmd)}">'
          f'<b>{idx}.</b> <code>{plain(cmd, 76)}</code></td>')
        for _i, _st, v in plan:
            cls = {"RUN": "c-run", "DEPENDS": "c-dep", "SKIP": "c-skip"}[v]
            sym = {"RUN": "&#9679;", "DEPENDS": "?", "SKIP": "&middot;"}[v]
            A(f'<td class="{cls}">{sym}</td>')
        A("</tr>")
    A("</tbody></table></div>")

    # R1..Rn are meaningless without their commands
    A('<h3 class="keyhead">Rollback steps</h3>')
    A('<ol class="rkey">')
    for i, rst, _v in matrix[0][2]:
        node = SHORT_NODE.get(str(rst.get("node", "local")), str(rst.get("node", "local")))
        A(f'<li><span class="rn">R{i}</span><span class="rnode">{plain(node)}</span>'
          f'<code>{plain(cmd_text(rst), 118)}</code></li>')
    A("</ol>")

    # ---- one repeated lane per failure ------------------------------------ #
    counts = [sum(1 for _i, _s, v in plans[i] if v != "SKIP") for i in points]
    A('<h2 style="margin-top:34px">Activity spine with rollback branches</h2>')
    A(f'<p class="pdesc">The whole ACTIVITY phase as one vertical run, top to bottom. '
      f"{len(points)} of its commands are drawn red — those are the ones whose failure sets "
      "<code>ROLLBACK_REQUIRED = true</code>. Each red command has a <b>horizontal branch</b> "
      "running off to the right: the rollback commands that failing <em>that</em> command "
      "triggers, in order, each on its own node. Only the commands that actually run are drawn "
      "(green), plus the ones that depend on what the node returns (amber dashed); the branch "
      f"title says how many a gate ruled out. The branches get longer further down — "
      f"{min(counts)} rollback commands at the earliest failure, {max(counts)} at the latest.</p>")
    A('<script type="application/json" id="rbdata">')
    A(json.dumps({s["key"]: {"m": s["mermaid"], "why": s["why"]} for s in scenarios}))
    A("</script>")
    A("</div>")        # /.pbody
    A("</section>")
    return "\n".join(out)


STYLE = """<style>
:root{
  --ink:#16222e; --dim:#6b7c8d; --ground:#eef1f5; --panel:#fff; --rule:#dbe3ec;
  --accent:#2a6099; --accent-soft:#e7effa; --accent-deep:#1c4470;
  --sheet:#fff; --grid:#dde5ef;
  --code-bg:#eef2f8; --code-ink:#1b3350;
  --sans:"Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
  --mono:Consolas,"Cascadia Mono",Menlo,monospace;
  --shadow:0 1px 2px rgba(20,40,70,.06),0 6px 20px rgba(20,40,70,.07);
}
@media (prefers-color-scheme:dark){:root{
  --ink:#dbe4ee; --dim:#93a4b6; --ground:#141a21; --panel:#1c242e; --rule:#2e3a47;
  --accent:#6fa8e0; --accent-soft:#22303f; --accent-deep:#9cc6ef;
  --sheet:#f2f5f9; --grid:#dde5ef;
  --code-bg:#222c38; --code-ink:#c3d6ee;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 6px 20px rgba(0,0,0,.35);
}}
:root[data-theme=dark]{
  --ink:#dbe4ee; --dim:#93a4b6; --ground:#141a21; --panel:#1c242e; --rule:#2e3a47;
  --accent:#6fa8e0; --accent-soft:#22303f; --accent-deep:#9cc6ef;
  --sheet:#f2f5f9; --grid:#dde5ef;
  --code-bg:#222c38; --code-ink:#c3d6ee;
}
:root[data-theme=light]{
  --ink:#16222e; --dim:#6b7c8d; --ground:#eef1f5; --panel:#fff; --rule:#dbe3ec;
  --accent:#2a6099; --accent-soft:#e7effa; --accent-deep:#1c4470;
  --sheet:#fff; --grid:#dde5ef; --code-bg:#eef2f8; --code-ink:#1b3350;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
 font-size:15px;line-height:1.6;}
.wrap{max-width:1400px;margin:0 auto;padding:0 20px;}

/* light header - no heavy colour block */
.mast{background:var(--panel);border-bottom:1px solid var(--rule);padding:22px 0 20px;}
.mast .wrap{display:flex;align-items:flex-end;gap:18px;flex-wrap:wrap;}
.mast .titles{flex:1;min-width:280px;}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;
 color:var(--accent);font-weight:700;margin-bottom:6px;}
h1{margin:0 0 5px;font-size:24px;font-weight:600;letter-spacing:-.01em;}
.stand{margin:0;color:var(--dim);max-width:82ch;font-size:14px;}
.stand b{color:var(--ink);font-weight:600;}
.counts{display:flex;gap:20px;}
.counts div{text-align:right;}
.counts .n{font-size:24px;font-weight:700;color:var(--accent);line-height:1;
 font-variant-numeric:tabular-nums;}
.counts .l{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
 color:var(--dim);margin-top:3px;}

.rail{position:sticky;top:0;z-index:40;background:var(--ground);
 border-bottom:1px solid var(--rule);}
.rail .wrap{display:flex;gap:5px;flex-wrap:wrap;padding:9px 20px;}
.rail a{font-size:11.5px;font-weight:600;color:var(--accent);text-decoration:none;
 padding:5px 10px;border-radius:6px;border:1px solid transparent;}
.rail a:hover{background:var(--accent-soft);border-color:var(--rule);}
.rail a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
.rail a i{font-style:normal;color:var(--dim);font-weight:500;}
.rail a.navmark{margin-left:auto;background:var(--accent);color:#fff;
 border-color:var(--accent);}
.rail a.navmark:hover{background:var(--accent-deep);border-color:var(--accent-deep);}
.rail a.navmark i{color:rgba(255,255,255,.75);}

.main{padding-bottom:60px;}
section{margin-top:32px;scroll-margin-top:56px;}

/* ---- collapsible phases -------------------------------------------------- */
.phead{display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none;
 padding:6px 10px;margin:0 -10px 6px;border-radius:8px;}
.phead:hover{background:var(--panel);}
.phead:focus-visible{outline:2px solid var(--accent);outline-offset:1px;}
.phead h2{margin:0;}
.caret{color:var(--accent);font-size:13px;line-height:1;transition:transform .15s;}
.pcount{font-size:11.5px;color:var(--dim);font-variant-numeric:tabular-nums;}
.phint{font-size:11px;color:var(--dim);opacity:0;margin-left:auto;}
.phead:hover .phint{opacity:1;}
section.collapsed .caret{transform:rotate(-90deg);}
section.collapsed .pbody{display:none;}
section.collapsed .phead{background:var(--panel);border:1px solid var(--rule);}
.allbtn{font-family:var(--sans);font-size:11.5px;font-weight:600;cursor:pointer;
 color:var(--accent);background:transparent;border:1px solid var(--rule);
 border-radius:6px;padding:4px 10px;}
.allbtn:hover{background:var(--accent-soft);border-color:var(--accent);}
.allbtn:focus-visible{outline:2px solid var(--accent);outline-offset:1px;}
h2{font-size:14px;letter-spacing:.09em;text-transform:uppercase;color:var(--accent);
 margin:0 0 4px;font-weight:700;}
.pdesc{color:var(--dim);margin:0 0 12px;max-width:84ch;}
.gate{display:flex;flex-wrap:wrap;gap:6px 16px;background:var(--panel);
 border:1px solid var(--rule);border-radius:8px;padding:9px 14px;margin-bottom:12px;
 font-size:12.5px;}
.gate b{color:var(--accent);}
code{font-family:var(--mono);font-size:12.5px;background:var(--code-bg);
 color:var(--code-ink);padding:1px 5px;border-radius:3px;}

/* ---- draw.io style canvas ------------------------------------------------ */
.canvas{background:var(--panel);border:1px solid var(--rule);border-radius:10px;
 box-shadow:var(--shadow);overflow:hidden;}
.toolbar{display:flex;align-items:center;gap:6px;padding:7px 10px;
 border-bottom:1px solid var(--rule);background:var(--panel);flex-wrap:wrap;}
.toolbar button{font-family:var(--sans);font-size:12.5px;font-weight:600;
 color:var(--ink);background:transparent;border:1px solid var(--rule);
 border-radius:6px;padding:4px 10px;cursor:pointer;line-height:1.35;}
.toolbar button:hover{background:var(--accent-soft);border-color:var(--accent);
 color:var(--accent-deep);}
.toolbar button:focus-visible{outline:2px solid var(--accent);outline-offset:1px;}
.toolbar .zoom{font-family:var(--mono);font-size:12px;color:var(--dim);
 min-width:52px;text-align:center;font-variant-numeric:tabular-nums;}
.toolbar .spacer{flex:1;}
.toolbar .hint{font-size:11.5px;color:var(--dim);}
.viewport{position:relative;height:620px;overflow:hidden;cursor:grab;
 background-color:var(--sheet);
 background-image:radial-gradient(var(--grid) 1px,transparent 1px);
 background-size:22px 22px;
 /* without this a double-click selects the diagram text instead of fitting */
 user-select:none;-webkit-user-select:none;-ms-user-select:none;}
.viewport.grabbing{cursor:grabbing;}
.viewport:fullscreen,.canvas:fullscreen .viewport{height:100vh;}
.canvas:fullscreen{border-radius:0;}
.stage{position:absolute;top:0;left:0;transform-origin:0 0;will-change:transform;}
.stage .mermaid{margin:0;}
.stage svg{max-width:none!important;height:auto;display:block;}

/* keep every label readable whatever the page theme is doing */
/* mermaid-rendered diagrams only - the hand-drawn SVG sets its own colours */
.stage svg:not(.drawn) .nodeLabel,.stage svg:not(.drawn) .edgeLabel,
.stage svg:not(.drawn) .label,.stage svg:not(.drawn) text,
.stage svg:not(.drawn) span,.stage svg:not(.drawn) p{color:#10243a!important;
 fill:#10243a!important;}
.stage svg .edgeLabel{background:transparent!important;}
.stage svg .edgeLabel rect{fill:#fff!important;opacity:1!important;}

/* Mermaid measures each label, sizes a foreignObject to that measurement, then
   clips anything outside it. Bold runs (<b>) and any padding we add afterwards
   render WIDER than what was measured, so the last glyph was being sliced off -
   "fail" showed as "fai", "5. node1" as "5. node". Letting the foreignObject
   overflow costs nothing visually and ends the clipping. */
.stage svg foreignObject{overflow:visible!important;}
.stage svg .nodeLabel,.stage svg .edgeLabel,
.stage svg .nodeLabel div,.stage svg .edgeLabel div{overflow:visible!important;}
.stage svg .edgeLabel foreignObject div{background:#fff;border-radius:3px;
 /* visual breathing room that does NOT widen the measured box */
 box-shadow:0 0 0 3px #fff;}
.stage svg .nodeLabel b{font-weight:700;}
.stage svg .nodeLabel i{font-style:italic;opacity:.75;}

/* --- highlight / dim states driven by search and selection --------------- */
.stage svg g.node,.stage svg g.edgeLabel,.stage svg .edgePath{transition:opacity .12s;}
.stage.filtering svg g.node,.stage.filtering svg .edgePath,
.stage.filtering svg g.edgeLabel{opacity:.13;}
.stage.filtering svg g.node.hit{opacity:1;}
.stage svg g.node.hit rect,.stage svg g.node.hit polygon{stroke-width:3px!important;}
.stage svg g.node.picked rect,.stage svg g.node.picked polygon{
 stroke:#b02a6b!important;stroke-width:3.5px!important;}
.stage svg g.node{cursor:pointer;}

/* --- search box in the toolbar ------------------------------------------- */
.toolbar input.find{font-family:var(--sans);font-size:12.5px;color:var(--ink);
 background:var(--panel);border:1px solid var(--rule);border-radius:6px;
 padding:4px 9px;width:170px;}
.toolbar input.find:focus{outline:2px solid var(--accent);outline-offset:1px;
 border-color:var(--accent);}
.toolbar .found{font-size:11.5px;color:var(--dim);min-width:64px;
 font-variant-numeric:tabular-nums;}
.toolbar button.on{background:var(--accent);border-color:var(--accent);color:#fff;}

/* --- minimap -------------------------------------------------------------- */
.minimap{position:absolute;right:12px;bottom:12px;width:168px;height:126px;
 background:rgba(255,255,255,.94);border:1px solid var(--rule);border-radius:8px;
 box-shadow:var(--shadow);overflow:hidden;cursor:pointer;z-index:5;}
.minimap.hidden{display:none;}
.minimap svg{width:100%;height:100%;display:block;}
.minimap .mmview{position:absolute;border:2px solid var(--accent);
 background:rgba(42,96,153,.12);pointer-events:none;border-radius:2px;}

/* --- step detail drawer --------------------------------------------------- */
.drawer{position:absolute;top:0;right:0;width:340px;max-width:86%;height:100%;
 background:var(--panel);border-left:1px solid var(--rule);box-shadow:var(--shadow);
 transform:translateX(102%);transition:transform .18s ease;z-index:8;
 display:flex;flex-direction:column;}
.drawer.open{transform:none;}
.drawer header{display:flex;align-items:center;gap:8px;padding:10px 12px;
 border-bottom:1px solid var(--rule);}
.drawer header .n{font-family:var(--mono);font-size:11.5px;font-weight:700;
 background:var(--accent);color:#fff;border-radius:4px;padding:2px 7px;}
.drawer header .t{flex:1;font-weight:600;font-size:13px;}
.drawer header button{border:1px solid var(--rule);background:transparent;
 color:var(--ink);border-radius:6px;cursor:pointer;padding:2px 8px;font-size:14px;}
.drawer .body{padding:12px;overflow:auto;font-size:13px;}
.drawer h5{margin:14px 0 4px;font-size:10.5px;letter-spacing:.09em;
 text-transform:uppercase;color:var(--dim);}
.drawer h5:first-child{margin-top:0;}
.drawer pre{font-family:var(--mono);font-size:12px;background:var(--code-bg);
 color:var(--code-ink);padding:9px 10px;border-radius:6px;margin:0;
 white-space:pre-wrap;word-break:break-word;}
.drawer .kv{color:var(--dim);}
.drawer .kv b{color:var(--ink);font-weight:600;}
.drawer .tag{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;
 border-radius:20px;text-transform:uppercase;letter-spacing:.04em;}
.tag-stop{background:rgba(169,50,38,.14);color:#a93226;}
.tag-rb{background:rgba(180,83,9,.16);color:#b45309;}
.tag-cont{background:rgba(30,126,69,.14);color:#1e7e45;}

.hint-row{margin:8px 2px 0;font-size:12px;color:var(--dim);}
.hint-row kbd{font-family:var(--mono);font-size:11px;background:var(--code-bg);
 color:var(--code-ink);border:1px solid var(--rule);border-bottom-width:2px;
 border-radius:4px;padding:0 5px;}

.legend{display:flex;flex-wrap:wrap;gap:9px 22px;margin:16px 0 0;font-size:13.5px;
 color:var(--dim);}
.legend span{display:flex;align-items:center;gap:7px;}
.key{width:14px;height:14px;border-radius:3px;border:1.5px solid;flex:none;}
.k-cmd{background:#eaf0f7;border-color:#2a6099;}
.k-gate{background:#fdf3d8;border-color:#b7770d;}
.k-rb{background:#fbf0dd;border-color:#b45309;}
.k-stop{background:#fbe0de;border-color:#a93226;}
.k-ok{background:#e4f4ea;border-color:#1e7e45;}
.k-dep{background:#fdf7e8;border-color:#c9a24d;}
.k-skip{background:#f0f1f3;border-color:#b9c2cc;}
.k-run{background:#e8f2ea;border-color:#1e7e45;}

/* ---- auto-rollback explorer --------------------------------------------- */
.scen{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));
 gap:8px;margin:0 0 14px;}
.scenbtn{display:flex;flex-direction:column;gap:3px;align-items:flex-start;
 text-align:left;font-family:var(--sans);background:var(--panel);
 border:1px solid var(--rule);border-radius:8px;padding:9px 12px;cursor:pointer;
 color:var(--ink);}
.scenbtn:hover{border-color:var(--accent);background:var(--accent-soft);}
.scenbtn.on{border-color:var(--accent);border-left:4px solid var(--accent);
 background:var(--accent-soft);}
.scenbtn:focus-visible{outline:2px solid var(--accent);outline-offset:1px;}
.scenbtn .scl{font-size:12.5px;font-weight:600;line-height:1.4;}
.scenbtn .scl code{font-family:var(--mono);font-size:11.5px;background:transparent;
 color:var(--ink);padding:0;font-weight:400;}
.scenbtn.on .scl code{color:var(--accent-deep);}
.scnode{font-family:var(--mono);font-size:10.5px;color:var(--accent);
 background:var(--accent-soft);border-radius:3px;padding:0 4px;}
.scenbtn .scd{font-size:11.5px;color:var(--dim);line-height:1.35;}
.keyhead{font-size:12px;letter-spacing:.08em;text-transform:uppercase;
 color:var(--dim);font-weight:700;margin:22px 0 8px;}
ol.rkey{list-style:none;margin:0;padding:0;display:grid;
 grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:3px 18px;}
ol.rkey li{display:flex;align-items:baseline;gap:8px;font-size:12px;
 padding:3px 0;border-bottom:1px solid var(--rule);}
.rn{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--accent);
 min-width:30px;}
.rnode{font-family:var(--mono);font-size:10.5px;color:var(--dim);min-width:62px;}
ol.rkey code{font-family:var(--mono);font-size:11px;background:transparent;
 color:var(--ink);padding:0;word-break:break-all;}
.scenbtn .scn{font-size:11.5px;color:var(--dim);font-variant-numeric:tabular-nums;}
.scenbtn .scn b{color:var(--accent);}
.matwrap{overflow:auto;max-height:520px;border:1px solid var(--rule);
 border-radius:8px;background:var(--panel);}
table.mat{border-collapse:collapse;font-size:12px;width:100%;}
table.mat th{position:sticky;top:0;z-index:2;background:var(--panel);
 color:var(--dim);font-size:10px;font-weight:700;padding:6px 3px;
 border-bottom:1px solid var(--rule);text-align:center;}
table.mat td{padding:5px 3px;text-align:center;border-bottom:1px solid var(--rule);
 font-size:13px;line-height:1;}
table.mat .rowhead{text-align:left;font-size:11.5px;padding:5px 10px;
 white-space:nowrap;color:var(--ink);position:sticky;left:0;z-index:1;
 background:var(--panel);border-right:1px solid var(--rule);}
table.mat th.rowhead{color:var(--dim);z-index:3;}
/* ---- activity spine, laid out by hand so the column is exactly straight -- */
.spine{position:relative;padding:4px 0 4px 0;}
.spine::before{content:"";position:absolute;left:169px;top:14px;bottom:14px;
 width:2px;background:var(--rule);}
.srow{display:flex;align-items:flex-start;gap:0;margin:0 0 6px;min-width:0;}
.acard{position:relative;z-index:1;flex:0 0 340px;display:flex;align-items:baseline;
 gap:8px;background:var(--panel);border:1px solid var(--rule);border-radius:7px;
 padding:7px 10px;font-size:12px;}
.acard code{font-family:var(--mono);font-size:11px;background:transparent;
 color:var(--ink);padding:0;word-break:break-all;line-height:1.35;}
.an{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--accent);
 min-width:20px;text-align:right;flex:none;}
.anode{font-family:var(--mono);font-size:10px;color:var(--dim);flex:none;
 min-width:56px;}
.acard.arm{border-color:#c0392b;border-width:2px;background:rgba(169,50,38,.05);}
.acard.arm .an{color:#a93226;}
.srow.armed{margin-bottom:12px;}
.bwrap{flex:1;min-width:0;margin-left:12px;position:relative;}
.bwrap::before{content:"";position:absolute;left:-12px;top:18px;width:12px;
 height:2px;background:#c0392b;}
.btag{font-size:10.5px;color:#a93226;font-weight:700;letter-spacing:.03em;
 margin:0 0 4px 2px;text-transform:uppercase;}
.btag b{font-size:12px;}
.branch{display:flex;gap:7px;overflow-x:auto;padding:0 2px 6px;}
.rcard{flex:0 0 210px;background:var(--panel);border:1px solid var(--rule);
 border-radius:6px;padding:6px 9px;font-size:11px;}
.rcard code{font-family:var(--mono);font-size:10.5px;background:transparent;
 color:var(--ink);padding:0;word-break:break-all;display:block;margin-top:2px;
 line-height:1.35;}
.rcard .rh{font-size:10.5px;color:var(--ink);}
.rnode2{font-family:var(--mono);font-size:9.5px;color:var(--dim);}
.rcard.run{border-left:3px solid #1e7e45;}
.rcard.dep{border-left:3px solid #c9a24d;border-style:dashed;}
.dnote{display:block;margin-top:3px;font-size:9.5px;color:#b7770d;font-style:italic;}
@media (max-width:900px){
  .spine::before{display:none;}
  .srow{flex-direction:column;}
  .acard{flex:1 1 auto;width:100%;}
  .bwrap{margin-left:22px;}
}

/* mesh: dim everything except the isolated command's calls */
#meshcanvas .stage svg g.node{cursor:pointer;}
#meshcanvas .stage svg.isolated .edgePaths path{opacity:.05;}
#meshcanvas .stage svg.isolated .edgePaths path.lit{opacity:1;stroke:#a93226;
 stroke-width:2px;}
#meshcanvas .stage svg.isolated g.node{opacity:.18;}
#meshcanvas .stage svg.isolated g.node.lit{opacity:1;}
.c-run{color:#1e7e45;}
.c-dep{color:#b7770d;font-weight:700;}
.c-skip{color:#b9c2cc;}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--rule);
 font-size:12.5px;color:var(--dim);}
@media (prefers-reduced-motion:reduce){*{transition:none!important;}}
</style>"""


PANZOOM = """<script>
(function () {
  var MIN = 0.15, MAX = 4;
  var INFO = window.STEP_INFO || {};

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c];
    });
  }

  function setup(canvas) {
    var viewport = canvas.querySelector('.viewport');
    var stage    = canvas.querySelector('.stage');
    var readout  = canvas.querySelector('.zoom');
    var svg      = stage.querySelector('svg');
    var s = { k: 1, x: 20, y: 20 };
    var nat = { w: 0, h: 0 };

    if (svg) {                       // measure once, at scale 1, before any transform
      var b = svg.getBoundingClientRect();
      nat = { w: b.width, h: b.height };
    }

    var mm = canvas.querySelector('.minimap');
    var mmView = mm && mm.querySelector('.mmview');

    function apply() {
      stage.style.transform = 'translate(' + s.x + 'px,' + s.y + 'px) scale(' + s.k + ')';
      if (readout) readout.textContent = Math.round(s.k * 100) + '%';
      drawMinimap();
    }
    function drawMinimap() {
      if (!mm || !mmView || !nat.w) return;
      var r = viewport.getBoundingClientRect();
      var mb = mm.getBoundingClientRect();
      var f = Math.min(mb.width / nat.w, mb.height / nat.h);
      var ox = (mb.width - nat.w * f) / 2, oy = (mb.height - nat.h * f) / 2;
      mmView.style.left   = (ox + (-s.x / s.k) * f) + 'px';
      mmView.style.top    = (oy + (-s.y / s.k) * f) + 'px';
      mmView.style.width  = Math.max(6, (r.width  / s.k) * f) + 'px';
      mmView.style.height = Math.max(6, (r.height / s.k) * f) + 'px';
    }
    function zoomAt(cx, cy, factor) {
      var k = Math.min(MAX, Math.max(MIN, s.k * factor));
      var ratio = k / s.k;
      s.x = cx - (cx - s.x) * ratio;
      s.y = cy - (cy - s.y) * ratio;
      s.k = k;
      apply();
    }
    function zoomCentre(factor) {
      var r = viewport.getBoundingClientRect();
      zoomAt(r.width / 2, r.height / 2, factor);
    }
    function fit() {
      var r = viewport.getBoundingClientRect();
      if (!nat.w || !nat.h) return;
      var k = Math.min((r.width - 40) / nat.w, (r.height - 40) / nat.h);
      s.k = Math.min(MAX, Math.max(MIN, k));
      s.x = (r.width - nat.w * s.k) / 2;
      s.y = 20;
      apply();
    }
    function open() {
      // Fitting a 100-step phase into 620px lands near 15% - legible to nobody.
      // These flows read top-down, so open at actual size and only scale back if
      // the diagram is too WIDE for the viewport. Vertical is what panning is for,
      // and Fit is one click (or 'f') away.
      var r = viewport.getBoundingClientRect();
      s.k = Math.min(1, (r.width - 40) / nat.w);
      s.x = Math.max(20, (r.width - nat.w * s.k) / 2);
      s.y = 20;
      apply();
    }
    function reset() { s.k = 1; s.x = 20; s.y = 20; apply(); }

    function centreOn(el) {
      var sb = stage.getBoundingClientRect(), eb = el.getBoundingClientRect();
      var cx = (eb.left + eb.width / 2 - sb.left) / s.k;
      var cy = (eb.top + eb.height / 2 - sb.top) / s.k;
      var r = viewport.getBoundingClientRect();
      s.x = r.width / 2 - cx * s.k;
      s.y = r.height / 2 - cy * s.k;
      apply();
    }

    // ---- pan / zoom -------------------------------------------------------
    viewport.addEventListener('wheel', function (e) {
      e.preventDefault();
      var r = viewport.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 1 / 1.12);
    }, { passive: false });

    var dragging = false, moved = 0, px = 0, py = 0;
    viewport.addEventListener('pointerdown', function (e) {
      if (e.target.closest('.drawer') || e.target.closest('.minimap')) return;
      dragging = true; moved = 0; px = e.clientX; py = e.clientY;
      viewport.classList.add('grabbing');
      viewport.setPointerCapture(e.pointerId);
    });
    viewport.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      var dx = e.clientX - px, dy = e.clientY - py;
      moved += Math.abs(dx) + Math.abs(dy);
      s.x += dx; s.y += dy; px = e.clientX; py = e.clientY; apply();
    });
    function stopDrag(e) {
      dragging = false; viewport.classList.remove('grabbing');
      try { viewport.releasePointerCapture(e.pointerId); } catch (_) {}
    }
    viewport.addEventListener('pointerup', stopDrag);
    viewport.addEventListener('pointercancel', stopDrag);
    viewport.addEventListener('dblclick', function (e) {
      e.preventDefault();
      if (window.getSelection) window.getSelection().removeAllRanges();
      fit();
    });
    viewport.addEventListener('selectstart', function (e) { e.preventDefault(); });

    // ---- click a command box for its detail -------------------------------
    var drawer = canvas.querySelector('.drawer');
    function closeDrawer() {
      if (drawer) drawer.classList.remove('open');
      stage.querySelectorAll('g.node.picked').forEach(function (n) {
        n.classList.remove('picked');
      });
    }
    function showStep(id, g) {
      var d = INFO[id];
      if (!d || !drawer) return;
      stage.querySelectorAll('g.node.picked').forEach(function (n) {
        n.classList.remove('picked');
      });
      g.classList.add('picked');
      var tag = d.fail === 'stop' ? '<span class="tag tag-stop">stops the run</span>'
              : d.fail === 'rb'   ? '<span class="tag tag-rb">arms rollback</span>'
              :                     '<span class="tag tag-cont">run continues</span>';
      var h = '<h5>Command</h5><pre>' + esc(d.cmd) + '</pre>';
      h += '<h5>Target</h5><p class="kv"><b>' + esc(d.node) + '</b>' +
           (d.loop ? ' · inside a retry loop' : '') + '</p>';
      if (d.when)  h += '<h5>Runs when</h5><pre>' + esc(d.when) + '</pre>';
      if (d.skip)  h += '<h5>Skipped when</h5><pre>' + esc(d.skip) + '</pre>';
      if (d.crit)  h += '<h5>Passes if</h5><pre>' + esc(d.crit) + '</pre>';
      if (d.sets && d.sets.length) {
        h += '<h5>On failure sets</h5><p class="kv">' +
             d.sets.map(function (x) { return '<b>' + esc(x) + '</b>'; }).join('<br>') + '</p>';
      }
      h += '<h5>On failure</h5><p class="kv">' + tag +
           ' <span style="margin-left:6px">' + esc(d.failtext || '') + '</span></p>';
      drawer.querySelector('.n').textContent = d.n;
      drawer.querySelector('.t').textContent = d.desc || d.node;
      drawer.querySelector('.body').innerHTML = h;
      drawer.classList.add('open');
    }
    stage.addEventListener('click', function (e) {
      if (moved > 4) return;                       // that was a pan, not a click
      var g = e.target.closest('g.node');
      if (!g) return;
      var id = (g.id || '').replace(/^flowchart-/, '').replace(/-\\d+$/, '');
      if (INFO[id]) showStep(id, g); else closeDrawer();
    });
    if (drawer) {
      drawer.querySelector('button').addEventListener('click', closeDrawer);
    }

    // ---- search ------------------------------------------------------------
    var find = canvas.querySelector('input.find');
    var found = canvas.querySelector('.found');
    var hits = [], at = -1;
    function runFind() {
      var q = (find.value || '').trim().toLowerCase();
      stage.querySelectorAll('g.node.hit').forEach(function (n) { n.classList.remove('hit'); });
      hits = []; at = -1;
      if (!q) {
        stage.classList.remove('filtering');
        found.textContent = '';
        return;
      }
      stage.querySelectorAll('g.node').forEach(function (g) {
        // The visible label is wrapped and may be elided, so searching only the
        // rendered text misses the tail of long commands. Match the full command
        // and description from STEP_INFO as well.
        var id = (g.id || '').replace(/^flowchart-/, '').replace(/-\\d+$/, '');
        var d = INFO[id];
        var hay = (g.textContent || '');
        if (d) hay += ' ' + d.cmd + ' ' + d.desc + ' ' + d.node;
        if (hay.toLowerCase().indexOf(q) >= 0) {
          g.classList.add('hit'); hits.push(g);
        }
      });
      stage.classList.add('filtering');
      found.textContent = hits.length ? ('1/' + hits.length) : 'no match';
      if (hits.length) { at = 0; centreOn(hits[0]); }
    }
    function step(dir) {
      if (!hits.length) return;
      at = (at + dir + hits.length) % hits.length;
      found.textContent = (at + 1) + '/' + hits.length;
      centreOn(hits[at]);
    }
    if (find) {
      var t;
      find.addEventListener('input', function () {
        clearTimeout(t); t = setTimeout(runFind, 160);
      });
      find.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
        if (e.key === 'Escape') { find.value = ''; runFind(); find.blur(); }
      });
    }

    // ---- minimap ------------------------------------------------------------
    if (mm && svg) {
      var clone = svg.cloneNode(true);
      clone.removeAttribute('id');
      clone.setAttribute('width', '100%');
      clone.setAttribute('height', '100%');
      clone.setAttribute('viewBox', '0 0 ' + nat.w + ' ' + nat.h);
      clone.setAttribute('preserveAspectRatio', 'xMidYMid meet');
      mm.insertBefore(clone, mmView);
      mm.addEventListener('click', function (e) {
        var mb = mm.getBoundingClientRect();
        var f = Math.min(mb.width / nat.w, mb.height / nat.h);
        var ox = (mb.width - nat.w * f) / 2, oy = (mb.height - nat.h * f) / 2;
        var dx = (e.clientX - mb.left - ox) / f, dy = (e.clientY - mb.top - oy) / f;
        var r = viewport.getBoundingClientRect();
        s.x = r.width / 2 - dx * s.k;
        s.y = r.height / 2 - dy * s.k;
        apply();
      });
    }

    // ---- export --------------------------------------------------------------
    function download(href, name) {
      var a = document.createElement('a');
      a.href = href; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
    }
    function svgText() {
      var c = svg.cloneNode(true);
      c.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
      c.setAttribute('width', nat.w); c.setAttribute('height', nat.h);
      var st = document.createElementNS('http://www.w3.org/2000/svg', 'style');
      st.textContent = 'text,span,div,p{font-family:Segoe UI,system-ui,sans-serif;' +
                       'color:#10243a;fill:#10243a;}';
      c.insertBefore(st, c.firstChild);
      return new XMLSerializer().serializeToString(c);
    }
    var base = (canvas.getAttribute('data-name') || 'phase').replace(/[^\\w.-]+/g, '_');
    function exportSvg() {
      download('data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgText()),
               base + '.svg');
    }
    function exportPng() {
      var scale = 2, img = new Image();
      img.onload = function () {
        var cv = document.createElement('canvas');
        cv.width = nat.w * scale; cv.height = nat.h * scale;
        var ctx = cv.getContext('2d');
        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, cv.width, cv.height);
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        ctx.drawImage(img, 0, 0);
        try { download(cv.toDataURL('image/png'), base + '.png'); }
        catch (err) { console.error('png export failed', err); exportSvg(); }
      };
      img.onerror = function () { exportSvg(); };
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgText());
    }

    // ---- toolbar -------------------------------------------------------------
    canvas.querySelectorAll('button[data-act]').forEach(function (b) {
      b.addEventListener('click', function () {
        var a = b.getAttribute('data-act');
        if (a === 'in')    zoomCentre(1.25);
        if (a === 'out')   zoomCentre(1 / 1.25);
        if (a === 'fit')   fit();
        if (a === 'reset') reset();
        if (a === 'svg')   exportSvg();
        if (a === 'png')   exportPng();
        if (a === 'map') {
          if (mm) { mm.classList.toggle('hidden'); b.classList.toggle('on', !mm.classList.contains('hidden')); }
        }
        if (a === 'full') {
          if (document.fullscreenElement) document.exitFullscreen();
          else if (canvas.requestFullscreen) canvas.requestFullscreen();
        }
      });
    });
    document.addEventListener('fullscreenchange', function () {
      if (document.fullscreenElement === canvas) setTimeout(fit, 60);
    });

    // ---- keyboard (only while the pointer is over this canvas) --------------
    var hot = false;
    canvas.addEventListener('mouseenter', function () { hot = true; });
    canvas.addEventListener('mouseleave', function () { hot = false; });
    document.addEventListener('keydown', function (e) {
      if (!hot) return;
      var typing = /^(INPUT|TEXTAREA)$/.test((e.target.tagName || ''));
      if (e.key === 'Escape') { closeDrawer(); if (find) { find.value = ''; runFind(); } return; }
      if (typing) return;
      if (e.key === '/') { e.preventDefault(); if (find) find.focus(); }
      if (e.key === '+' || e.key === '=') zoomCentre(1.25);
      if (e.key === '-') zoomCentre(1 / 1.25);
      if (e.key === '0') reset();
      if (e.key === 'f') fit();
      if (e.key === 'n') step(1);
      if (e.key === 'p') step(-1);
    });

    open();
    window.addEventListener('resize', drawMinimap);
    // exposed so a canvas whose content is swapped in later can re-open + re-index
    canvas.__pz = { fit: fit, open: open,
                    refresh: function () { open(); drawMinimap(); } };
  }

  /* ---- auto-rollback explorer ------------------------------------------- */
  function rollback() {
    var holder = document.getElementById('rbdata');
    var canvas = document.getElementById('rbcanvas');
    if (!holder || !canvas || !window.mermaid) return;
    var data;
    try { data = JSON.parse(holder.textContent); } catch (e) { return; }
    var stage = canvas.querySelector('.stage');
    var why = document.getElementById('rbwhy');
    var seq = 0;

    function show(key, btn) {
      var sc = data[key];
      if (!sc) return;
      document.querySelectorAll('.scenbtn').forEach(function (b) {
        b.classList.toggle('on', b === btn);
      });
      if (why) why.textContent = sc.why || '';
      stage.innerHTML = '<div style="padding:16px;color:#5b6b7c">rendering…</div>';
      mermaid.render('rbsvg' + (seq++), sc.m).then(function (r) {
        stage.innerHTML = r.svg;
        if (canvas.__pz) canvas.__pz.refresh();
      }).catch(function (e) {
        stage.innerHTML = '<div style="padding:16px;color:#a93226">could not render: '
          + String((e && e.message) || e) + '</div>';
        console.error('rollback diagram failed', e);
      });
    }

    var buttons = document.querySelectorAll('.scenbtn');
    buttons.forEach(function (b) {
      b.addEventListener('click', function () { show(b.getAttribute('data-sc'), b); });
    });
    if (buttons.length) show(buttons[0].getAttribute('data-sc'), buttons[0]);
  }

  /* ---- collapsible phases ----------------------------------------------- */
  function collapsing() {
    function setState(sec, collapsed) {
      sec.classList.toggle('collapsed', collapsed);
      var head = sec.querySelector('.phead');
      if (head) head.setAttribute('aria-expanded', String(!collapsed));
      var hint = sec.querySelector('.phint');
      if (hint) hint.textContent = collapsed ? 'click to expand' : 'click to collapse';
      if (!collapsed) {
        // it was laid out at zero size while hidden - measure and re-fit now
        sec.querySelectorAll('.canvas').forEach(function (c) {
          if (c.__pz) setTimeout(function () { c.__pz.refresh(); }, 0);
        });
      }
    }
    document.querySelectorAll('section.phase').forEach(function (sec) {
      var head = sec.querySelector('.phead');
      if (!head) return;
      head.addEventListener('click', function () {
        setState(sec, !sec.classList.contains('collapsed'));
      });
      head.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          setState(sec, !sec.classList.contains('collapsed'));
        }
      });
    });
    function all(collapsed) {
      document.querySelectorAll('section.phase').forEach(function (sec) {
        setState(sec, collapsed);
      });
    }
    var c = document.getElementById('collapseAll');
    var x = document.getElementById('expandAll');
    if (c) c.addEventListener('click', function () { all(true); });
    if (x) x.addEventListener('click', function () { all(false); });

    // opening a section from the nav must expand it first
    document.querySelectorAll('.rail a[href^="#"]').forEach(function (a) {
      a.addEventListener('click', function () {
        var sec = document.querySelector(a.getAttribute('href'));
        if (sec && sec.classList.contains('collapsed')) setState(sec, false);
      });
    });
  }

  /* ---- complete mesh: click a command to isolate its rollback ----------- */
  function mesh() {
    var holder = document.getElementById('meshdata');
    var canvas = document.getElementById('meshcanvas');
    if (!holder || !canvas) return;
    var map;
    try { map = JSON.parse(holder.textContent); } catch (e) { return; }
    var stage = canvas.querySelector('.stage');
    var why = document.getElementById('meshwhy');
    var svg = stage.querySelector('svg');
    if (!svg) return;

    var edges = svg.querySelectorAll('.edgePaths path, g.edgePaths > path');
    var current = null;

    function clear() {
      current = null;
      svg.classList.remove('isolated');
      edges.forEach(function (p) { p.classList.remove('lit'); });
      svg.querySelectorAll('g.node').forEach(function (n) { n.classList.remove('lit'); });
      if (why) why.textContent = 'click a command on the left to isolate its rollback';
    }

    function isolate(actId, gnode) {
      var d = map[actId];
      if (!d) return;
      if (current === actId) { clear(); return; }
      clear();
      current = actId;
      svg.classList.add('isolated');
      d.edges.forEach(function (i) { if (edges[i]) edges[i].classList.add('lit'); });
      if (gnode) gnode.classList.add('lit');
      svg.querySelectorAll('g.node').forEach(function (n) {
        var id = (n.id || '').replace(/^flowchart-/, '').replace(/-\\d+$/, '');
        if (d.rnodes.indexOf(id) >= 0) n.classList.add('lit');
      });
      if (why) {
        why.textContent = 'activity command ' + actId + ' → ' + d.rnodes.length
          + ' rollback steps: ' + d.rnodes.join(', ');
      }
    }

    stage.addEventListener('click', function (e) {
      var g = e.target.closest('g.node');
      if (!g) { clear(); return; }
      var id = (g.id || '').replace(/^flowchart-/, '').replace(/-\\d+$/, '');
      var m = /^A(\\d+)$/.exec(id);
      if (m) isolate(m[1], g); else clear();
    });
  }

  function start() {
    document.querySelectorAll('.canvas').forEach(setup);
    rollback();
    collapsing();
    mesh();
  }

  if (window.mermaid) {
    mermaid.initialize({ startOnLoad: false, securityLevel: 'loose',
                         flowchart: { useMaxWidth: false, htmlLabels: true,
                                      curve: 'basis',
                                      padding: 14, nodeSpacing: 52, rankSpacing: 64,
                                      diagramPadding: 26 } });
    mermaid.run({ querySelector: '.mermaid' }).then(start).catch(function (e) {
      console.error('mermaid render failed', e); start();
    });
  } else {
    document.addEventListener('DOMContentLoaded', start);
  }
})();
</script>"""


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python gen_command_diagram.py <workflow.yaml> <output.html> "
                 "[mermaid.min.js]")
    src, dst = sys.argv[1], sys.argv[2]
    merm_path = sys.argv[3] if len(sys.argv) > 3 else None
    if merm_path is None:                      # default: mermaid.min.js next to this script
        sibling = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mermaid.min.js")
        merm_path = sibling if os.path.exists(sibling) else None

    with open(src, encoding="utf-8", errors="replace") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict) or not doc.get("phases"):
        sys.exit(f"{src}: not a CLI-CR workflow (no top-level 'phases:' mapping)")

    # a step with no on_failure of its own inherits this
    default_on_failure = (((doc.get("globals") or {}).get("defaults") or {})
                          .get("on_failure") or "stop")

    ROLLBACK_GLOBALS.update(doc.get("globals") or {})

    phases = OrderedDict()
    for pid, p in (doc.get("phases") or {}).items():
        if isinstance(p, dict):
            phases[pid] = (p, list(flatten(p.get("steps"))))

    out = [STYLE]
    A = out.append
    total = sum(len(r) for _p, r in phases.values())
    name = src.replace("\\", "/").split("/")[-1]

    A('<header class="mast"><div class="wrap">')
    A('<div class="titles">')
    A('<div class="eyebrow">CLI-CR workflow · generated from the YAML</div>')
    A("<h1>Command flow diagram</h1>")
    A(f'<p class="stand">Every command in <b>{html.escape(name)}</b> as a box, wired in '
      "execution order. Drag to pan, scroll to zoom, double-click to fit.</p>")
    A("</div>")
    A('<div class="counts">')
    A(f'<div><div class="n">{total}</div><div class="l">commands</div></div>')
    A(f'<div><div class="n">{len(phases)}</div><div class="l">phases</div></div>')
    A("</div></div></header>")

    A('<nav class="rail"><div class="wrap">')
    for pid, (p, rows) in phases.items():
        A(f'<a href="#{pid}">{html.escape(p.get("name", pid))} <i>{len(rows)}</i></a>')
    # the explorer sits below five tall canvases - it needs its own way in
    n_arm = len(arming_points(next((rows for _pid, (p, rows) in phases.items()
                                    if p.get("name") == "ACTIVITY_CONFIGURATION"), [])))
    if n_arm:
        A(f'<a href="#autorollback" class="navmark">&#8630; Auto-rollback explorer '
          f"<i>{n_arm}</i></a>")
    A('<button class="allbtn" id="collapseAll">Collapse all</button>')
    A('<button class="allbtn" id="expandAll">Expand all</button>')
    A("</div></nav>")

    A('<div class="wrap main">')
    info = {}
    A('<div class="legend" style="margin-top:20px">'
      '<span><i class="key k-cmd"></i> command sent to a node</span>'
      '<span><i class="key k-gate"></i> condition the engine evaluates</span>'
      '<span><i class="key k-rb"></i> arms the rollback</span>'
      '<span><i class="key k-stop"></i> stops the run</span></div>')

    for pid, (p, rows) in phases.items():
        pname = p.get("name", pid)
        A(f'<section id="{pid}" class="phase">')
        A('<div class="phead" role="button" tabindex="0" aria-expanded="true">'
          '<span class="caret">&#9662;</span>'
          f'<h2>{html.escape(pname)}</h2>'
          f'<span class="pcount">{len(rows)} commands</span>'
          '<span class="phint">click to collapse</span></div>')
        A('<div class="pbody">')
        if p.get("description"):
            A(f'<p class="pdesc">{html.escape(str(p["description"]))}</p>')
        A('<div class="gate">')
        A(f'<span><b>phase runs when</b> <code>'
          f'{html.escape(str(p.get("when") or "always"))}</code></span>')
        if p.get("on_failure"):
            A(f'<span><b>on_failure</b> <code>{html.escape(str(p["on_failure"]))}</code></span>')
        A(f"<span><b>commands</b> {len(rows)}</span>")
        A("</div>")

        A(f'<div class="canvas" data-name="{html.escape(pname)}">')
        A('<div class="toolbar">')
        A('<button data-act="out" title="Zoom out (-)">&minus;</button>')
        A('<span class="zoom">100%</span>')
        A('<button data-act="in" title="Zoom in (+)">&plus;</button>')
        A('<button data-act="fit" title="Fit to window (f)">Fit</button>')
        A('<button data-act="reset" title="Actual size (0)">100%</button>')
        A('<input class="find" type="search" placeholder="Find a command  /" '
          'aria-label="Find a command">')
        A('<span class="found"></span>')
        A('<span class="spacer"></span>')
        A('<button data-act="map" class="on" title="Toggle minimap">Map</button>')
        A('<button data-act="svg" title="Download this phase as SVG">SVG</button>')
        A('<button data-act="png" title="Download this phase as PNG">PNG</button>')
        A('<button data-act="full" title="Fullscreen">Fullscreen</button>')
        A("</div>")
        A('<div class="viewport"><div class="stage"><pre class="mermaid">')
        A(phase_diagram(pname, rows, pid, default_on_failure))
        A("</pre></div>")
        A('<div class="minimap"><div class="mmview"></div></div>')
        A('<aside class="drawer"><header><span class="n"></span>'
          '<span class="t"></span><button title="Close">&times;</button></header>'
          '<div class="body"></div></aside>')
        A("</div>")
        A('<p class="hint-row">click any command for its full detail · drag to pan · '
          "scroll to zoom · <kbd>/</kbd> find · <kbd>f</kbd> fit · <kbd>0</kbd> 100% · "
          "<kbd>Esc</kbd> clear</p>")
        A("</div>")        # /.canvas
        A("</div>")        # /.pbody
        A("</section>")
        info.update(step_info(rows, pid, default_on_failure))

    A(rollback_section(phases))

    A(f'<footer>Generated from <code>{html.escape(src)}</code> · {total} commands · '
      "re-run gen_command_diagram.py after any YAML change.</footer>")
    A("</div>")

    script = ""
    if merm_path:
        with open(merm_path, encoding="utf-8", errors="replace") as fh:
            script = f"<script>\n{fh.read()}\n</script>\n"
    # </script> inside a JSON string would end the tag early
    payload = json.dumps(info, ensure_ascii=False).replace("</", "<\\/")
    script += f"<script>window.STEP_INFO = {payload};</script>\n"
    script += PANZOOM

    page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>Command flow — {html.escape(name)}</title>\n</head>\n<body>\n"
            + "\n".join(out) + "\n" + script + "\n</body>\n</html>\n")
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(page)

    print(f"wrote {dst}")
    print(f"  phases: {len(phases)}   commands: {total}")
    for pid, (p, rows) in phases.items():
        print(f"    {p.get('name', pid):<26} {len(rows):>3}")


if __name__ == "__main__":
    main()
