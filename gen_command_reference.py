"""Generate a line-by-line command reference from a CLI-CR workflow YAML.

    python gen_command_reference.py <workflow.yaml> <output.html>

For every step, in execution order, it documents:
  * the exact command sent and the node it goes to
  * when it runs  (phase `when`, step `when` / `skip_when`)
  * its prerequisites - which variables the run condition depends on, and which
    earlier step actually assigns each of them
  * what it captures (`register`) and what it decides (`validation.criteria`)
  * every variable each branch writes, and the timeout / retry / on_failure policy

Nothing is hand-transcribed: re-run it whenever the YAML changes.
"""

import html
import re
import sys
from collections import OrderedDict, defaultdict

import yaml

# identifiers that appear in expressions but are values, not variables
LITERALS = {
    "true", "false", "SUCCESS", "FAILED", "NA", "and", "or", "not", "null", "None",
    # engine comparison operators (ExecutionContext.evaluateCondition) and the
    # ${base64:VAR} prefix - words, but never variable names
    "contains", "notContains", "startsWith", "notStartsWith", "matches", "base64",
}

# Values that exist before the workflow starts: injected by the engine's selector
# map (MopExecutionUtil / SimRunner) or read from the CIQ JSON. They are not
# assigned anywhere in the YAML, and that is correct.
EXTERNAL = {
    "ROLLBACK_ONLY", "PARENT_REQ_ID", "ORDER_NO", "CR_NAME", "currentCrGroup",
    "NIAM_IP", "M2MPORT", "M2MUSER", "M2MPASSWORD",
    "REPO_IP", "REPO_USER", "REPO_PASSWORD",
    "NODE1_NAME", "NODE2_NAME", "NODE1_NIAM_NAME", "NODE2_NIAM_NAME",
    "NEW_FILE_NAME", "NEW_VERSION", "TAC_COUNT", "Group", "crGroup", "email",
    "GITLAB_CLIENT_ID", "GITLAB_CLIENT_SECRET", "GITLAB_PROJECT_ID",
    "ALERT_EMAIL_TO", "ALERT_EMAIL_CC", "nodeName", "node",
    # standard MOP_EXECUTION arglist keys - present for every workflow, not just DPA
    "NODE_NAME", "NODE_TYPE", "SUB_ACTIVITY_NAME", "CHILD_REQ_ID", "CR_GROUP",
    "INPUT_JSON_FILE_NAME", "OUTPUT_LOGS_FILE_LOCATION", "OUTPUT_JSON_REPORT_LOCATION",
    "SSH_PORT", "NIAM_PORT", "USER", "repo_server_ip",
}


def var_tokens(expr):
    """Variable names referenced by a workflow expression."""
    if not expr:
        return []
    s = str(expr)
    s = re.sub(r'"[^"]*"', " ", s)          # drop double-quoted literals
    s = re.sub(r"'[^']*'", " ", s)          # drop single-quoted literals
    out = []
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", s):
        t = m.group(0)
        if t not in LITERALS and t not in out:
            out.append(t)
    return out


CONDITION_KEYS = ("when", "skip_when", "break_when", "expr")


def loop_item_vars(steps):
    """Names bound by `type: loop` bodies (`item_var`), recursively.

    The loop binds these for the steps inside it. Without this they look like
    variables nothing ever assigns, which buries the real findings in noise.
    """
    found = set()
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        if st.get("type") == "loop":
            if st.get("item_var"):
                found.add(str(st["item_var"]))
            if st.get("index_var"):
                found.add(str(st["index_var"]))
            found |= loop_item_vars(st.get("steps"))
        else:
            for key in ("steps", "then", "else"):
                if isinstance(st.get(key), list):
                    found |= loop_item_vars(st[key])
    return found


def read_tokens(obj):
    """Every variable a step *reads*, from anywhere in it.

    Reads hide in more places than the run condition: `node: ${ACTIVE_NODE}`,
    the command text, validation messages, register values, rest bodies. Walking
    the whole step and pulling every ${...} - plus the condition keys, whose
    contents are expressions - is the only way to get this right. Scanning a
    fixed list of fields silently under-counts and makes live variables look dead.
    """
    found = set()

    def walk(o, in_condition=False):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, in_condition or k in CONDITION_KEYS)
        elif isinstance(o, list):
            for v in o:
                walk(v, in_condition)
        elif o is not None:
            s = str(o)
            for m in re.finditer(r"\$\{([^}]*)\}", s):
                found.update(var_tokens(m.group(1)))
            if in_condition and "${" not in s:
                found.update(var_tokens(s))

    walk(obj)
    return found


