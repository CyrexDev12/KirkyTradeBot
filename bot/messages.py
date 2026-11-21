"""Discord presentation of stored NFL forecasts."""
from datetime import datetime

import discord


def timestamp(value, style="f"):
    return f"<t:{int(datetime.fromisoformat(value).timestamp())}:{style}>" if value else "Time TBD"


def percent(value):
    return f"{value:.1%}" if value is not None else "Unavailable"


def matchup_embed(payload):
    game = payload["game"]
    home, away = game["home"], game["away"]
    embed = discord.Embed(
        title=f"🏈 {game['away_team']['name']} at {game['home_team']['name']}",
        description=f"Week {game['week']} · {game['status']} · {timestamp(game['kickoff'])}",
        colour=0xA5E05E,
    )
    prediction, total, mc = (payload.get(k) for k in ("prediction", "total_prediction", "monte_carlo"))
    lines = []
    if prediction:
        p = prediction["home_probability"]
        lines.append(f"Win odds excluding ties · {away} **{percent(1-p)}** · {home} **{percent(p)}**")
    else:
        lines.append("Win forecast pending.")
    if total:
        lines.append(f"Expected total **{total['predicted_total']:.1f} points**")
    if any((forecast or {}).get("kind") == "walk_forward_reconstruction" for forecast in (prediction, total)):
        lines.append("Historical reconstruction")
    embed.add_field(name="📊 Gradient model", value="\n".join(lines), inline=False)

    if mc and mc.get("status") == "available":
        scores = mc["distributions"]
        low, high = scores["total"]["interval_80"]
        value = (
            f"Score forecast · **{away} {scores['away']['mean']:.1f} – {home} {scores['home']['mean']:.1f}**\n"
            f"Win odds · {away} **{percent(mc['probability_away_win'])}** · {home} **{percent(mc['probability_home_win'])}**"
            f" · tie {percent(mc['probability_tie'])}\n"
            f"Expected total **{scores['total']['mean']:.1f}** · 80% of simulations land between {low} and {high} points"
        )
        if mc.get("limited_sample"):
            value += "\nOnly a few games of data so far."
    else:
        value = (mc or {}).get("unavailable_reason") or "Waiting for enough game data."
    embed.add_field(name="🎲 Monte Carlo", value=value, inline=False)
    if payload.get("schedule_stale"):
        embed.add_field(name="🕒 Schedule update", value="The kickoff feed is running behind.", inline=False)
    # DiscordTransport reconciles pregame deliveries using this stable footer prefix.
    embed.set_footer(text=f"NFL Fair Value · {game['game_id']} · Beef Kirkey’s game board")
    return embed


def week_embed(data):
    lines = [f"**{g['away']} at {g['home']}** · {timestamp(g['kickoff'])}" for g in data["games"]]
    return discord.Embed(title=f"🏈 Game board · Week {data['week']}, {data['season']}",
                         description=("Tap a matchup for the forecast.\n\n" + "\n".join(lines)) if lines
                         else "No games on the board for this week.", colour=0xA5E05E)
