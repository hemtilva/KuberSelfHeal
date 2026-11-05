"""Build the KuberSelfHeal presentation (.pptx).

    python ppt/source/build_slides.py
"""
import json
import statistics as st
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt

HERE = Path(__file__).parent
REPO = HERE.parent.parent
OUT = HERE.parent / "KuberSelfHeal_Slides.pptx"
FIG1 = REPO / "report" / "source" / "fig1_architecture.png"

NAVY, TEAL, MINT, CORAL = "0B2B3A", "0E7C86", "2EC4B6", "E4572E"
INK, INK2, CARD, WHITE, LINE = "1B2A33", "4A5A63", "EEF5F6", "FFFFFF", "C9D6D9"
HEAD, BODY = "Cambria", "Calibri"
TEAM = [("Hem Tilva", "24110374"), ("Vansh Barfiwala", "24110380"), ("Pratham Choksi", "24110275"),
        ("Shivangi Thaker", "24110371"), ("Jils Shah", "24110148")]

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
BLANK = prs.slide_layouts[6]


def rgb(h):
    return RGBColor.from_string(h)


def slide(dark=False, notes=""):
    s = prs.slides.add_slide(BLANK)
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = rgb(NAVY if dark else WHITE)
    if notes:
        s.notes_slide.notes_text_frame.text = notes
    return s


def text(s, x, y, w, h, content, size=16, color=INK, bold=False, font=BODY, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, italic=False, space_after=6, name=None):
    """content: str, or list of paragraphs; a paragraph is str or list of (text, {opts}) runs."""
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    paras = content if isinstance(content, list) else [content]
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(space_after)
        runs = para if isinstance(para, list) else [(para, {})]
        for t, o in runs:
            r = p.add_run()
            r.text = t
            f = r.font
            f.name = o.get("font", font)
            f.size = Pt(o.get("size", size))
            f.bold = o.get("bold", bold)
            f.italic = o.get("italic", italic)
            f.color.rgb = rgb(o.get("color", color))
    return tb


def title(s, t, dark=False):
    text(s, 0.6, 0.45, 12.1, 0.9, t, size=34, bold=True, font=HEAD, color=WHITE if dark else NAVY,
         anchor=MSO_ANCHOR.MIDDLE, name="Title")


def box(s, x, y, w, h, fill=CARD, line=None, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08):
    b = s.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    b.fill.solid()
    b.fill.fore_color.rgb = rgb(fill)
    if line:
        b.line.color.rgb = rgb(line)
        b.line.width = Pt(1)
    else:
        b.line.fill.background()
    b.shadow.inherit = False
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        b.adjustments[0] = radius
    return b


def badge(s, x, y, label, fill=TEAL, size=0.55, fsize=16):
    c = box(s, x, y, size, size, fill=fill, shape=MSO_SHAPE.OVAL)
    tf = c.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = label
    r.font.size, r.font.bold, r.font.name = Pt(fsize), True, BODY
    r.font.color.rgb = rgb(WHITE)
    return c


def arrow(s, x, y, w=0.45, h=0.4, color=MINT):
    a = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(x), Inches(y), Inches(w), Inches(h))
    a.fill.solid()
    a.fill.fore_color.rgb = rgb(color)
    a.line.fill.background()
    return a


def stat(s, x, y, w, big, label, color=TEAL, big_size=54):
    text(s, x, y, w, 1.0, big, size=big_size, bold=True, font=HEAD, color=color)
    text(s, x, y + 1.05, w, 0.9, label, size=15, color=INK2)


# ---------- 1. Title ----------
s = slide(dark=True, notes=(
    "KuberSelfHeal: an agent that finds out why a pod on one Kubernetes node cannot reach its database on "
    "another node, and repairs it when that is safe. Built and evaluated on three Raspberry Pi 5 boards."))
text(s, 0.8, 1.6, 11.7, 1.2, "KuberSelfHeal", size=60, bold=True, font=HEAD, color=WHITE)
text(s, 0.8, 2.8, 11.7, 1.1, "Diagnosing and self-healing cross-node pod connectivity failures "
     "on a Raspberry Pi Kubernetes cluster", size=24, color="CFE3E6")
