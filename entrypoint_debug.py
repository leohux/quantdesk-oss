#!/usr/bin/env python3
"""Debug entrypoint: trace WebSocket send messages."""
import logging
import sys
sys.path.insert(0, "/app")
logging.basicConfig(level=logging.ERROR, stream=sys.stderr)

# Patch starlette WebSocket to trace all method calls
from starlette.websockets import WebSocket as _WS
import starlette.types as _st

_orig_ws_init = _WS.__init__
def _debug_ws_init(self, scope, receive, send):
    _orig_ws_init(self, scope, receive, send)
    # Wrap send to log all messages
    orig_send = self._send
    async def debug_send(message):
        msg_type = message.get("type", "?")
        status = message.get("status_code", "")
        logging.error(f">>> WS.SEND: type={msg_type} status={status} scope_path={self.scope.get('path')}")
        if msg_type == "http.response.start":
            import traceback
            logging.error(">>> HTTP.RESPONSE traceback:")
            traceback.print_stack(limit=10)
        return await orig_send(message)
    self._send = debug_send
_WS.__init__ = _debug_ws_init

from api.main import app
import uvicorn

logging.error(">>> Starting uvicorn on 0.0.0.0:8000 ws=wsproto")
uvicorn.run(app, host="0.0.0.0", port=8000, ws="wsproto", log_level="info")
