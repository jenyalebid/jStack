#!/usr/bin/env python3
"""The guest-side reads the work-harness scenarios share. Stdlib only, and
3.9-safe: it runs on whatever `python3` a clean guest has, before anything
jStack installs is trusted to work.

    work_probe.py api METHOD PATH [JSON-BODY]   the Hub's API, bearer attached
    work_probe.py py EXPR < file.json           EXPR over the JSON as `d`

`api` exits 1 on a non-2xx and puts the status and body on stderr, so a
scenario can tell a refused call from an empty answer. Its token is
$JSTACK_TOKEN and its base $JSTACK_API (default the local Hub on 9090).
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("JSTACK_API", "http://127.0.0.1:9090/api/jremote/v1")


def api(method, path, body=None):
    data = body.encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method.upper())
    req.add_header("Authorization", "Bearer " + os.environ.get("JSTACK_TOKEN", ""))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            sys.stdout.write(r.read().decode())
            return 0
    except urllib.error.HTTPError as e:
        sys.stderr.write("%s %s -> %s %s\n" % (method, path, e.code, e.read().decode()[:400]))
        return 1


def main(argv):
    if len(argv) >= 3 and argv[0] == "api":
        return api(argv[1], argv[2], argv[3] if len(argv) > 3 else None)
    if len(argv) == 2 and argv[0] == "py":
        d = json.load(sys.stdin)
        out = eval(argv[1], {"d": d, "json": json})  # the scenario's own text
        print(out if isinstance(out, str) else json.dumps(out))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
