"""Bound decoded HTTP body bytes before routing or costly external calls."""

from starlette.datastructures import Headers

from app.errors import error_response


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes, audio_prefix=None, audio_max_bytes=10485760):
        self.app, self.max_bytes = app, max_bytes
        self.audio_prefix, self.audio_max_bytes = audio_prefix, audio_max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        maximum = self.max_bytes
        if (self.audio_prefix and scope.get("method") == "PUT"
                and scope.get("path", "").startswith(self.audio_prefix)
                and scope.get("path", "").endswith("/upload")):
            maximum = self.audio_max_bytes
        length = headers.get("content-length")
        invalid = False
        try:
            invalid = length is not None and (int(length) < 0 or int(length) > maximum)
        except ValueError:
            invalid = True
        # Reject compressed bodies to prevent decompression-size bypasses downstream.
        invalid = invalid or headers.get("content-encoding", "identity") != "identity"
        chunks, size = [], 0
        if not invalid:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > maximum:
                    invalid = True
                    break
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
        if invalid:
            response = error_response("request_too_large", "Request body is not supported",
                                      scope["state"]["request_id"], 413)
            await response(scope, receive, send)
            return
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
