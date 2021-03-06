import logging
import typing

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

RequestResponseEndpoint = typing.Callable[
    [Request], typing.Awaitable[Response]
]
DispatchFunction = typing.Callable[
    [Request, RequestResponseEndpoint], typing.Awaitable[Response]
]


class AbstractHTTPMiddleware:
    def __init__(
        self, app: ASGIApp, dispatch: typing.Optional[DispatchFunction] = None
    ) -> None:
        self.app = app
        self.dispatch_func = self.dispatch if dispatch is None else dispatch

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def call_next(request: Request) -> Response:
            app_exc: typing.Optional[Exception] = None
            send_stream, recv_stream = anyio.create_memory_object_stream()

            async def coro() -> None:
                nonlocal app_exc

                async with send_stream:
                    try:
                        await self.app(
                            scope, request.receive, send_stream.send
                        )
                    except Exception as exc:
                        app_exc = exc

            task_group.start_soon(coro)

            try:
                message = await recv_stream.receive()
            except anyio.EndOfStream:
                if app_exc is not None:
                    raise app_exc from None  # pylint: disable=raising-bad-type

                if await request.is_disconnected():
                    # client is disconnected,
                    # log info and return new response with status 499
                    return self._client_closed_connection()

                # handle situation when client is connected and
                # response from previous middleware is missing
                return self._client_is_connected_not_response_error()

            assert message["type"] == "http.response.start"

            async def body_stream() -> typing.AsyncGenerator[bytes, None]:
                async with recv_stream:
                    async for message in recv_stream:
                        assert message["type"] == "http.response.body"
                        yield message.get("body", b"")

                if app_exc is not None:
                    raise app_exc from None  # pylint: disable=raising-bad-type

            response = StreamingResponse(
                status_code=message["status"], content=body_stream()
            )
            response.raw_headers = message["headers"]
            return response

        async with anyio.create_task_group() as task_group:
            request = Request(scope, receive=receive)
            response = await self.dispatch_func(request, call_next)
            await response(scope, receive, send)
            task_group.cancel_scope.cancel()

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        raise NotImplementedError()

    @staticmethod
    def _client_closed_connection() -> Response:
        # client is disconnected, must return response
        # (protecting another middleware from failing)
        logging.info(
            "Client closed connection, returning new response with status"
            " code 499"
        )

        return Response(status_code=499, content="Client closed connection")

    @staticmethod
    def _client_is_connected_not_response_error():
        # client is not disconected,
        # this situation should probably never happen,
        # but log potential error and send new response with status 500
        logging.error("No response returned, EndOfStream, sending status 500.")

        return JSONResponse(
            status_code=500,
            content={"errors": [{"message": "something went wrong"}]},
        )
