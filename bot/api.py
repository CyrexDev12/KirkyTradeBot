from urllib.parse import quote

import httpx


class PredictionAPI:
    def __init__(self, base_url):
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30)

    async def get(self, path, **params):
        response = await self.client.get(path, params={k: v for k, v in params.items() if v is not None})
        response.raise_for_status()
        return response.json()

    async def games(self, season=None, week=None):
        return await self.get("/api/games", season=season, week=week)

    async def upcoming(self, hours=48):
        return await self.get("/api/upcoming", hours=hours)

    async def forecast(self, game_id):
        return await self.get(f"/api/games/{quote(game_id, safe='')}/forecast")

    async def close(self):
        await self.client.aclose()
