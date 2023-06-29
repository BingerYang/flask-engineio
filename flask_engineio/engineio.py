import logging
from uuid import uuid4

import gevent

from ._sockets import Sockets
from gevent import queue, contextvars
from flask import copy_current_request_context, session, request
from geventwebsocket import WebSocketError

ctx_engine = contextvars.ContextVar("engine.io")

logger = logging.getLogger(__name__)


def copy_current_app_context(f):
    from flask.globals import _app_ctx_stack
    ctx = _app_ctx_stack.top

    def wrapper(*args, **kwargs):
        with ctx:
            return f(*args, **kwargs)

    return wrapper


class EngineAsync(object):
    def __init__(self, rule_event_cb, app=None, path="/", **route_options):
        self.sockets = None
        self._path = path
        self._rule_event_cb = rule_event_cb
        self.route_options = route_options
        self._rule_map = {}
        if app:
            self.init_app(app)

    def init_app(self, app):
        self.sockets = Sockets(app=app)
        endpoint = self.route_options.pop('endpoint', None)
        self.sockets.add_url_rule(self._path, endpoint, self._receive_at_route, **self.route_options)

    @classmethod
    def context(cls) -> dict:
        return ctx_engine.get()

    @classmethod
    def sendto(cls, ws, data):
        try:
            ws.send(data)
        except WebSocketError as e:
            logger.warning(e)

    def __handle_data(self, que: queue.Queue, sid_spaces):
        ctx_engine.set(sid_spaces)

        session["ws"] = ws = sid_spaces["ws"]
        while True:
            _code, event, message = que.get()
            if _code == -1:
                break
            try:
                func_cb = self._rule_map.get(event)
                if func_cb:
                    try:
                        func_cb(ws, message)
                    except Exception as e:
                        logger.exception(e)
                else:
                    logger.warning("no found event:{}".format(event))
            except Exception as e:
                logger.warning("error：{}".format(e.args))
                logger.exception(e)
                continue

    def __handle_receive(self, que_pool, sid_spaces):
        ws = sid_spaces["ws"]
        try:
            while True:
                message = ws.receive()

                try:
                    event, info = self._rule_event_cb(message)
                except:
                    # 规则异常
                    logger.warning("No parsable event information found:{}".format(message))
                    continue
                q = que_pool.get(event)
                if q:
                    q.put((0, event, info))

        except WebSocketError as e:
            # closed 已经关闭
            logger.info("websocket closed")
            for event, q in que_pool.items():
                q.put((-1, "exit", None))

    def _receive_at_route(self, ws):
        sid_spaces = {"ws": ws, "sid": uuid4().hex}
        que_pool = {}
        tasks = []

        @copy_current_app_context
        @copy_current_request_context  # 只能拷贝：request、g、session 不能变更影响通知其他的
        def handle_data(*args, **kwargs):
            request.sid_spaces = kwargs["sid_spaces"]
            return self.__handle_data(*args, **kwargs)

        for event, func in self._rule_map.items():
            que = que_pool[event] = queue.Queue()
            _task = gevent.spawn(handle_data, que, sid_spaces=sid_spaces)
            tasks.append(_task)
        tasks.append(gevent.spawn(self.__handle_receive, que_pool, sid_spaces))
        gevent.joinall(tasks)

    def on(self, event):
        def decorator(f):
            self._rule_map[event] = f
            return f

        return decorator