for i, (n, r) in enumerate(TEAM):
    x = 0.8 + i * 2.4
    text(s, x, 5.2, 2.3, 0.4, n, size=16, bold=True, color=WHITE)
    text(s, x, 5.6, 2.3, 0.4, r, size=14, color=MINT)

# ---------- 2. Problem ----------
s = slide(notes=(
    "When the backend cannot reach the database, the user sees one symptom. The cause can sit in any layer: "
    "DNS, the Service, its selector, endpoints, the target port, or the overlay network between the nodes. "
    "Kubernetes restarts crashed pods, but it keeps a wrong configuration in place, faithfully."))
title(s, "One symptom, many possible causes")
text(s, 0.6, 1.7, 5.0, 3.6, [
    [("The backend on node A cannot reach its database on node B.", {"bold": True, "color": NAVY, "size": 20})],
    "Every request crosses several layers. A mistake in any one of them looks the same to the user.",
    [("Kubernetes restarts crashed pods. It does ", {}), ("not", {"bold": True}),
     (" fix a configuration that is valid but wrong.", {})],
], size=18, space_after=14)
layers = [("DB_HOST", "name the backend uses"), ("DNS", "CoreDNS resolves it"), ("Service", "db-service"),
          ("Selector", "which pods it picks"), ("Ready endpoints", "pods that pass readiness"),
          ("targetPort", "where traffic is sent"), ("VXLAN tunnel", "route, firewall, MTU, loss, delay")]
for i, (a, b) in enumerate(layers):
    y = 1.55 + i * 0.78
    box(s, 6.3, y, 6.4, 0.64, fill=CARD)
    badge(s, 6.42, y + 0.07, str(i + 1), fill=TEAL if i < 6 else CORAL, size=0.5, fsize=14)
    text(s, 7.1, y + 0.05, 2.6, 0.55, a, size=17, bold=True, color=NAVY, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 9.6, y + 0.05, 3.0, 0.55, b, size=15, color=INK2, anchor=MSO_ANCHOR.MIDDLE)

# ---------- 3. What we built ----------
s = slide(notes=(
    "The agent diagnoses, applies one fix only when the fix is safe and unambiguous, verifies that the app "
    "really answers HTTP 200 again, and undoes the change or escalates otherwise. Anything outside the faults "
    "it covers is labelled 'other' and handed to a human instead of being guessed at."))
title(s, "What we built: diagnose, fix, verify")
steps = [("1", "Diagnose", "walk the connection path; label the cause with evidence"),
         ("2", "Fix, only if safe", "high confidence and one exact action"),
         ("3", "Verify", "the app must answer HTTP 200 again"),
         ("4", "Undo or escalate", "a fix that did not help is rolled back")]
for i, (n, h, d) in enumerate(steps):
    x = 0.6 + i * 3.15
    box(s, x, 1.9, 2.7, 2.7)
    badge(s, x + 0.25, 2.15, n, size=0.6, fsize=18)
    text(s, x + 0.25, 2.95, 2.25, 0.5, h, size=20, bold=True, color=NAVY)
    text(s, x + 0.25, 3.5, 2.25, 1.1, d, size=15, color=INK2)
    if i < 3:
        arrow(s, x + 2.72, 3.05, w=0.4, h=0.38)
box(s, 0.6, 5.15, 12.1, 1.35, fill="FBE9E4")
badge(s, 0.85, 5.5, "?", fill=CORAL, size=0.62, fsize=20)
text(s, 1.7, 5.3, 10.8, 1.1, [
    [("Anything else is labelled ", {}), ("other", {"bold": True, "color": CORAL}),
     (" and handed to a human, never “fixed”.", {})],
    [("A label must hold both ways: the agent reports a fault if and only if it is the cause.",
      {"size": 15, "color": INK2})]], size=19, anchor=MSO_ANCHOR.MIDDLE, space_after=4)

# ---------- 4. Testbed ----------
s = slide(notes=(
    "Three Raspberry Pi 5 boards with 8 GB each, on a phone Wi-Fi hotspot. The control plane runs the agent. "
    "The backend runs on user1 and Postgres on user2, so every request crosses the VXLAN tunnel. Toolbox "
    "pods on each node run the active tests and read routes, firewall and traffic control."))
