"""Compress text responses without recompressing existing binary assets."""
from starlette.middleware.gzip import GZipMiddleware


class TextGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").lower()
        if path.endswith((".png", ".webp", ".jpg", ".jpeg", ".zip", ".gz", ".bin", ".mp4")):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)
