import logging
import urllib
from contextlib import suppress
from typing import Any, Dict, List, Optional

import tornado.web
from asgiref.sync import sync_to_async
from django import http
from django.core import signals
from django.core.handlers import base
from django.core.handlers.wsgi import WSGIRequest, get_script_name
from django.http import HttpRequest, HttpResponse
from django.urls import set_script_prefix
from django.utils.cache import patch_vary_headers
from tornado.iostream import StreamClosedError
from tornado.wsgi import WSGIContainer

from zerver.lib.response import AsynchronousResponse, json_response
from zerver.tornado.descriptors import get_descriptor_by_handler_id

current_handler_id = 0
handlers: Dict[int, "AsyncDjangoHandler"] = {}
fake_wsgi_container = WSGIContainer(lambda environ, start_response: [])


def get_handler_by_id(handler_id: int) -> "AsyncDjangoHandler":
    return handlers[handler_id]


def allocate_handler_id(handler: "AsyncDjangoHandler") -> int:
    global current_handler_id
    handlers[current_handler_id] = handler
    handler_id = current_handler_id
    current_handler_id += 1
    return handler_id


def clear_handler_by_id(handler_id: int) -> None:
    del handlers[handler_id]


def handler_stats_string() -> str:
    return f"{len(handlers)} handlers, latest ID {current_handler_id}"


def finish_handler(handler_id: int, event_queue_id: str, contents: List[Dict[str, Any]]) -> None:
    try:
        # We do the import during runtime to avoid cyclic dependency
        # with zerver.lib.request
        from zerver.lib.request import RequestNotes
        from zerver.middleware import async_request_timer_restart

        # We call async_request_timer_restart here in case we are
        # being finished without any events (because another
        # get_events request has supplanted this request)
        handler = get_handler_by_id(handler_id)
        request = handler._request
        assert request is not None
        async_request_timer_restart(request)
        log_data = RequestNotes.get_notes(request).log_data
        assert log_data is not None
        if len(contents) != 1:
            log_data["extra"] = f"[{event_queue_id}/1]"
        else:
            log_data["extra"] = "[{}/1/{}]".format(event_queue_id, contents[0]["type"])

        tornado.ioloop.IOLoop.current().add_callback(
            handler.zulip_finish,
            dict(result="success", msg="", events=contents, queue_id=event_queue_id),
            request,
        )
    except Exception as e:
        if not (
            (isinstance(e, OSError) and str(e) == "Stream is closed")
            or (isinstance(e, AssertionError) and str(e) == "Request closed")
        ):
            logging.exception(
                "Got error finishing handler for queue %s", event_queue_id, stack_info=True
            )