title(s, "Testbed: three Raspberry Pi 5 on a hotspot")
s.shapes.add_picture(str(FIG1), Inches(0.6), Inches(1.55), height=Inches(5.5))
specs = [("Hardware", "3× Raspberry Pi 5, 8 GB RAM"),
         ("Software", "Debian 13 (arm64), k3s v1.36, Flannel VXLAN"),
         ("Network", "phone Wi-Fi hotspot (wlan0) only"),
         ("App", "Flask backend on user1 → PostgreSQL 15 on user2"),
         ("Toolbox", "diag-pod (pod network) and diag-host (node network) on every Pi")]
for i, (k, v) in enumerate(specs):
    y = 1.65 + i * 1.07
    box(s, 7.25, y, 5.45, 0.9)
    text(s, 7.45, y + 0.1, 1.35, 0.7, k, size=16, bold=True, color=TEAL, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 8.8, y + 0.1, 3.75, 0.7, v, size=15, color=INK, anchor=MSO_ANCHOR.MIDDLE)

# ---------- 5. Baseline ----------
s = slide(notes=(
    "Before injecting anything we recorded the healthy network. Pod-to-pod across nodes: 12.7 ms, 0% loss, "
    "13.2 Mbit/s, and a path MTU of 1450 bytes, 50 bytes less than the Wi-Fi's 1500 because of the VXLAN "
    "header. Network faults are judged against these numbers."))
title(s, "First, record what healthy looks like")
for i, (big, lab) in enumerate([("12.7 ms", "pod → pod RTT across nodes\n(node → node: 10.2 ms)"),
                                ("0 %", "packet loss, pod → pod and node → node"),
                                ("1450 B", "path MTU in the tunnel\n(Wi-Fi: 1500 B, VXLAN header 50 B)"),
                                ("13.2 Mbit/s", "pod → pod throughput\n(node → node: 14.9 Mbit/s)")]):
    x = 0.6 + i * 3.15
    box(s, x, 2.0, 2.85, 3.3)
    stat(s, x + 0.25, 2.35, 2.45, big, lab, big_size=30 if len(big) > 7 else 44)
text(s, 0.6, 5.75, 12.1, 0.9, "All 60 requests through the app succeeded during the baseline "
     "(p50 95 ms, p99 162 ms). Wi-Fi signal: −45 to −49 dBm.", size=16, color=INK2)

# ---------- 6. Faults ----------
s = slide(notes=(
    "The agent covers ten faults in two groups. Group one is the Kubernetes service path; group two is the "
    "network between the nodes. We inject each of them on purpose. Everything else, such as a NetworkPolicy "
    "or the Wi-Fi going down, must come out as 'other'."))
title(s, "Ten faults in two groups; everything else is “other”")
groups = [("Group 1: Kubernetes service path", TEAL,
           ["CoreDNS down", "wrong Service name (DB_HOST)", "selector does not match the DB pods",
            "no Ready endpoints (scaled to 0, failing readiness)", "wrong targetPort"]),
          ("Group 2: cross-node network", NAVY,
           ["VXLAN blocked by a firewall rule (UDP 8472)", "route to the other node's pods missing",
            "MTU black hole (big packets vanish)", "packet loss on the tunnel", "high latency on the tunnel"])]
for gi, (h, col, items) in enumerate(groups):
    x = 0.6 + gi * 6.2
    box(s, x, 1.65, 5.9, 4.1)
    text(s, x + 0.3, 1.85, 5.3, 0.5, h, size=20, bold=True, color=col)
    for i, it in enumerate(items):
        y = 2.55 + i * 0.62
        badge(s, x + 0.3, y, str(i + 1 + gi * 5), fill=col, size=0.45, fsize=13)
        text(s, x + 0.95, y - 0.02, 4.8, 0.5, it, size=16, color=INK, anchor=MSO_ANCHOR.MIDDLE)
box(s, 0.6, 6.0, 12.1, 0.9, fill="FBE9E4")
text(s, 0.9, 6.0, 11.6, 0.9, [[("other: ", {"bold": True, "color": CORAL}),
     ("a NetworkPolicy blocking the DB, the Wi-Fi itself down, a database error, "
      "or a diagnosis that cannot finish (API unreachable)", {})]], size=16, anchor=MSO_ANCHOR.MIDDLE)