def flatten(steps, phase, depth=0, loop=None):
    """Yield steps in execution order, expanding loop bodies once."""
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        if st.get("type") == "loop":
            label = (f"loop over {st.get('for_each','')} as {st.get('item_var','')}"
                     f" | when {st.get('when','-')} | break_when {st.get('break_when','-')}")
            for sub in flatten(st.get("steps"), phase, depth + 1, label):
                yield sub
        else:
            yield {"step": st, "phase": phase, "depth": depth, "loop": loop}


def collect(doc):
    phases = OrderedDict()
    for pid, p in (doc.get("phases") or {}).items():
        if not isinstance(p, dict):
            continue
        phases[pid] = {
            "id": pid,
            "name": p.get("name", pid),
            "description": p.get("description", ""),
            "when": p.get("when"),
            "on_failure": p.get("on_failure"),
            "then": p.get("then"),
            "rows": list(flatten(p.get("steps"), p.get("name", pid))),
        }
    return phases


def validation_vars(val):
    """[(branch, message, {var: value})] for a validation block."""
    out = []
    if not isinstance(val, dict):
        return out
    for branch in ("success", "warning", "failure"):
        b = val.get(branch)
        if isinstance(b, dict):
            out.append((branch, b.get("message", ""), b.get("vars") or {}))
    return out


def criteria_text(val):
    if not isinstance(val, dict):
        return ""
    s = val.get("success")
    if not isinstance(s, dict):
        return ""
    c = s.get("criteria")
    if not isinstance(c, dict):
        return ""
    if "expr" in c:
        return str(c["expr"])
    for key in ("all", "any"):
        if key in c:
            parts = []
            for item in c[key] or []:
                if isinstance(item, dict):
                    parts += [f"{k}: {v}" for k, v in item.items()]
                else:
                    parts.append(str(item))
            return f"{key} of [ " + " ; ".join(parts) + " ]"
    return ""


