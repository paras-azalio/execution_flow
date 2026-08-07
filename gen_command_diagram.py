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


def flatten(steps, depth=0, loop=False):
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        if st.get("type") == "loop":
            for sub in flatten(st.get("steps"), depth + 1, True):
                yield sub
        else:
            yield st, loop


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

.main{padding-bottom:60px;}
section{margin-top:32px;scroll-margin-top:56px;}
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
.stage svg .nodeLabel,.stage svg .edgeLabel,.stage svg .label,
.stage svg text,.stage svg span,.stage svg p{color:#10243a!important;
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
  }

  function start() { document.querySelectorAll('.canvas').forEach(setup); }

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
        A(f'<section id="{pid}"><h2>{html.escape(pname)}</h2>')
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
        A("</div></section>")
        info.update(step_info(rows, pid, default_on_failure))

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