# ---------- 7. Diagnosis ----------
s = slide(notes=(
    "Group one follows the connection in order and stops at the first broken link, so a later symptom is "
    "never blamed for an earlier cause. Group two runs only if group one is clean. It compares the Wi-Fi "
    "link, the pod tunnel and the baseline: if the nodes can talk but pods cannot, the tunnel is broken; "
    "then it looks for the exact route, firewall rule or traffic-control rule."))
title(s, "Diagnosis: follow the path, then compare layers")
box(s, 0.6, 1.6, 5.7, 5.3)
text(s, 0.9, 1.8, 5.2, 0.5, "Group 1: first broken link wins", size=20, bold=True, color=TEAL)
for i, t in enumerate(["Does CoreDNS have a ready pod?", "Does the Service in DB_HOST exist?",
                       "Does its selector pick the DB pods?", "Is any of them Ready?",
                       "Is the targetPort the one that is open?"]):
    y = 2.5 + i * 0.8
    badge(s, 0.9, y, str(i + 1), size=0.45, fsize=13)
    text(s, 1.55, y - 0.02, 4.6, 0.5, t, size=16, anchor=MSO_ANCHOR.MIDDLE)
box(s, 6.6, 1.6, 6.1, 5.3)
text(s, 6.9, 1.8, 5.6, 0.5, "Group 2: compare layers and the baseline", size=20, bold=True, color=NAVY)
rows = [("nodes OK, pods ≥ 90 % loss", "tunnel broken → check route, firewall, netem"),
        ("pod loss or RTT ≫ baseline, nodes clean", "shaping on the tunnel → netem rule?"),
        ("1450-byte DF ping fails, small ones pass", "MTU black hole → measure path MTU, bulk TCP"),
        ("tunnel fine, DB pod silent", "blocked at the pod → other")]
for i, (a, b) in enumerate(rows):
    y = 2.5 + i * 1.08
    text(s, 6.9, y, 5.6, 0.45, a, size=16, bold=True, color=INK)
    text(s, 6.9, y + 0.42, 5.6, 0.45, b, size=15, color=INK2)

# ---------- 8. Lesson ----------
s = slide(notes=(
    "Our first evaluation labelled all three NetworkPolicy trials as a wrong port. k3s rejects packets denied "
    "by a policy, so the client sees 'connection refused', exactly like a wrong port. We changed the rule: "
    "the agent now needs positive evidence for every label, here that the port is undeclared or another "
    "declared port is open. The second run got all 24 right."))
title(s, "Lesson: “refused” does not prove a wrong port")
box(s, 0.6, 1.7, 5.4, 4.9)
text(s, 0.95, 1.95, 4.8, 0.5, "First evaluation run", size=18, color=INK2)
text(s, 0.95, 2.45, 4.8, 1.3, "21/24", size=72, bold=True, font=HEAD, color=CORAL)
text(s, 0.95, 3.85, 4.8, 2.5, "All 3 NetworkPolicy trials labelled port_mismatch: k3s rejects denied "
     "packets, and the client sees the same “connection refused” as a wrong port.", size=16)
arrow(s, 6.2, 3.85, w=0.75, h=0.55)
box(s, 7.15, 1.7, 5.55, 4.9)
text(s, 7.5, 1.95, 4.9, 0.5, "After requiring positive evidence", size=18, color=INK2)
text(s, 7.5, 2.45, 4.9, 1.3, "24/24", size=72, bold=True, font=HEAD, color=TEAL)
text(s, 7.5, 3.85, 4.95, 2.5, "port_mismatch now needs the targetPort to be undeclared or another declared "
     "port to be open. Otherwise: other, with the NetworkPolicies listed as evidence.", size=16)

# ---------- 9. Healing safely ----------
s = slide(notes=(
    "The healer is deliberately conservative. It acts only on a high-confidence label with one exact action, "
    "one change at a time, and calls it fixed only when the app answers again. A change that did not help "
    "is undone, except removing a faulty rule, which would only bring the fault back. 'other' is never "
    "touched, because a NetworkPolicy may be intentional security."))
title(s, "Healing safely: six rules")
rules = [("1", "High confidence, exact fix", "otherwise escalate with the evidence"),
         ("2", "One change at a time", "each one verified before the next"),
         ("3", "Healed = HTTP 200", "not “kubectl succeeded”"),
         ("4", "Undo what did not help", "except removing a faulty rule or route"),
         ("5", "Never touch other", "a NetworkPolicy may be intentional"),
         ("6", "Log every step", "audit trail in heal_log.jsonl")]