def build_index(phases, doc=None):
    """variable -> list of (phase, idx, how) that assign it.

    Index 0 is used for values that exist before any step runs: workflow
    globals, and values injected at runtime from the CIQ JSON / arglist.
    """
    setters = defaultdict(list)

    for v in ((doc or {}).get("globals") or {}).get("vars") or {}:
        setters[v].append(("globals", 0, "workflow default"))
    for v in EXTERNAL:
        setters[v].append(("runtime", 0, "supplied by arglist / CIQ"))
    # a phase block may seed variables before any of its steps run
    for pid, p in ((doc or {}).get("phases") or {}).items():
        if isinstance(p, dict):
            for v in (p.get("vars") or {}):
                setters[v].append((p.get("name", pid), 0, "phase var"))
            for v in loop_item_vars(p.get("steps")):
                setters[v].append((p.get("name", pid), 0, "loop variable"))

    n = 0
    for p in phases.values():
        for row in p["rows"]:
            n += 1
            row["idx"] = n
            st = row["step"]
            for r in st.get("register") or []:
                if not isinstance(r, dict):
                    continue
                if r.get("name"):
                    setters[r["name"]].append((p["name"], n, "register"))
                for m in re.finditer(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>", str(r.get("regex", ""))):
                    setters[m.group(1)].append((p["name"], n, "capture"))
            for branch, _msg, vs in validation_vars(st.get("validation")):
                for v in vs:
                    setters[v].append((p["name"], n, f"validation {branch}"))
    return setters


def audit(phases, setters):
    """Cross-check every variable read against every variable written.

    Three things go wrong in a hand-edited workflow, and all three are invisible
    when reading it top-to-bottom:

      never-assigned   a step gates on a variable nothing ever writes, so the
                       condition is permanently false and the step never runs
      read-before-set  the only writer runs later than the reader
      write-only       something is written that nothing reads

    A never-assigned name paired with a write-only near-match is almost always
    one typo (e.g. FOO written, FOO2 read), so they are reported together.
    """
    first_read = {}
    reads_at = defaultdict(list)
    for p in phases.values():
        for row in p["rows"]:
            idx = row["idx"]
            for v in read_tokens(row["step"]):
                reads_at[v].append((p["name"], idx))
                if v not in first_read:
                    first_read[v] = (p["name"], idx)

    written = {v: [s for s in l if s[1] > 0] for v, l in setters.items()}
    preset = {v for v, l in setters.items() if any(i == 0 for _p, i, _h in l)}

    never, late, writeonly = [], [], []
    for v, places in sorted(reads_at.items()):
        if v in preset:
            continue
        w = written.get(v) or []
        if not w:
            never.append((v, places))
        elif min(i for _p, i, _h in w) > places[0][1]:
            late.append((v, places[0], min(w, key=lambda s: s[1])))

    for v, w in sorted(written.items()):
        if w and v not in reads_at:
            writeonly.append((v, w))

    # pair each never-assigned name with a plausible write-only typo source
    import difflib
    wo_names = [v for v, _w in writeonly]
    twins = {}
    for v, _places in never:
        match = difflib.get_close_matches(v, wo_names, n=1, cutoff=0.82)
        if match:
            twins[v] = match[0]

    return {"never": never, "late": late, "writeonly": writeonly, "twins": twins}


def render_findings(f):
    """The findings panel. Empty sections are omitted rather than shown green."""
    n_err = len(f["never"]) + len(f["late"])
    out = []
    A = out.append
    A('<section id="findings"><h2>Findings</h2>')
    A('<p class="pdesc">Automatic cross-check of every variable read against every '
      "variable written. These are the mistakes that a top-to-bottom read of the "
      "YAML will not reveal.</p>")

    if not n_err and not f["writeonly"]:
        A('<div class="finding ok"><b>No variable-wiring problems found.</b> '
          "Every condition depends on something that is assigned before it runs.</div>")
        A("</section>")
        return "\n".join(out)

    for v, places in f["never"]:
        where = ", ".join(f'<a href="#s{i}">step {i}</a> ({ph})' for ph, i in places[:6])
        twin = f["twins"].get(v)
        hint = ""
        if twin:
            hint = (f'<div class="hint">Nothing reads <code>{esc(twin)}</code>, which is '
                    f"written but never used. <b>These two look like the same variable "
                    f"with a typo</b> — one of the two names is wrong.</div>")
        A(f'<div class="finding err"><b>Never assigned:</b> <code>{esc(v)}</code> is read at '
          f"{where}, but nothing in this workflow ever writes it — it is always empty. "
          f"A <code>when</code> that tests it never opens; a validation that tests it never "
          f"passes.{hint}</div>")

    for v, (rph, ridx), (wph, widx, how) in f["late"]:
        A(f'<div class="finding err"><b>Read before it is set:</b> <code>{esc(v)}</code> is '
          f'read at <a href="#s{ridx}">step {ridx}</a> ({esc(rph)}) but only written later, '
          f'at <a href="#s{widx}">step {widx}</a> ({esc(wph)}, {esc(how)}). '
          f"It is empty when the earlier step reads it.</div>")

    if f["writeonly"]:
        items = ", ".join(
            f'<code>{esc(v)}</code> <span class="dim">(step {w[0][1]})</span>'
            for v, w in f["writeonly"][:40])
        more = "" if len(f["writeonly"]) <= 40 else f" …and {len(f['writeonly']) - 40} more"
        A(f'<div class="finding warn"><b>Written but never read ({len(f["writeonly"])}):</b> '
          f"{items}{more}. Usually harmless — a value captured for the report — but a name "
          f"here that should have been used is how a typo hides.</div>")

    A("</section>")
    return "\n".join(out)


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def render(phases, setters, src, findings):
    total = sum(len(p["rows"]) for p in phases.values())
    n_err = len(findings["never"]) + len(findings["late"])
    out = []
    A = out.append

    A(f"<title>Command reference — {esc(src.split('/')[-1])}</title>")
    A(STYLE)
    A('<header class="mast"><div class="wrap">')
    A('<div class="eyebrow">Nokia CLI-CR · generated from the workflow YAML</div>')
    A("<h1>Line-by-line command reference</h1>")
    A(f'<p class="stand">Every step in <code>{esc(src.split("/")[-1])}</code> in execution order — '
      f"the command sent, the node it targets, the condition that decides whether it runs, "
      f"what it captures, and every variable each outcome writes. "
      f"<b>{total} steps</b> across <b>{len(phases)} phases</b>.</p>")
    A("</div></header>")

    A('<nav class="rail"><div class="wrap">')
    A(f'<a href="#findings" class="{"alert" if n_err else ""}">Findings <i>{n_err}</i></a>')
    for p in phases.values():
        A(f'<a href="#{esc(p["id"])}">{esc(p["name"])} <i>{len(p["rows"])}</i></a>')
    A('<a href="#xref">Variable index</a>')
    A("</div></nav>")

    A('<div class="wrap main">')
    A(render_findings(findings))

    for p in phases.values():
        A(f'<section id="{esc(p["id"])}">')
        A(f'<h2>{esc(p["name"])}</h2>')
        if p["description"]:
            A(f'<p class="pdesc">{esc(p["description"])}</p>')
        A('<div class="gate">')
        A(f'<span><b>phase runs when</b> <code>{esc(p["when"] or "always")}</code></span>')
        if p["on_failure"]:
            A(f'<span><b>on_failure</b> <code>{esc(p["on_failure"])}</code></span>')
        if p["then"]:
            A(f'<span><b>then</b> <code>{esc(p["then"])}</code></span>')
        A(f'<span><b>steps</b> {len(p["rows"])}</span>')
        A("</div>")

        for row in p["rows"]:
            st, idx = row["step"], row["idx"]
            node = st.get("node", "local")
            cmd = st.get("send")
            rest = st.get("rest")
            if cmd is None and isinstance(rest, dict):
                cmd = f'{rest.get("method","GET")} {rest.get("path","")}'
            cmd = (str(cmd).strip() if cmd is not None else "(no command — plugin step)")

            cond_when, cond_skip = st.get("when"), st.get("skip_when")
            always = not cond_when and not cond_skip
            cls = "step" + (" cond" if not always else "") + (" inloop" if row["loop"] else "")

            A(f'<article class="{cls}" id="s{idx}">')
            A('<div class="shead">')
            A(f'<span class="num">{idx}</span>')
            A(f'<span class="target">{esc(node)}</span>')
            A(f'<span class="sdesc">{esc(st.get("command_description", ""))}</span>')
            A(f'<span class="badge {"always" if always else "conditional"}">'
              f'{"always runs" if always else "conditional"}</span>')
            A("</div>")

            if row["loop"]:
                A(f'<div class="loopnote">inside {esc(row["loop"])}</div>')

            A(f'<pre class="cmd">{esc(cmd)}</pre>')

            # ---- when / prerequisites -------------------------------------
            A('<div class="block"><h4>Runs when</h4>')
            if always:
                A('<p class="plain">No step condition — runs whenever the phase runs.</p>')
            else:
                if cond_when:
                    A(f'<p class="plain"><b>when</b> <code>{esc(cond_when)}</code></p>')
                if cond_skip:
                    A(f'<p class="plain"><b>skip_when</b> <code>{esc(cond_skip)}</code></p>')
            deps = []
            for e in (cond_when, cond_skip):
                for t in var_tokens(e):
                    if t not in deps:
                        deps.append(t)
            if deps:
                A('<table class="mini"><thead><tr><th>Prerequisite variable</th>'
                  "<th>Set by</th></tr></thead><tbody>")
                for d in deps:
                    src_list = setters.get(d) or []
                    earlier = [s for s in src_list if s[1] < idx]
                    if earlier:
                        txt = ", ".join(
                            (f'<span class="dim">{how}</span>' if i == 0 else
                             f'<a href="#s{i}">step {i}</a> <span class="dim">({ph}, {how})</span>')
                            for ph, i, how in earlier[:6])
                    elif src_list:
                        txt = ('<span class="warnv">only assigned later in the run — '
                               + ", ".join(f"step {i} ({ph})" for ph, i, _h in src_list[:4]) + "</span>")
                    else:
                        txt = '<span class="warnv">never assigned anywhere in this workflow</span>'
                    A(f"<tr><td>{esc(d)}</td><td>{txt}</td></tr>")
                A("</tbody></table>")
            A("</div>")

            # ---- policy ----------------------------------------------------
            pol = []
            for label, key in (("timeout", "timeout_sec"), ("retries", "retries"),
                               ("retry delay", "retry_delay_sec"), ("retry on", "retry_on"),
                               ("on_failure", "on_failure"), ("uses exit code", "use_exit_code")):
                if st.get(key) is not None:
                    pol.append(f"<span><b>{label}</b> <code>{esc(st.get(key))}</code></span>")
            if st.get("expect_reply"):
                pats = ", ".join(esc(e.get("expect", "")) for e in st["expect_reply"]
                                 if isinstance(e, dict))
                pol.append(f"<span><b>answers prompt</b> <code>{pats}</code></span>")
            if pol:
                A('<div class="policy">' + "".join(pol) + "</div>")

            # ---- registers -------------------------------------------------
            regs = st.get("register") or []
            if regs:
                A('<div class="block"><h4>Captures</h4><table class="mini">'
                  "<thead><tr><th>Variable</th><th>From</th></tr></thead><tbody>")
                for r in regs:
                    if not isinstance(r, dict):
                        continue
                    if r.get("name"):
                        cond = f' when <code>{esc(r.get("when"))}</code>' if r.get("when") else ""
                        A(f'<tr><td>{esc(r["name"])}</td><td>set to '
                          f'<code>{esc(r.get("value",""))}</code>{cond}</td></tr>')
                    else:
                        names = re.findall(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>", str(r.get("regex", "")))
                        A(f'<tr><td>{esc(", ".join(names) or "—")}</td>'
                          f'<td>regex <code>{esc(r.get("regex",""))}</code></td></tr>')
                A("</tbody></table></div>")

            # ---- validation ------------------------------------------------
            val = st.get("validation")
            if isinstance(val, dict) and val.get("enabled"):
                A('<div class="block"><h4>Decides</h4>')
                if val.get("description"):
                    A(f'<p class="plain">{esc(val["description"])}</p>')
                crit = criteria_text(val)
                if crit:
                    A(f'<p class="plain"><b>passes if</b> <code>{esc(crit)}</code></p>')
                rows = validation_vars(val)
                if rows:
                    A('<table class="mini"><thead><tr><th>Outcome</th><th>Message</th>'
                      "<th>Sets</th></tr></thead><tbody>")
                    for branch, msg, vs in rows:
                        sets = "<br>".join(
                            f'<code class="{"hot" if v == "ROLLBACK_REQUIRED" else ""}">{esc(v)} = {esc(x)}</code>'
                            for v, x in vs.items()) or '<span class="dim">—</span>'
                        A(f'<tr><td><span class="pill p-{branch}">{branch}</span></td>'
                          f'<td>{esc(msg)}</td><td>{sets}</td></tr>')
                    A("</tbody></table>")
                A("</div>")
            elif isinstance(val, dict):
                A('<div class="block"><h4>Decides</h4>'
                  '<p class="plain dim">Validation disabled — informational step.</p></div>')

            A("</article>")
        A("</section>")

    # ---- variable cross reference ----------------------------------------
    A('<section id="xref"><h2>Variable index</h2>')
    A('<p class="pdesc">Every variable the workflow assigns, and where. '
      "Useful for answering &ldquo;what has to have happened before this step can run?&rdquo;</p>")
    A('<div class="tblwrap"><table class="full"><thead><tr>'
      "<th>Variable</th><th>Assigned at</th></tr></thead><tbody>")
    for v in sorted(setters):
        places = ", ".join(
            (f'<span class="dim">{how}</span>' if i == 0 else
             f'<a href="#s{i}">step {i}</a> <span class="dim">({ph}, {how})</span>')
            for ph, i, how in setters[v])
        hot = ' class="hotrow"' if v.startswith("ROLLBACK_REQUIRED") else ""
        A(f"<tr{hot}><td><code>{esc(v)}</code></td><td>{places}</td></tr>")
    A("</tbody></table></div></section>")

    A(f'<footer>Generated from <code>{esc(src)}</code> · {total} steps · '
      "re-run gen_command_reference.py after any YAML change.</footer>")
    A("</div>")
    return "\n".join(out)


STYLE = """<style>
:root{--ink:#16222e;--dim:#64748b;--ground:#f5f7fa;--panel:#fff;--rule:#d8e2ee;
 --navy:#1a3a5c;--navy-deep:#12293f;--ok:#1e7e45;--bad:#a93226;--warn:#b7770d;
 --code-bg:#eef2f8;--code-ink:#1b3350;
 --sans:"Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
 --mono:Consolas,"Cascadia Mono",Menlo,monospace;}
@media (prefers-color-scheme:dark){:root{--ink:#dce5ef;--dim:#8fa1b4;--ground:#0f151b;
 --panel:#161f29;--rule:#2a3947;--navy:#7ea9d6;--navy-deep:#a9c8e8;--ok:#5fc98b;
 --bad:#e8837a;--warn:#e0ac48;--code-bg:#1d2836;--code-ink:#bcd3ee;}}
:root[data-theme=dark]{--ink:#dce5ef;--dim:#8fa1b4;--ground:#0f151b;--panel:#161f29;
 --rule:#2a3947;--navy:#7ea9d6;--navy-deep:#a9c8e8;--ok:#5fc98b;--bad:#e8837a;
 --warn:#e0ac48;--code-bg:#1d2836;--code-ink:#bcd3ee;}
:root[data-theme=light]{--ink:#16222e;--dim:#64748b;--ground:#f5f7fa;--panel:#fff;
 --rule:#d8e2ee;--navy:#1a3a5c;--navy-deep:#12293f;--ok:#1e7e45;--bad:#a93226;
 --warn:#b7770d;--code-bg:#eef2f8;--code-ink:#1b3350;}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
 font-size:14.5px;line-height:1.6;}
.wrap{max-width:1080px;margin:0 auto;padding:0 22px;}
.mast{background:var(--navy-deep);color:#fff;padding:30px 0 26px;}
:root[data-theme=dark] .mast,@media (prefers-color-scheme:dark){.mast{background:#0b1219;}}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#9fbcda;
 font-weight:600;margin-bottom:8px;}
h1{margin:0 0 9px;font-size:27px;font-weight:600;color:#fff;letter-spacing:-.01em;}
.stand{margin:0;color:#c3d6e8;max-width:78ch;font-size:14.5px;}
.stand code,.stand b{color:#fff;}
.rail{position:sticky;top:0;z-index:20;background:var(--ground);
 border-bottom:1px solid var(--rule);}
.rail .wrap{display:flex;gap:5px;flex-wrap:wrap;padding-top:9px;padding-bottom:9px;}
.rail a{font-size:11.5px;font-weight:600;color:var(--navy);text-decoration:none;
 padding:4px 9px;border-radius:4px;border:1px solid transparent;}
.rail a:hover{background:var(--code-bg);border-color:var(--rule);}
.rail a i{font-style:normal;color:var(--dim);font-weight:500;}
.main{padding-bottom:70px;}
section{margin-top:38px;scroll-margin-top:56px;}
h2{font-size:15px;letter-spacing:.09em;text-transform:uppercase;color:var(--navy);
 margin:0 0 5px;font-weight:700;}
.pdesc{color:var(--dim);margin:0 0 12px;max-width:80ch;}
.gate{display:flex;flex-wrap:wrap;gap:7px 16px;background:var(--panel);
 border:1px solid var(--rule);border-left:4px solid var(--navy);
 border-radius:0 6px 6px 0;padding:9px 14px;margin-bottom:18px;font-size:12.5px;}
.gate b{color:var(--navy);}
code{font-family:var(--mono);font-size:12.5px;background:var(--code-bg);
 color:var(--code-ink);padding:1px 5px;border-radius:3px;word-break:break-word;}
code.hot{background:rgba(183,119,13,.18);color:var(--warn);font-weight:700;}
.step{background:var(--panel);border:1px solid var(--rule);border-radius:8px;
 padding:14px 16px;margin-bottom:12px;scroll-margin-top:56px;}
.step.cond{border-left:4px solid var(--warn);}
.step.inloop{border-style:dashed;}
.shead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:9px;}
.num{font-family:var(--mono);font-size:11.5px;font-weight:700;background:var(--navy);
 color:#fff;border-radius:4px;padding:2px 7px;flex:none;}
:root[data-theme=dark] .num{color:#0b1219;}
.target{font-family:var(--mono);font-size:12px;color:var(--navy);font-weight:600;}
.sdesc{flex:1;min-width:200px;font-weight:600;font-size:13.5px;}
.badge{font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
 padding:2px 8px;border-radius:20px;flex:none;}
.badge.always{background:rgba(30,126,69,.14);color:var(--ok);}
.badge.conditional{background:rgba(183,119,13,.16);color:var(--warn);}
.loopnote{font-size:12px;color:var(--warn);margin-bottom:7px;font-family:var(--mono);}
pre.cmd{font-family:var(--mono);font-size:12.5px;background:var(--code-bg);
 color:var(--code-ink);padding:10px 12px;border-radius:6px;margin:0 0 11px;
 white-space:pre-wrap;word-break:break-word;overflow-x:auto;}
.block{margin-top:10px;}
.block h4{margin:0 0 5px;font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
 color:var(--dim);font-weight:700;}
.plain{margin:0 0 5px;font-size:13px;}
.policy{display:flex;flex-wrap:wrap;gap:5px 14px;margin-top:9px;font-size:12px;
 color:var(--dim);}
.policy b{color:var(--ink);font-weight:600;}
table.mini,table.full{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:5px;}
table.mini th,table.full th{text-align:left;font-size:10.5px;letter-spacing:.05em;
 text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--rule);
 padding:4px 8px;font-weight:700;}
table.mini td,table.full td{padding:5px 8px;border-bottom:1px solid var(--rule);
 vertical-align:top;}
table.full{background:var(--panel);border:1px solid var(--rule);border-radius:6px;}
.tblwrap{overflow-x:auto;}
.dim{color:var(--dim);}
.warnv{color:var(--warn);font-weight:600;}
.hotrow td{background:rgba(183,119,13,.07);}
.rail a.alert{color:var(--bad);}
.rail a.alert i{color:var(--bad);}
.finding{background:var(--panel);border:1px solid var(--rule);border-left:4px solid var(--dim);
 border-radius:0 7px 7px 0;padding:11px 15px;margin-bottom:10px;font-size:13.5px;}
.finding b{color:var(--ink);}
.finding.err{border-left-color:var(--bad);}
.finding.warn{border-left-color:var(--warn);}
.finding.ok{border-left-color:var(--ok);}
.finding .hint{margin-top:7px;padding-top:7px;border-top:1px dashed var(--rule);
 font-size:13px;color:var(--dim);}
.finding .hint b{color:var(--warn);}
.pill{font-size:10px;font-weight:700;text-transform:uppercase;padding:1px 7px;
 border-radius:10px;}
.p-success{background:rgba(30,126,69,.14);color:var(--ok);}
.p-warning{background:rgba(183,119,13,.16);color:var(--warn);}
.p-failure{background:rgba(169,50,38,.14);color:var(--bad);}
a{color:var(--navy);}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--rule);
 font-size:12.5px;color:var(--dim);}
</style>"""


def main():
    argv = [a for a in sys.argv[1:]]
    # --external NAME,NAME  declares values this workflow receives from outside
    # (CIQ fields, arglist keys). They are reported as "never assigned" otherwise,
    # which is correct in the letter and useless in practice.
    for i, a in enumerate(list(argv)):
        if a == "--external" and i + 1 < len(argv):
            EXTERNAL.update(x.strip() for x in argv[i + 1].split(",") if x.strip())
            del argv[i:i + 2]
            break
        if a.startswith("--external="):
            EXTERNAL.update(x.strip() for x in a.split("=", 1)[1].split(",") if x.strip())
            argv.remove(a)
            break
    if len(argv) < 2:
        sys.exit("usage: python gen_command_reference.py <workflow.yaml> <output.html> "
                 "[--external NAME,NAME]")
    src = argv[0]
    dst = argv[1]
    with open(src, encoding="utf-8", errors="replace") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict) or not doc.get("phases"):
        sys.exit(f"{src}: not a CLI-CR workflow (no top-level 'phases:' mapping)")
    phases = collect(doc)
    setters = build_index(phases, doc)
    findings = audit(phases, setters)
    body = render(phases, setters, src.replace("\\", "/"), findings)
    page = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<style>*{box-sizing:border-box}</style>\n</head>\n<body>\n"
            + body + "\n</body>\n</html>\n")
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(page)
    total = sum(len(p["rows"]) for p in phases.values())
    print(f"wrote {dst}")
    print(f"  phases: {len(phases)}   steps: {total}   variables: {len(setters)}")
    for p in phases.values():
        print(f"    {p['name']:<26} {len(p['rows']):>3} steps")

    # surface the audit on the console too - the whole point is that these are
    # easy to miss, and nobody scrolls a 3000-line page looking for them.
    for v, places in findings["never"]:
        twin = findings["twins"].get(v)
        extra = f"  (likely a typo of {twin}, which is written but never read)" if twin else ""
        print(f"  ! never assigned : {v} - read at step {places[0][1]}{extra}")
    for v, (_rph, ridx), (_wph, widx, _how) in findings["late"]:
        print(f"  ! read too early : {v} - read at step {ridx}, first written at step {widx}")
    if findings["writeonly"]:
        print(f"  - written but never read: {len(findings['writeonly'])}")
    if findings["never"] or findings["late"]:
        print("  see the Findings section at the top of the report")


if __name__ == "__main__":
    main()
