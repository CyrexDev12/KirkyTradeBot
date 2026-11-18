"""Kickoff scheduling, recovery and duplicate suppression."""
from datetime import datetime, timezone
import logging

from .messages import matchup_embed

logger = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc)


def due(game, now, minutes):
    if not game.get("kickoff_confirmed", True) or game.get("home_score") is not None or game.get("away_score") is not None:
        return False
    if str(game.get("source_status") or "").lower() in {"cancelled", "canceled", "postponed", "suspended", "live"}:
        return False
    seconds = (datetime.fromisoformat(game["kickoff"]) - now).total_seconds()
    return 0 < seconds <= minutes * 60


class AlertScheduler:
    def __init__(self, api, store, transport, clock=utcnow):
        self.api, self.store, self.transport, self.clock = api, store, transport, clock
        self.last_checked = None
        self.last_error = None

    async def tick(self):
        settings = self.store.settings()
        if not settings:
            self.last_checked, self.last_error = self.clock(), None
            return
        feed = await self.api.upcoming()
        self.last_checked = self.clock()
        if feed["schedule_stale"]:
            self.last_error = "Schedule refresh is overdue. Automatic posts are waiting for fresh data."
            return
        self.last_error = None
        for game in feed["games"]:
            relevant = [s for s in settings if due(game, self.clock(), s["minutes"])
                        and not self.store.delivered(s["guild_id"], game["game_id"])]
            if not relevant:
                continue
            try:
                payload = await self.api.forecast(game["game_id"])
                current_game = payload["game"]
                if payload.get("schedule_stale") or current_game["status"] != "Upcoming":
                    continue
                seconds_left = (datetime.fromisoformat(current_game["kickoff"]) - self.clock()).total_seconds()
                # Give a cold backend time to finish training, then report unavailable
                # fields honestly if kickoff is only two minutes away.
                if (not payload.get("prediction") or not payload.get("total_prediction")) and seconds_left > 120:
                    continue
                for config in relevant:
                    await self.deliver(config, current_game, payload)
            except Exception as exc:
                self.last_error = f"Prediction request failed ({type(exc).__name__}). Retrying next minute."
                logger.warning("Prediction preparation failed for %s (%s)", game["game_id"], type(exc).__name__)

    async def deliver(self, config, game, payload):
        # Settings and kickoff can change while HTTP calls are in progress.
        config = self.store.settings(config["guild_id"])
        if not config or not config["enabled"] or not due(game, self.clock(), config["minutes"]):
            return
        row = self.store.claim(config, game, payload, self.clock())
        if not row:
            return
        try:
            if row["attempts"] > 1:
                existing = await self.transport.find_existing(row)
                if existing:
                    self.store.mark_sent(row, existing)
                    return
            config = self.store.settings(row["guild_id"])
            if not config or not config["enabled"]:
                return
            self.store.prepare_retry(row, config["channel_id"], game, payload)
            if not due(game, self.clock(), config["minutes"]):
                return
            row["kickoff"] = game["kickoff"]
            message_id = await self.transport.send(config["channel_id"], row, matchup_embed(payload))
            self.store.mark_sent(row, message_id)
        except Exception as exc:
            # Keep the attempted payload and channel for recovery after an uncertain send.
            self.store.failed(row, type(exc).__name__, self.clock())
            self.last_error = f"Alert delivery failed ({type(exc).__name__}). Check channel permissions."
            logger.warning("Alert delivery failed for guild %s game %s (%s)",
                           config["guild_id"], game["game_id"], type(exc).__name__)
