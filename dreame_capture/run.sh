#!/bin/sh
set -e

# MediaMTX with no config file uses its built-in defaults, which include a
# catch-all path ("all_others") - any path name can be published/read on
# demand without pre-declaring it, which is what lets us use the vacuum's
# did as an RTSP path directly.
mediamtx ./mediamtx.yml &

# Control panel UI (Ingress-only, separate port from the token-authed API in
# app.py so the UI is never reachable from the LAN).
python3 ui.py &

exec python3 app.py
