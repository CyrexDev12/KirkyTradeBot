import os
from pathlib import Path
from urllib.parse import quote

import httpx


def operator_token():
    path = os.getenv("NFL_PAPER_OPERATOR_TOKEN_FILE")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8-sig").strip() or None
    return os.getenv("NFL_PAPER_OPERATOR_TOKEN") or None


class PredictionAPI:
    def __init__(self, base_url, token=None):
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30)
        self.token = token if token is not None else operator_token()

    async def get(self, path, *, timeout=30, **params):
        response = await self.client.get(path, timeout=timeout,
                                         params={k: v for k, v in params.items() if v is not None})
        response.raise_for_status()
        return response.json()

    async def games(self, season=None, week=None):
        return await self.get("/api/games", season=season, week=week)

    async def upcoming(self, hours=48):
        return await self.get("/api/upcoming", hours=hours)

    async def forecast(self, game_id):
        return await self.get(f"/api/games/{quote(game_id, safe='')}/forecast")

    async def paper(self, path):
        return await self.get("/api/paper" + path)

    async def crypto(self, path, **params):
        return await self.get("/api/crypto" + path, **params)

    async def updown(self, path, **params):
        return await self.get("/api/updown" + path, **params)

    async def gold(self, path, **params):
        return await self.get("/api/gold" + path, **params)

    async def tracker(self, path, **params):
        return await self.get("/api/tracker" + path, **params)

    async def player_projection(self, player, stat, *, team=None, season=None, week=None):
        return await self.get("/api/players/projection", player=player, stat=stat, team=team,
                              season=season, week=week, timeout=90)

    async def player_picks(self):
        return await self.get("/api/players/picks", timeout=120)

    async def team_points(self, team, *, season=None):
        return await self.get("/api/graphs/team-points", team=team, season=season)

    def _operator_headers(self):
        if not self.token:
            raise PermissionError("NFL_PAPER_OPERATOR_TOKEN is not configured for the bot")
        return {"X-Paper-Operator-Token": self.token}

    async def paper_operator(self, path, body):
        response = await self.client.post("/api/paper" + path, json=body, headers=self._operator_headers())
        response.raise_for_status()
        return response.json()

    async def paper_outbox(self, channels=None):
        params = [("channels", channel) for channel in (channels or [])]
        if channels and "parlay" in channels:
            params.append(("include_current_parlay", "true"))
        response = await self.client.get("/api/paper/outbox/pending", params=params, headers=self._operator_headers())
        response.raise_for_status()
        return response.json()

    async def paper_ack(self, publication_id, revision, message_id, error):
        response = await self.client.post(f"/api/paper/outbox/{quote(publication_id, safe='')}/ack",
                                          json={"revision": revision, "message_id": message_id, "error": error},
                                          headers=self._operator_headers())
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self.client.aclose()
