import os
import asyncio
import logging
import traceback
import h11
from quart import make_response, request, Quart
from quart.app import _cancel_all_tasks
from hypercorn.config import Config
from hypercorn.asyncio import serve
from localstack import config
from localstack.utils.common import TMP_THREADS, FuncThread, load_file
from localstack.utils.http_utils import uses_chunked_encoding
from localstack.utils.async_utils import run_sync, ensure_event_loop

LOG = logging.getLogger(__name__)

HTTP_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS', 'PATCH']


def setup_quart_logging():
    # set up loggers to avoid duplicate log lines in quart
    for name in ['quart.app', 'quart.serving']:
        log = logging.getLogger(name)
        log.setLevel(logging.INFO if config.DEBUG else logging.WARNING)
        for hdl in list(log.handlers):
            log.removeHandler(hdl)


def apply_patches():

    def InformationalResponse_init(self, *args, **kwargs):
        if kwargs.get('status_code') == 100 and not kwargs.get('reason'):
            # add missing "100 Continue" keyword which makes boto3 HTTP clients fail/hang
            kwargs['reason'] = 'Continue'
        InformationalResponse_init_orig(self, *args, **kwargs)

    InformationalResponse_init_orig = h11.InformationalResponse.__init__
    h11.InformationalResponse.__init__ = InformationalResponse_init


def run_server(port, handler=None, asynchronous=True, ssl_creds=None):

    ensure_event_loop()
    app = Quart(__name__)

    @app.route('/', methods=HTTP_METHODS, defaults={'path': ''})
    @app.route('/<path:path>', methods=HTTP_METHODS)
    async def index(path=None):
        response = await make_response('{}')
        if handler:
            data = await request.get_data()
            try:
                result = await run_sync(handler, request, data)
            except Exception as e:
                LOG.warning('Error in proxy handler for request %s %s: %s %s' %
                    (request.method, request.url, e, traceback.format_exc()))
                response.status_code = 500
                return response
            if result is not None:
                is_chunked = uses_chunked_encoding(result)
                result_content = result.content or ''
                response = await make_response(result_content)
                response.status_code = result.status_code
                if is_chunked:
                    response.headers.pop('Content-Length', None)
                result.headers.pop('Server', None)
                result.headers.pop('Date', None)
                response.headers.update(dict(result.headers))
                # set multi-value headers
                multi_value_headers = getattr(result, 'multi_value_headers', {})
                for key, values in multi_value_headers.items():
                    for value in values:
                        response.headers.add_header(key, value)
                # set default headers, if required
                if 'Content-Length' not in response.headers and not is_chunked:
                    response.headers['Content-Length'] = str(len(result_content) if result_content else 0)
                if 'Connection' not in response.headers:
                    response.headers['Connection'] = 'close'
        return response

    def run_app_sync(*args, loop=None, shutdown_event=None):
        kwargs = {}
        config = Config()
        cert_file_name, key_file_name = ssl_creds or (None, None)
        if cert_file_name:
            kwargs['certfile'] = cert_file_name
            config.certfile = cert_file_name
        if key_file_name:
            kwargs['keyfile'] = key_file_name
            config.keyfile = key_file_name
        setup_quart_logging()
        config.bind = ['0.0.0.0:%s' % port]
        loop = loop or ensure_event_loop()
        run_kwargs = {}
        if shutdown_event:
            run_kwargs['shutdown_trigger'] = shutdown_event.wait
        try:
            try:
                return loop.run_until_complete(serve(app, config, **run_kwargs))
            except Exception as e:
                LOG.info('Error running server event loop on port %s: %s %s' % (port, e, traceback.format_exc()))
                if 'SSL' in str(e):
                    c_exists = os.path.exists(cert_file_name)
                    k_exists = os.path.exists(key_file_name)
                    c_size = len(load_file(cert_file_name)) if c_exists else 0
                    k_size = len(load_file(key_file_name)) if k_exists else 0
                    LOG.warning('Unable to create SSL context. Cert files exist: %s %s (%sB), %s %s (%sB)' %
                                (cert_file_name, c_exists, c_size, key_file_name, k_exists, k_size))
                raise
        finally:
            try:
                _cancel_all_tasks(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    class ProxyThread(FuncThread):
        def __init__(self):
            FuncThread.__init__(self, self.run_proxy, None)

        def run_proxy(self, *args):
            loop = ensure_event_loop()
            self.shutdown_event = asyncio.Event()
            run_app_sync(loop=loop, shutdown_event=self.shutdown_event)

        def stop(self, quiet=None):
            self.shutdown_event.set()

    def run_in_thread():
        thread = ProxyThread()
        thread.start()
        TMP_THREADS.append(thread)
        return thread

    if asynchronous:
        return run_in_thread()

    return run_app_sync()


# apply patches on startup
apply_patches()
