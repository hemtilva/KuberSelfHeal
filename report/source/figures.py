"""Figures for the report: architecture (Fig. 1) and healing time per fault (Fig. 2).

    python report/source/figures.py code report/source
"""
import json, statistics as st, sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

REPO = Path(sys.argv[1]); OUT = Path(sys.argv[2])
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e4e3df"
BLUE, ORANGE = "#2a78d6", "#eb6834"          # categorical slots 1 and 2 (reference palette, light)
plt.rcParams.update({"font.family": "Times New Roman", "font.size": 8, "text.color": INK,
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})

# ---------- Fig. 1: architecture ----------
fig, ax = plt.subplots(figsize=(3.45, 3.0), dpi=300)
ax.set_xlim(0, 100); ax.set_ylim(0, 88); ax.axis("off")

def box(x, y, w, h, title, lines=(), fill="#f4f3f0", edge=MUTED, bold=True, size=7.5):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.4,rounding_size=1.5",
                                fc=fill, ec=edge, lw=0.8))
    ax.text(x + w / 2, y + h - 2.6, title, ha="center", va="top", fontsize=size,
            fontweight="bold" if bold else "normal")
    for i, l in enumerate(lines):
        ax.text(x + w / 2, y + h - 7.0 - i * 3.9, l, ha="center", va="top", fontsize=6.4, color=INK2)

def arrow(p, q, color=INK2, style="-|>", ls="-", lw=0.9, rad=0.0):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle=style, mutation_scale=7, color=color, lw=lw,
                                 linestyle=ls, connectionstyle=f"arc3,rad={rad}"))

# control-plane node
box(2, 56, 96, 30, "master (control plane)", fill="#ffffff")
box(5, 59, 28, 20, "diagnose.py", ["service path, then", "the network;", "labels the cause"], fill="#eaf1fb", edge=BLUE)
box(36, 59, 28, 20, "heal.py", ["safe fix, verify", "(HTTP 200), else", "undo / escalate"], fill="#eaf1fb", edge=BLUE)
box(67, 59, 28, 20, "fault.sh + eval/", ["inject a fault,", "score label and", "healing, reset"], fill="#f4f3f0")
arrow((33, 69), (36, 69))

# worker nodes
box(2, 6, 45, 40, "user1 (backend node)", fill="#ffffff")
box(5, 25, 19, 14, "diag-pod", ["ping, nc, curl,", "iperf3"], fill="#f4f3f0", size=7)
box(26, 25, 19, 14, "backend", ["Flask pod", ":30080"], fill="#f4f3f0", size=7)
box(5, 9, 40, 12, "diag-host (host network)", ["ip route, iptables, tc"], fill="#f4f3f0", size=7)

box(53, 6, 45, 40, "user2 (database node)", fill="#ffffff")
box(56, 25, 19, 14, "database", ["Postgres pod", "db-service"], fill="#f4f3f0", size=7)
box(77, 25, 19, 14, "diag-pod", ["iperf3 server"], fill="#f4f3f0", size=7)
box(56, 9, 40, 12, "diag-host (host network)", ["ip route, iptables, tc"], fill="#f4f3f0", size=7)

# data path backend -> db (VXLAN over Wi-Fi)
arrow((45.6, 32), (55.4, 32), color=ORANGE, lw=1.4)
for i, t in enumerate(("SQL", "VXLAN", "UDP", "8472")):
    ax.text(50.5, 29 - i * 3.3, t, ha="center", va="top", fontsize=6.0, color=INK)
ax.text(50, 1.2, "all three Pis on one phone Wi-Fi hotspot (wlan0, 10.206.185.0/24)",
        ha="center", fontsize=6.3, color=INK2, style="italic")
# control: kubectl / kubectl exec
arrow((19, 59), (19, 46.5), ls=(0, (3, 2)))
arrow((80, 59), (80, 46.5), ls=(0, (3, 2)))
ax.text(21, 51.5, "kubectl get / exec", fontsize=6.3, color=INK2)
ax.text(82, 51.5, "kubectl exec", fontsize=6.3, color=INK2)
fig.savefig(OUT / "fig1_architecture.png", bbox_inches="tight", pad_inches=0.02)
plt.close(fig)

# ---------- Fig. 2: healing time per fault ----------
def load(d):
    return [json.loads(l) for l in (REPO / "eval/results" / d / "trials.jsonl").read_text().splitlines() if l.strip()]
trials = load("heal-20261105-023713") + load("heal-group2-20261105-031405")
names = {"coredns": "CoreDNS down", "wrong-name": "wrong service name", "selector": "selector typo",
         "scale0": "DB scaled to 0", "port": "wrong targetPort", "fw-vxlan": "VXLAN blocked",
         "route": "route missing", "loss": "packet loss", "delay": "high latency", "mtu": "MTU blackhole"}
rows = []
for f, label in names.items():
    ft = [t for t in trials if t["fault"] == f and t["correct"]]
    heal, fix = st.mean(t["heal_s"] for t in ft), st.mean(t["mttr_s"] for t in ft)
    rows.append((label, heal - fix, fix, heal))
rows = rows[::-1]  # first fault at the top

fig, ax = plt.subplots(figsize=(3.45, 2.75), dpi=300)
y = range(len(rows))
ax.barh(y, [r[1] for r in rows], height=0.62, color=BLUE, label="before the fix (diagnosis, decision)")
ax.barh(y, [r[2] for r in rows], left=[r[1] for r in rows], height=0.62, color=ORANGE,
        edgecolor="white", linewidth=1.0, label="fix applied → app answers again")
for i, r in enumerate(rows):
    ax.text(r[3] + 0.8, i, f"{r[3]:.1f}", va="center", fontsize=7, color=INK)
ax.set_yticks(list(y)); ax.set_yticklabels([r[0] for r in rows], fontsize=7.5, color=INK)
ax.set_xlabel("mean time per healing run (s), 3 runs each", fontsize=7.5)
ax.set_xlim(0, 68)
ax.axhline(4.5, color=MUTED, lw=0.6, ls=(0, (2, 2)))
ax.text(67, 4.62, "group 1: Kubernetes", ha="right", va="bottom", fontsize=6.5, color=INK2)
ax.text(67, 4.38, "group 2: network", ha="right", va="top", fontsize=6.5, color=INK2)
ax.grid(axis="x", color=GRID, lw=0.6); ax.set_axisbelow(True)
for s in ("top", "right", "left"): ax.spines[s].set_visible(False)
ax.spines["bottom"].set_color(MUTED); ax.tick_params(axis="y", length=0)
ax.legend(loc="upper center", bbox_to_anchor=(0.42, -0.17), ncol=1, frameon=False, fontsize=7)
fig.savefig(OUT / "fig2_healing_time.png", bbox_inches="tight", pad_inches=0.02)
plt.close(fig)
print("\n".join(f"{r[0]:<20} before {r[1]:5.1f}  fix->ok {r[2]:5.1f}  total {r[3]:5.1f}" for r in rows[::-1]))