class AsyncDjangoHandler(tornado.web.RequestHandler):
    handler_id: int

    def initialize(self, django_handler: base.BaseHandler) -> None:
        self.django_handler = django_handler

        # Prevent Tornado from automatically finishing the request
        self._auto_finish = False

        # Handler IDs are allocated here, and the handler ID map must
        # be cleared when the handler finishes its response
        self.handler_id = allocate_handler_id(self)

        self._request: Optional[HttpRequest] = None

    def __repr__(self) -> str:
        descriptor = get_descriptor_by_handler_id(self.handler_id)
        return f"AsyncDjangoHandler<{self.handler_id}, {descriptor}>"

    async def convert_tornado_request_to_django_request(self) -> HttpRequest:
        # This takes the WSGI environment that Tornado received (which
        # fully describes the HTTP request that was sent to Tornado)
        # and pass it to Django's WSGIRequest to generate a Django
        # HttpRequest object with the original Tornado request's HTTP
        # headers, parameters, etc.
        environ = fake_wsgi_container.environ(self.request)
        environ["PATH_INFO"] = urllib.parse.unquote(environ["PATH_INFO"])

        # Django WSGIRequest setup code that should match logic from
        # Django's WSGIHandler.__call__ before the call to
        # `get_response()`.
        set_script_prefix(get_script_name(environ))
        await sync_to_async(
            lambda: signals.request_started.send(sender=type(self.django_handler)),
            thread_sensitive=True,
        )()
        self._request = WSGIRequest(environ)

        # We do the import during runtime to avoid cyclic dependency
        from zerver.lib.request import RequestNotes

        # Provide a way for application code to access this handler
        # given the HttpRequest object.
        RequestNotes.get_notes(self._request).tornado_handler_id = self.handler_id

        return self._request

    async def write_django_response_as_tornado_response(self, response: HttpResponse) -> None:
        # This takes a Django HttpResponse and copies its HTTP status
        # code, headers, cookies, and content onto this
        # tornado.web.RequestHandler (which is how Tornado prepares a
        # response to write).

        # Copy the HTTP status code.
        self.set_status(response.status_code)

        # Copy the HTTP headers (iterating through a Django
        # HttpResponse is the way to access its headers as key/value pairs)
        for h in response.items():
            self.set_header(h[0], h[1])

        # Copy any cookies
        if not hasattr(self, "_new_cookies"):
            self._new_cookies: List[http.cookie.SimpleCookie[str]] = []
        self._new_cookies.append(response.cookies)

        # Copy the response content
        self.write(response.content)

        # Close the connection.
        # While writing the response, we might realize that the
        # user already closed the connection; that is fine.
        with suppress(StreamClosedError):
            await self.finish()

    async def get(self, *args: Any, **kwargs: Any) -> None:
        request = await self.convert_tornado_request_to_django_request()
        response = await sync_to_async(
            lambda: self.django_handler.get_response(request), thread_sensitive=True
        )()

        try:
            if isinstance(response, AsynchronousResponse):
                # We import async_request_timer_restart during runtime
                # to avoid cyclic dependency with zerver.lib.request
                from zerver.middleware import async_request_timer_stop

                # For asynchronous requests, this is where we exit
                # without returning the HttpResponse that Django
                # generated back to the user in order to long-poll the
                # connection.  We save some timers here in order to
                # support accurate accounting of the total resources
                # consumed by the request when it eventually returns a
                # response and is logged.
                async_request_timer_stop(request)
            else:
                # For normal/synchronous requests that don't end up
                # long-polling, we just need to write the HTTP
                # response that Django prepared for us via Tornado.

                # Mark this handler ID as finished for Zulip's own tracking.
                clear_handler_by_id(self.handler_id)

                assert isinstance(response, HttpResponse)
                await self.write_django_response_as_tornado_response(response)
        finally:
            # Tell Django that we're done processing this request on
            # the Django side; this triggers cleanup work like
            # resetting the urlconf and any cache/database
            # connections.
            await sync_to_async(response.close, thread_sensitive=True)()

    async def head(self, *args: Any, **kwargs: Any) -> None:
        await self.get(*args, **kwargs)

    async def post(self, *args: Any, **kwargs: Any) -> None:
        await self.get(*args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        await self.get(*args, **kwargs)

    def on_connection_close(self) -> None:
        # Register a Tornado handler that runs when client-side
        # connections are closed to notify the events system.
        #
        # TODO: Theoretically, this code should run when you Ctrl-C
        # curl to cause it to break a `GET /events` connection, but
        # that seems to no longer run this code.  Investigate what's up.
        client_descriptor = get_descriptor_by_handler_id(self.handler_id)
        if client_descriptor is not None:
            client_descriptor.disconnect_handler(client_closed=True)

    async def zulip_finish(self, result_dict: Dict[str, Any], old_request: HttpRequest) -> None:
        # Function called when we want to break a long-polled
        # get_events request and return a response to the client.

        # Marshall the response data from result_dict.
        if result_dict["result"] == "error":
            self.set_status(400)

        # The `result` dictionary contains the data we want to return
        # to the client.  We want to do so in a proper Tornado HTTP
        # response after running the Django response middleware (which
        # does things like log the request, add rate-limit headers,
        # etc.).  The Django middleware API expects to receive a fresh
        # HttpRequest object, and so to minimize hacks, our strategy
        # is to create a duplicate Django HttpRequest object, tagged
        # to automatically return our data in its response, and call
        # Django's main self.get_response() handler to generate an
        # HttpResponse with all Django middleware run.
        request = await self.convert_tornado_request_to_django_request()

        # We import RequestNotes during runtime to avoid
        # cyclic import
        from zerver.lib.request import RequestNotes

        request_notes = RequestNotes.get_notes(request)
        old_request_notes = RequestNotes.get_notes(old_request)

        # Add to this new HttpRequest logging data from the processing of
        # the original request; we will need these for logging.
        request_notes.log_data = old_request_notes.log_data
        if request_notes.rate_limit is not None:
            request_notes.rate_limit = old_request_notes.rate_limit
        if request_notes.requestor_for_logs is not None:
            request_notes.requestor_for_logs = old_request_notes.requestor_for_logs
        request.user = old_request.user
        request_notes.client = old_request_notes.client
        request_notes.client_name = old_request_notes.client_name
        request_notes.client_version = old_request_notes.client_version

        # The saved_response attribute, if present, causes
        # rest_dispatch to return the response immediately before
        # doing any work.  This arrangement allows Django's full
        # request/middleware system to run unmodified while avoiding
        # running expensive things like Zulip's authentication code a
        # second time.
        request_notes.saved_response = json_response(
            res_type=result_dict["result"], data=result_dict, status=self.get_status()
        )

        response = await sync_to_async(
            lambda: self.django_handler.get_response(request), thread_sensitive=True
        )()
        try:
            # Explicitly mark requests as varying by cookie, since the
            # middleware will not have seen a session access
            patch_vary_headers(response, ("Cookie",))
            assert isinstance(response, HttpResponse)
            await self.write_django_response_as_tornado_response(response)
        finally:
            # Tell Django we're done processing this request
            await sync_to_async(response.close, thread_sensitive=True)()
