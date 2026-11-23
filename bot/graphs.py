"""Render inspectable PNG and CSV attachments without a display server."""
from __future__ import annotations

import csv
from io import BytesIO, StringIO
from threading import Lock

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure


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
