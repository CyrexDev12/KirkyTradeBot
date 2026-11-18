"""Discord presentation of the actual stored forecasts."""
from datetime import datetime

import discord


def timestamp(value, style="f"):
    return f"<t:{int(datetime.fromisoformat(value).timestamp())}:{style}>" if value else "Unknown"


def percent(value):
    return f"{value:.1%}" if value is not None else "Unavailable"


def legacy_origin(prediction):
    if prediction.get("kind") == "walk_forward_reconstruction":
        return "Historical reconstruction"
    return "Generated " + timestamp(prediction.get("generated_at"))


def matchup_embed(payload):
    game = payload["game"]
    home, away = game["home"], game["away"]
    embed = discord.Embed(
        title=f"{game['away_team']['name']} at {game['home_team']['name']}",
        description=f"{game['season']} · Week {game['week']} · {game['status']}\nKickoff {timestamp(game['kickoff'], 'F')}",
        colour=0xA5E05E,
    )
    prediction, total, mc = (payload.get(k) for k in ("prediction", "total_prediction", "monte_carlo"))
    existing = "Win prediction unavailable"
    if prediction:
        p = prediction["home_probability"]
        existing = f"{away} **{percent(1-p)}** · {home} **{percent(p)}**\nBinary win probabilities, excluding ties\n{legacy_origin(prediction)}"
    if total:
        existing += f"\nExpected total **{total['predicted_total']:.1f}** points"
        if total.get("interval_low") is not None:
            existing += f" · 80% interval {total['interval_low']:.1f}–{total['interval_high']:.1f}"
        existing += f"\nTotal forecast · {legacy_origin(total)}"
    else:
        existing += "\nTotal prediction unavailable"
    embed.add_field(name="Existing model", value=existing, inline=False)
    if mc and mc.get("status") == "available":
        distributions = mc["distributions"]
        low, high = distributions["total"]["interval_80"]
        value = (
            f"Expected score · {away} **{distributions['away']['mean']:.1f}** · {home} **{distributions['home']['mean']:.1f}**\n"
            f"Win · {away} **{percent(mc['probability_away_win'])}** · {home} **{percent(mc['probability_home_win'])}**\n"
            f"Final tie **{percent(mc['probability_tie'])}**\n"
            f"Expected total **{distributions['total']['mean']:.1f}** · 80% interval **{low}–{high}**\n"
            f"{mc['simulation_count']:,} draws · {mc['residual_pair_count']} earlier error pairs\n"
            f"Cutoff {timestamp(mc['as_of'])}"
        )
        if mc.get("limited_sample"):
            value += "\nLimited current-season sample"
        embed.add_field(name="Current-season Monte Carlo · experimental", value=value, inline=False)
        conditional = mc.get("home_win_given_no_tie")
        if prediction and conditional is not None:
            difference = (conditional - prediction["home_probability"]) * 100
            embed.add_field(name=f"{home} win given no tie",
                            value=f"Existing {percent(prediction['home_probability'])} · MC {percent(conditional)}\n"
                                  f"MC minus existing **{difference:+.1f} percentage points**", inline=False)
    else:
        embed.add_field(name="Current-season Monte Carlo",
                        value=(mc or {}).get("unavailable_reason") or "Prediction unavailable", inline=False)
    if payload.get("schedule_stale"):
        embed.add_field(name="Schedule freshness", value="The schedule source is delayed.", inline=False)
    embed.set_footer(text=f"NFL Fair Value · {game['game_id']} · Final scores include overtime. MC tie estimates are experimental.")
    return embed


def week_embed(data):
    lines = [f"**{g['away']} at {g['home']}** · {timestamp(g['kickoff'])}" for g in data["games"]]
    return discord.Embed(title=f"NFL matchups · {data['season']} week {data['week']}",
                         description="Choose a game below for both models.\n\n" + "\n".join(lines),
                         colour=0xA5E05E)
