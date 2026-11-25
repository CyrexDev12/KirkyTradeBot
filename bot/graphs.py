"""Render inspectable PNG and CSV attachments without a display server."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from threading import Lock

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator, StrMethodFormatter


_render_lock = Lock()
OBSERVED = "#315e97"
ADJUSTED = "#ba5930"
PROJECTED = "#247c66"


def _csv(rows, columns):
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return BytesIO(output.getvalue().encode("utf-8"))


def _png(figure):
    output = BytesIO()
    FigureCanvasAgg(figure).print_png(output)
    output.seek(0)
    return output


def oil_live_graph(payload, *, title="Oil D live · realized performance"):
    phoenix = timezone(-timedelta(hours=7), "MST")
    times = [datetime.fromisoformat(p["at"]).astimezone(phoenix) for p in payload["points"]]
    balances = [float(p["balance"]) for p in payload["points"]]
    budget = float(payload["starting_budget"])
    with _render_lock:
        figure = Figure(figsize=(9.2, 4.8), dpi=135, layout="constrained")
        axis = figure.subplots()
        axis.axhline(budget, color="#84919c", linewidth=1.2, linestyle="--", label="Initial budget")
        axis.step(times, balances, where="post", color=OBSERVED, linewidth=2, label="Realized balance")
        axis.scatter(times[1:-1], balances[1:-1], color=OBSERVED, s=17, zorder=3)
        axis.set_xlim(times[0], max(times[-1], times[0] + timedelta(seconds=1)))
        ymin, ymax = min(budget, *balances), max(budget, *balances)
        margin = max(0.02, (ymax - ymin) * 0.15)
        axis.set_ylim(ymin - margin, ymax + margin)
        axis.set_ylabel("Realized balance (USD)")
        axis.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
        axis.yaxis.set_major_formatter(StrMethodFormatter("${x:,.2f}"))
        pnl = axis.secondary_yaxis("right", functions=(lambda v: v - budget, lambda v: v + budget))
        pnl.set_ylabel("P&L since initial budget (USD)")
        pnl.set_yticks([round(tick - budget, 6) for tick in axis.get_yticks()])
        pnl.yaxis.set_major_formatter(StrMethodFormatter("{x:+z,.2f}"))
        locator = AutoDateLocator(tz=phoenix, minticks=3, maxticks=8)
        axis.xaxis.set_major_locator(locator)
        axis.xaxis.set_major_formatter(ConciseDateFormatter(locator, tz=phoenix))
        axis.set_xlabel("Trade close time · Arizona (MST)")
        axis.set_title(title, loc="left", fontsize=15)
        axis.grid(axis="y", alpha=0.2)
        axis.legend(loc="best", framealpha=0.9)
        return _png(figure)


def _series(axis, observed, adjusted, *, ylabel):
    steps = range(len(observed))
    axis.plot(steps, observed, color=OBSERVED, marker="o", markersize=4, label="Recorded")
    if any(value is not None for value in adjusted):
        axis.plot(steps, adjusted, color=ADJUSTED, linestyle="--", alpha=0.85,
                  label="Outlier adjusted")
        changed = [(i, raw) for i, (raw, clean) in enumerate(zip(observed, adjusted))
                   if raw is not None and clean is not None and abs(raw - clean) > 0.01]
        if changed:
            x, y = zip(*changed)
            axis.scatter(x, y, facecolors="none", edgecolors=ADJUSTED, s=90,
                         linewidths=1.3, zorder=3, label="Adjusted game")
    axis.set_ylabel(ylabel)
    axis.grid(axis="y", alpha=0.24)
    axis.spines[["top", "right"]].set_visible(False)


def team_points_graph(payload):
    rows = payload["games"]
    if not rows:
        raise ValueError("No completed games are available for this team and season.")
    with _render_lock:
        figure = Figure(figsize=(10.4, 6.3), dpi=145, layout="constrained")
        scoring, defense = figure.subplots(2, 1, sharex=True, sharey=True)
        for axis, key, label in ((scoring, "points_for", "Points scored"),
                                 (defense, "points_against", "Points allowed")):
            _series(axis, [g[key] for g in rows], [g.get(f"robust_{key}") for g in rows], ylabel=label)
        defense.set_xticks(range(len(rows)), [f"W{g['week']}\n{g['opponent']}" for g in rows], fontsize=8)
        legend = {}
        for axis in (scoring, defense):
            handles, labels = axis.get_legend_handles_labels()
            legend.update(zip(labels, handles))
        figure.legend(legend.values(), legend.keys(), loc="outside lower center", ncols=3, fontsize=9)
        figure.suptitle(f"{payload['team']} · {payload['season']} scoring by game", fontsize=16)
        png = _png(figure)
    return png, _csv(rows, ("week", "kickoff", "opponent", "points_for", "points_against",
                            "robust_points_for", "robust_points_against"))


def player_yards_graph(payload):
    history = payload["history"]
    if not history:
        raise ValueError("There is no game history available for this forecast.")
    index = len(history)
    estimate = payload["projection"]
    low, high = payload.get("interval_low"), payload.get("interval_high")
    with _render_lock:
        figure = Figure(figsize=(10.4, 5.4), dpi=145, layout="constrained")
        axis = figure.subplots()
        _series(axis, [g["yards"] for g in history], [g.get("robust_yards") for g in history],
                ylabel="Yards")
        if low is not None and high is not None:
            axis.vlines(index, low, high, color=PROJECTED, linewidth=3,
                        label="80% prediction interval")
            axis.scatter([index, index], [low, high], marker="_", color=PROJECTED, s=130)
        axis.scatter(index, estimate, color=PROJECTED, marker="D", s=64, zorder=3, label="Projection")
        axis.axvline(index - 0.5, color="#8292a1", linewidth=1, linestyle=":")
        labels = [f"'{str(g['season'])[-2:]} W{g['week']}\n{g['opponent']}" for g in history]
        labels.append(f"W{payload['week']}\n{payload['opponent']}")
        steps = list(range(0, index, max(1, (index + 13) // 14))) + [index]
        axis.set_xticks(steps, [labels[i] for i in steps], fontsize=8)
        figure.legend(loc="outside lower center", fontsize=9, ncols=3)
        figure.suptitle(f"{payload['player']} · {payload['stat']} yards", fontsize=16)
        png = _png(figure)
    rows = [{**g, "kind": "history"} for g in history]
    rows.append({"kind": payload.get("kind", "projection"), "season": payload["season"], "week": payload["week"],
                 "kickoff": payload["kickoff"], "opponent": payload["opponent"], "projection": estimate,
                 "interval_low": low, "interval_high": high})
    return png, _csv(rows, ("kind", "season", "week", "kickoff", "opponent", "yards", "attempts", "robust_yards",
                            "outlier", "projection", "interval_low", "interval_high"))
