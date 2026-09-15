#!/usr/bin/env python3
"""Read-only MCP adapter for the SourceKit-LSP bundled with Xcode.

Codex accepts MCP tools but does not consume Claude's lspServers catalog.
Keep the same language server and expose its navigation/diagnostic requests.
"""
import atexit
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path


class LanguageServer:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        command = os.environ.get("JSTACK_SOURCEKIT_LSP")
        argv = [command] if command else ["xcrun", "sourcekit-lsp"]
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=self.workspace)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.buffer, self.serial, self.opened = b"", 0, {}
        self.diagnostics = {}
        try:
            self.call("initialize", {"processId": os.getpid(), "rootUri": self.workspace.as_uri(),
                                  "capabilities": {"textDocument": {"publishDiagnostics": {}},
                                                   "workspace": {"configuration": True}},
                                  "workspaceFolders": [{"uri": self.workspace.as_uri(),
                                                        "name": self.workspace.name}]})
        except Exception:
            self.close()
            raise
        self.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def send(self, row):
        data = json.dumps(row).encode()
        self.proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        self.proc.stdin.flush()

    def receive(self, deadline):
        while time.monotonic() < deadline:
            if b"\r\n\r\n" in self.buffer:
                header, body = self.buffer.split(b"\r\n\r\n", 1)
                length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                if len(body) >= length:
                    self.buffer = body[length:]
                    return json.loads(body[:length])
            if self.selector.select(max(0, deadline - time.monotonic())):
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("SourceKit-LSP exited")
                self.buffer += chunk
        raise TimeoutError("SourceKit-LSP request timed out")

    def call(self, method, params):
        self.serial += 1
        request_id = self.serial
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while True:
            row = self.receive(deadline)
            if row.get("method"):
                if "id" in row:
                    result = None
                    if row["method"] == "workspace/configuration":
                        result = [None] * len(row.get("params", {}).get("items", []))
                    elif row["method"] == "workspace/workspaceFolders":
                        result = [{"uri": self.workspace.as_uri(), "name": self.workspace.name}]
                    self.send({"jsonrpc": "2.0", "id": row["id"], "result": result})
                elif row["method"] == "textDocument/publishDiagnostics":
                    data = row.get("params") or {}
                    self.diagnostics[data["uri"]] = data.get("diagnostics", [])
                continue
            if row.get("id") == request_id:
                if "error" in row:
                    raise RuntimeError(row["error"].get("message", "LSP request failed"))
                return row.get("result")

    def document(self, path):
        path = Path(path).resolve()
        if path.suffix != ".swift" or not path.is_file():
            raise ValueError("file must name an existing Swift file")
        uri = path.as_uri()
        content = path.read_text()
        previous = self.opened.get(uri)
        if previous is None:
            self.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
                "textDocument": {"uri": uri, "languageId": "swift", "version": 1, "text": content}}})
            self.opened[uri] = (1, content)
        elif previous[1] != content:
            version = previous[0] + 1
            self.send({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": {
                "textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": content}]}})
            self.opened[uri] = (version, content)
        return uri

    def close(self):
        self.selector.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


METHODS = {"definition": "textDocument/definition", "references": "textDocument/references",
           "hover": "textDocument/hover", "symbols": "textDocument/documentSymbol",
           "diagnostics": "textDocument/diagnostic"}
SERVERS = {}


def tools():
    result = []
    for name in METHODS:
        props = {"workspace": {"type": "string", "description": "Absolute Swift package or Xcode project directory."},
                 "file": {"type": "string", "description": "Absolute path of an existing .swift file."}}
        required = ["workspace", "file"]
        if name in ("definition", "references", "hover"):
            props.update(line={"type": "integer", "minimum": 1, "description": "One-based line."},
                         column={"type": "integer", "minimum": 1, "description": "One-based UTF-16 column."})
            required += ["line", "column"]
        result.append({"name": name, "description": f"Swift {name} from SourceKit-LSP; reads current files without modifying them.",
                       "inputSchema": {"type": "object", "properties": props, "required": required,
                                       "additionalProperties": False},
                       "annotations": {"readOnlyHint": True, "destructiveHint": False}})
    return result


def invoke(name, args):
    if name not in METHODS:
        raise ValueError("unknown tool")
    workspace = str(Path(args["workspace"]).resolve())
    if not Path(args["workspace"]).is_absolute() or not Path(args["file"]).is_absolute():
        raise ValueError("workspace and file must be absolute paths")
    if workspace not in SERVERS:
        SERVERS[workspace] = LanguageServer(workspace)
    server = SERVERS[workspace]
    uri = server.document(args["file"])
    params = {"textDocument": {"uri": uri}}
    if name in ("definition", "references", "hover"):
        params["position"] = {"line": max(0, int(args["line"]) - 1),
                              "character": max(0, int(args["column"]) - 1)}
    if name == "references":
        params["context"] = {"includeDeclaration": True}
    return server.call(METHODS[name], params)


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method, params = request.get("method"), request.get("params") or {}
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18",
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "jstack-swift-lsp", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": tools()}
            elif method == "tools/call":
                try:
                    value = invoke(params["name"], params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": json.dumps(value)}]}
                except Exception as exc:
                    result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            elif method == "ping":
                result = {}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unknown method"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
        except (ValueError, KeyError, TypeError):
            continue


@atexit.register
def close_servers():
    for server in SERVERS.values():
        server.close()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