for i, (n, h, d) in enumerate(rules):
    x, y = 0.6 + (i % 3) * 4.1, 1.75 + (i // 3) * 2.55
    box(s, x, y, 3.8, 2.25)
    badge(s, x + 0.3, y + 0.3, n, fill=CORAL if n == "5" else TEAL, size=0.55, fsize=16)
    text(s, x + 0.3, y + 1.0, 3.3, 0.5, h, size=19, bold=True, color=NAVY)
    text(s, x + 0.3, y + 1.5, 3.3, 0.6, d, size=15, color=INK2)

# ---------- 10. Results ----------
s = slide(notes=(
    "On the real cluster, in the final runs, every trial got the right label: 24 of 24 for group one, "
    "including the NetworkPolicy as 'other', and 18 of 18 for group two. Healing fixed 30 of 33 injected "
    "faults; the three misses were the readiness fault in the first version. In the nine no-fault and "
    "NetworkPolicy runs the healer changed nothing."))
title(s, "Results on the Pi cluster")
for i, (big, lab, col) in enumerate([
        ("24/24", "correct labels, group 1\n(incl. no-fault and NetworkPolicy → other)", TEAL),
        ("18/18", "correct labels, group 2\n(incl. no-fault)", TEAL),
        ("30/33", "injected faults healed;\napp answers HTTP 200 again", NAVY),
        ("0", "changes in 9 no-fault and\nuncovered-fault runs", NAVY)]):
    x = 0.6 + i * 3.15
    box(s, x, 1.8, 2.85, 3.4)
    stat(s, x + 0.25, 2.15, 2.45, big, lab, color=col, big_size=54)
text(s, 0.6, 5.6, 12.1, 1.2, [
    "The 3 healing misses: the readiness fault, escalated instead of rolled back by the first version. "
    "The rule was fixed (roll back only a revision that was never Ready) but not yet re-run on the Pis.",
], size=16, color=INK2)

# ---------- 11. Time to recover (native chart) ----------
s = slide(notes=(
    "Kubernetes-level fixes bring the app back in two to eight seconds after the fix. For network faults most "
    "time goes into the diagnosis itself, which measures the network. The 7.3 seconds after a network fix is "
    "one verifying diagnosis, so real recovery is at most that. MTU takes longest because verification "
    "repeats the MTU search and a bulk transfer."))
title(s, "Time to recover per fault")
trials = []
for d in ("heal-20261105-023713", "heal-group2-20261105-031405"):
    p = REPO / "code" / "eval" / "results" / d / "trials.jsonl"
    trials += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
names = [("coredns", "CoreDNS down"), ("wrong-name", "wrong Service name"), ("selector", "selector typo"),
         ("scale0", "DB scaled to 0"), ("port", "wrong targetPort"), ("fw-vxlan", "VXLAN blocked"),
         ("route", "route missing"), ("loss", "packet loss"), ("delay", "high latency"), ("mtu", "MTU black hole")]
cats, before, after = [], [], []
for key, lab in reversed(names):
    ft = [t for t in trials if t["fault"] == key and t["correct"]]
    h, m = st.mean(t["heal_s"] for t in ft), st.mean(t["mttr_s"] for t in ft)
    cats.append(lab); before.append(round(h - m, 1)); after.append(round(m, 1))
cd = CategoryChartData()
cd.categories = cats
cd.add_series("before the fix (diagnosis, decision)", before)
cd.add_series("fix → app answers HTTP 200", after)
gf = s.shapes.add_chart(XL_CHART_TYPE.BAR_STACKED, Inches(0.5), Inches(1.5), Inches(8.2), Inches(5.6), cd)
ch = gf.chart
ch.has_legend = True
ch.legend.position = XL_LEGEND_POSITION.BOTTOM
ch.legend.include_in_layout = False
ch.legend.font.size, ch.legend.font.name = Pt(14), BODY
ch.legend.font.color.rgb = rgb(INK2)
ch.plots[0].gap_width = 45
for ser, col in zip(ch.series, (TEAL, CORAL)):
    ser.format.fill.solid()
    ser.format.fill.fore_color.rgb = rgb(col)
    ser.format.line.color.rgb = rgb(WHITE)
    ser.format.line.width = Pt(1)
    ser.data_labels.show_value = True
    ser.data_labels.position = XL_LABEL_POSITION.CENTER
    ser.data_labels.font.size, ser.data_labels.font.name = Pt(12), BODY
    ser.data_labels.font.color.rgb = rgb(WHITE)
ca, va = ch.category_axis, ch.value_axis
ca.tick_labels.font.size, ca.tick_labels.font.name = Pt(14), BODY
ca.tick_labels.font.color.rgb = rgb(INK)
ca.format.line.color.rgb = rgb(LINE)
va.tick_labels.font.size, va.tick_labels.font.name = Pt(12), BODY
va.tick_labels.font.color.rgb = rgb(INK2)
va.has_major_gridlines = True
va.major_gridlines.format.line.color.rgb = rgb("E3E9EB")
va.format.line.fill.background()
va.has_title = True
va.axis_title.text_frame.text = "mean seconds per healing run (3 runs each)"
va.axis_title.text_frame.paragraphs[0].runs[0].font.size = Pt(13)
va.axis_title.text_frame.paragraphs[0].runs[0].font.color.rgb = rgb(INK2)
for i, (big, lab) in enumerate([("2–8 s", "Kubernetes fixes: from the fix to a working app"),
                                ("≤ 7.3 s", "network fixes: one verifying diagnosis"),
                                ("23 s", "MTU: verification repeats the MTU search and a bulk transfer")]):
    y = 1.65 + i * 1.8
    box(s, 9.0, y, 3.7, 1.6)
    text(s, 9.25, y + 0.12, 3.3, 0.6, big, size=30, bold=True, font=HEAD, color=TEAL if i < 2 else CORAL)
    text(s, 9.25, y + 0.8, 3.3, 0.75, lab, size=14, color=INK2)

# ---------- 12. Limitations ----------
s = slide(notes=(
    "Limits: one application, one fault at a time, faults we injected ourselves, one small cluster on one "
    "Wi-Fi network. The last code changes, including the readiness rollback, still need a run on the Pis. "
    "Next: re-run the full evaluation, explain NetworkPolicy denials without changing them, handle several "
    "faults at once, and test on larger clusters and other network plugins."))
title(s, "Limitations and next steps")
cols = [("Limitations", CORAL, ["one application topology, one fault at a time",
                                "faults injected by us: clean signatures",
                                "one small cluster on one Wi-Fi network",
                                "latest code changes not yet re-run on the Pis"]),
        ("Next steps", TEAL, ["re-run the full evaluation on the current code",
                              "explain NetworkPolicy denials (which rule) without changing them",
                              "handle several simultaneous faults",
                              "larger clusters and other CNI plugins"])]
for ci, (h, col, items) in enumerate(cols):
    x = 0.6 + ci * 6.2
    box(s, x, 1.65, 5.9, 5.1)
    text(s, x + 0.3, 1.85, 5.3, 0.5, h, size=22, bold=True, color=col)
    for i, it in enumerate(items):
        y = 2.6 + i * 1.0
        badge(s, x + 0.3, y, "!" if ci == 0 else "→", fill=col, size=0.45, fsize=14)
        text(s, x + 0.95, y - 0.05, 4.75, 0.9, it, size=16)

# ---------- 13. Close ----------
s = slide(dark=True, notes="Thank you. The code, the report, the raw results and our AI transcripts are in the repository.")
text(s, 0.8, 1.7, 11.7, 1.2, "Thank you", size=60, bold=True, font=HEAD, color=WHITE)
text(s, 0.8, 2.9, 11.7, 0.7, "Questions?", size=28, color=MINT)
text(s, 0.8, 4.3, 11.7, 0.5, "github.com/hemtilva/KuberSelfHeal", size=20, color=WHITE)
text(s, 0.8, 4.85, 11.7, 0.5, "code · report · slides · raw results · AI transcripts",
     size=16, color="CFE3E6")
text(s, 0.8, 6.2, 11.7, 0.5, "  ·  ".join(n for n, _ in TEAM), size=15, color="CFE3E6")

prs.save(OUT)
print("saved", OUT)
