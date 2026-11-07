#!/usr/bin/env python3
"""
repofree - a self-refreshing map of a code repository, for people and AI assistants.

* One file, Python 3.9+ standard library only. No installs, no network, no telemetry.
* Every command first re-reads only the files that changed since the last run,
  so answers always match what is on disk.
* Maps functions/classes, who-calls-what, imports and the page -> script links,
  and the "string" wiring that call graphs usually miss: HTTP routes and the
  places that call them, database collections (with read/write), custom events,
  browser storage keys, environment variables, window globals, HTML handlers and
  cache-busting versions.

Run `repofree --help` or `repofree agent-help`.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from collections import defaultdict, deque

__version__ = "0.1.0"
SCHEMA = 1
EXTRACTOR_VERSION = 1  # bump whenever extraction output changes -> forces a rescan

MAX_FILE_BYTES = 3_000_000

LANG_BY_EXT = {
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js",
    ".html": "html", ".htm": "html", ".vue": "html", ".svelte": "html",
    ".py": "py",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".cs": "csharp", ".php": "php", ".rb": "ruby",
    ".swift": "swift", ".dart": "dart",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp",
}
FULL_TIER = {"js", "html", "py"}

DEFAULT_SKIP_DIRS = {
    ".git", "node_modules", "bower_components", "dist", "build", ".next", ".nuxt",
    ".svelte-kit", ".output", "coverage", ".nyc_output", "vendor", "venv", ".venv",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", ".repofree", "Pods",
    ".dart_tool", ".gradle", "target", ".idea", ".vscode", ".cache",
    ".parcel-cache", ".turbo", ".vercel", ".firebase", "DerivedData",
}

GUIDE = """\
repofree - quick guide (for people and AI assistants)

WHAT IT IS
  A map of this repository: functions, who calls what, which page loads which
  script, HTTP routes and their callers, database collections (read/write),
  custom events, storage keys, env vars, globals, cache-busting versions.
  It refreshes itself before every command (only changed files are re-read),
  so results always match the files on disk. Nothing leaves the machine.

HOW TO USE IT WELL
  1. Use it to FIND things fast, then open the real file at the line it gives
     before you change or claim anything.
  2. It is pattern-based. It can miss dynamic calls (names built at runtime,
     functions passed around through variables). "No callers found" means
     "none found by the map", not "safe to delete".
  3. Add --json to any command for machine-readable output.

COMMANDS
  repofree status                 what is mapped, when it was refreshed
  repofree find NAME              where a function/class is defined (fuzzy)
  repofree callers NAME           every call site (incl. HTML onclick, callbacks)
  repofree callees NAME           what a function calls
  repofree impact NAME|FILE       callers of callers (blast radius) + pages affected
  repofree path FROM TO           shortest call chain between two functions
  repofree file PATH              one file: definitions, imports, facts, who loads it
  repofree uses TEXT              anything mentioning TEXT (routes, collections, keys...)
  repofree endpoints [TEXT]       server routes and the code that calls them
  repofree collections [NAME]     database collections: where read / written
  repofree events [NAME]          custom events: who fires, who listens
  repofree storage [KEY]          localStorage / sessionStorage / prefs keys
  repofree env [NAME]             environment variables / secrets used
  repofree globals [NAME]         window.* / globalThis.* assignments
  repofree services               serverless/cloud function exports and triggers
  repofree versions [--changed]   ?v= cache-busting: mismatches, missed bumps
  repofree compare A B [--map old=new]  what exists under A but not under B
  repofree report                 regenerate .repofree/REPOFREE.md (overview)
  repofree export [-o FILE]       one Markdown file to upload to a chat assistant
  repofree docs [--missing DIR]   browsable docs + interactive graph (.repofree/docs/index.html);
                                   lists functions that have no comment, most-used first
  repofree build                  full rebuild (normally never needed)

OUTPUT LOCATION
  .repofree/ inside the repo (it ignores itself in git), or --dir / REPOFREE_DIR.
"""

# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

_NONNL = re.compile(r"[^\n]")


def _now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _blank(src, ranges):
    """Replace every char inside ranges with a space, keeping newlines."""
    if not ranges:
        return src
    ranges = sorted(ranges)
    out = []
    last = 0
    for a, b in ranges:
        if b <= last:
            continue
        if a < last:
            a = last
        out.append(src[last:a])
        out.append(_NONNL.sub(" ", src[a:b]))
        last = b
    out.append(src[last:])
    return "".join(out)


def _keep_only(src, keep):
    """Blank everything except the given ranges (newlines are kept)."""
    keep = sorted(keep)
    out = []
    last = 0
    for a, b in keep:
        if a < last:
            a = last
        if b <= a:
            continue
        out.append(_NONNL.sub(" ", src[last:a]))
        out.append(src[a:b])
        last = b
    out.append(_NONNL.sub(" ", src[last:]))
    return "".join(out)


class Lines:
    def __init__(self, text):
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]
        self.total = len(text)

    def line(self, pos):
        return bisect.bisect_right(self.starts, pos)


def _line_end(s, p):
    e = s.find("\n", p)
    return len(s) if e < 0 else e


def _skip_balanced(s, p, o, c):
    depth = 0
    n = len(s)
    while p < n:
        ch = s[p]
        if ch == o:
            depth += 1
        elif ch == c:
            depth -= 1
            if depth == 0:
                return p + 1
        p += 1
    return n


def _expr_end(s, q):
    depth = 0
    n = len(s)
    i = q
    while i < n:
        ch = s[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and ch in ";,\n":
            return i
        i += 1
    return n


_TYPEISH = set("_$*<>,.:?|&[]'\"`#-!@")


def _scope_end(ns, p, braces, after_arrow=False):
    """Given a position just after a definition header, find where its body ends."""
    n = len(ns)

    def skip_ws(q):
        while q < n and ns[q] in " \t\r\n":
            q += 1
        return q

    if after_arrow:
        q = skip_ws(p)
        if q < n and ns[q] == "{":
            return braces.get(q, _line_end(ns, q))
        return _expr_end(ns, q)
    seen_paren = False
    steps = 0
    while p < n and steps < 3000:
        steps += 1
        q = skip_ws(p)
        if q >= n:
            return n
        crossed = "\n" in ns[p:q]
        c = ns[q]
        if c == "{":
            return braces.get(q, _line_end(ns, q))
        if crossed and seen_paren:
            return p
        p = q
        if c == "(":
            p = _skip_balanced(ns, p, "(", ")")
            seen_paren = True
            continue
        if ns.startswith("=>", p):
            return _scope_end(ns, p + 2, braces, after_arrow=True)
        if c.isalnum() or c in _TYPEISH:
            p += 1
            continue
        return _line_end(ns, p)
    return min(p, n)


def _brace_map(ns):
    st = []
    mp = {}
    for m in re.finditer(r"[{}]", ns):
        if m.group() == "{":
            st.append(m.start())
        elif st:
            mp[st.pop()] = m.start()
    return mp


def _enclosing(ranges, positions):
    """ranges sorted by (start, -end). Returns innermost range index per position."""
    order = sorted(range(len(positions)), key=lambda k: positions[k])
    res = [-1] * len(positions)
    stack = []
    di = 0
    nd = len(ranges)
    for k in order:
        pos = positions[k]
        while di < nd and ranges[di][0] <= pos:
            while stack and ranges[stack[-1]][1] < ranges[di][0]:
                stack.pop()
            stack.append(di)
            di += 1
        while stack and ranges[stack[-1]][1] < pos:
            stack.pop()
        res[k] = stack[-1] if stack else -1
    return res


# --------------------------------------------------------------------------
# lexer: blank comments and string contents while keeping positions
# --------------------------------------------------------------------------

_TPL_RE = re.compile(r"\\[\s\S]|`|\$\{")
_QUOTE_RES = {q: re.compile(r"\\[\s\S]|" + re.escape(q) + r"|\n") for q in ("'", '"', "`")}
_RX_BODY = re.compile(r"\\.|\[(?:\\.|[^\]\\\n])*\]|/|\n")
_REGEX_PREV_KW = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
                  "throw", "case", "do", "else", "yield", "await"}
_LEX_CACHE = {}


def _lex_pattern(js, slash, hashc, backtick):
    key = (js, slash, hashc, backtick)
    if key not in _LEX_CACHE:
        parts = []
        if slash:
            parts += [r"//", r"/\*"]
        if hashc:
            parts.append(r"#")
        parts += [r"'", r'"']
        if backtick:
            parts.append(r"`")
        if js:
            parts += [r"\{", r"\}", r"/"]
        _LEX_CACHE[key] = re.compile("|".join(parts))
    return _LEX_CACHE[key]


def _end_quote(src, k, q):
    pat = _QUOTE_RES[q]
    while True:
        m = pat.search(src, k)
        if not m:
            return len(src)
        t = m.group()
        if t[0] == "\\" and len(t) == 2:
            k = m.end()
            continue
        return m.start()


def _scan_template(src, k, strings, stack):
    start = k
    n = len(src)
    while True:
        m = _TPL_RE.search(src, k)
        if not m:
            strings.append((start, n))
            return n
        t = m.group()
        if t[0] == "\\":
            k = m.end()
            continue
        strings.append((start, m.start()))
        if t == "`":
            return m.end()
        stack.append(0)
        return m.end()


def _regex_allowed(src, j):
    k = j - 1
    while k >= 0 and src[k] in " \t\r\n":
        k -= 1
    if k < 0:
        return True
    c = src[k]
    if c in "(,=:[!&|?{};+-*%<>~^}":
        return True
    if c.isalnum() or c in "_$":
        e = k + 1
        while k >= 0 and (src[k].isalnum() or src[k] in "_$"):
            k -= 1
        return src[k + 1:e] in _REGEX_PREV_KW
    return False


def _end_regex(src, k):
    while True:
        m = _RX_BODY.search(src, k)
        if not m:
            return None
        t = m.group()
        if t == "/":
            return m.start()
        if t == "\n":
            return None
        k = m.end()


def lex(src, js=False, slash=True, hashc=False, backtick=None):
    """Return (comment_ranges, string_content_ranges).

    backtick: None | "template" (JS, with ${} expressions kept as code) | "raw" (Go)."""
    pat = _lex_pattern(js, slash, hashc, backtick)
    n = len(src)
    i = 0
    comments = []
    strings = []
    stack = []
    while i < n:
        m = pat.search(src, i)
        if not m:
            break
        j = m.start()
        t = m.group()
        if t == "//" or t == "#":
            e = _line_end(src, j)
            comments.append((j, e))
            i = e
        elif t == "/*":
            e = src.find("*/", j + 2)
            e = n if e < 0 else e + 2
            comments.append((j, e))
            i = e
        elif t in ("'", '"'):
            e = _end_quote(src, j + 1, t)
            strings.append((j + 1, e))
            i = e + 1 if (e < n and src[e] == t) else e
        elif t == "`":
            if backtick == "raw":
                e = src.find("`", j + 1)
                e = n if e < 0 else e
                strings.append((j + 1, e))
                i = e + 1
            else:
                i = _scan_template(src, j + 1, strings, stack)
        elif t == "{":
            if stack:
                stack[-1] += 1
            i = j + 1
        elif t == "}":
            if stack:
                if stack[-1] == 0:
                    stack.pop()
                    i = _scan_template(src, j + 1, strings, stack)
                    continue
                stack[-1] -= 1
            i = j + 1
        elif t == "/":
            if _regex_allowed(src, j):
                e = _end_regex(src, j + 1)
                if e is not None:
                    strings.append((j + 1, e))
                    i = e + 1
                    continue
            i = j + 1
        else:
            i = j + 1
    return comments, strings


# --------------------------------------------------------------------------
# "fact" extraction shared by all languages (works on comment-free text)
# --------------------------------------------------------------------------

_Q = r"""(['"`])"""
_COLL_RE = re.compile(r"""\.\s*(collection|collectionGroup|collection_group)\(\s*(['"`])([^'"`\n]+)\2""")
_COLL_MOD_RE = re.compile(r"""(?<![\w$.])(collection|collectionGroup|doc)\(\s*[A-Za-z_$][\w$.]*\s*,""")
_DOCPATH_RE = re.compile(r"""\.\s*(doc|document)\(\s*(['"`])([^'"`\n]+/[^'"`\n]+)\2""")
_FROM_RE = re.compile(r"""\.\s*from\(\s*(['"])([A-Za-z_][\w.]*)\1\s*\)""")
_FWD_OPS = re.compile(r"\.\s*(get|getAll|onSnapshot|stream|count|listDocuments|set|update|add|create|delete|insert|upsert|select)\s*\(")
_BACK_OPS = re.compile(r"(?<![\w$])(getDocs?|getDocFromServer|getDocsFromServer|getCountFromServer|onSnapshot|setDoc|updateDoc|addDoc|deleteDoc)\s*\([^;]*$")
_BACK_CHAIN = re.compile(r"\.\s*(get|getAll|set|update|create|delete)\s*\(\s*[^();]*$")
_READ_OPS = {"get", "getAll", "onSnapshot", "stream", "count", "listDocuments", "select",
             "getDoc", "getDocs", "getDocFromServer", "getDocsFromServer", "getCountFromServer"}
_WRITE_OPS = {"set", "update", "add", "create", "delete", "insert", "upsert",
              "setDoc", "updateDoc", "addDoc", "deleteDoc"}

_HTTP_CALL_RE = re.compile(
    r"(?<![\w$.])(fetch|axios(?:\.(?:get|post|put|patch|delete|request|head))?|"
    r"\$\.(?:ajax|get|post|getJSON)|requests\.(?:get|post|put|patch|delete|head)|"
    r"httpx\.(?:get|post|put|patch|delete)|navigator\.sendBeacon|Uri\.parse|"
    r"http\.(?:get|post|put|patch|delete)|ky(?:\.(?:get|post|put|patch|delete))?|"
    r"got(?:\.(?:get|post|put|patch|delete))?|\$http\.(?:get|post|put|patch|delete))\s*\(")
_XHR_RE = re.compile(r"""\.open\(\s*(['"])(GET|POST|PUT|PATCH|DELETE|HEAD)\1\s*,""", re.I)
_ROUTE_RE = re.compile(
    r"""(?<![\w$])([A-Za-z_$][\w$]*)\s*\.\s*(get|post|put|patch|delete|del|all|use|options|head|route|"""
    r"""GET|POST|PUT|PATCH|DELETE|HandleFunc|Handle|Any)\s*\(\s*(['"`])(/[^'"`\n]*)\3""")
_SPRING_RE = re.compile(r"""@(Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:(?:value|path)\s*=\s*)?\{?\s*"(/?[^"]*)\"""")
_CSHARP_RE = re.compile(r"""\[\s*(?:Http(Get|Post|Put|Patch|Delete)|Route)\(\s*"([^"]*)"\s*\)""")
_CLIENT_RECEIVERS = {"axios", "http", "https", "$http", "$", "jquery", "api", "client", "request",
                     "superagent", "ky", "got", "requests", "httpx", "session", "dio", "fetch",
                     "httpclient", "instance", "apiclient", "supertest", "agent", "chai", "test",
                     "localstorage", "sessionstorage", "map", "params", "searchparams", "headers",
                     "cache", "store", "url", "formdata", "cookies", "res", "req"}
_CLOUDFN_RE = re.compile(
    r"""(?<![\w$.])(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*=\s*(?:[\w$]+\.)*"""
    r"""(onRequest|onCall|onSchedule|onDocumentCreated|onDocumentUpdated|onDocumentWritten|"""
    r"""onDocumentDeleted|onObjectFinalized|onMessagePublished|onTaskDispatched|onValueWritten|"""
    r"""beforeUserCreated|beforeUserSignedIn|schedule|document|onCreate|onUpdate|onWrite|onDelete|"""
    r"""https|pubsub|storage|auth|firestore|database)\b""")
_EMIT_RES = [
    re.compile(r"""\bnew\s+(?:Custom)?Event\(\s*(['"`])([^'"`\n]+)\1"""),
    re.compile(r"""(?<![\w$])\$?(?:emit|trigger)\(\s*(['"`])([^'"`\n]+)\1"""),
    re.compile(r"""\bipc\w*\.(?:send|invoke|sendSync|sendTo)\(\s*(['"`])([^'"`\n]+)\1"""),
    re.compile(r"""\.(?:send)\(\s*(['"`])([^'"`\n]+)\1""" r"""(?=[^\n]*ipc)"""),
]
_LISTEN_RES = [
    re.compile(r"""\baddEventListener\(\s*(['"`])([^'"`\n]+)\1"""),
    re.compile(r"""\.\s*(?:on|once|\$on|addListener|handle|handleOnce)\(\s*(['"`])([^'"`\n]+)\1"""),
]
_STORAGE_RES = [
    re.compile(r"""(?<![\w$])(localStorage|sessionStorage|AsyncStorage)\s*\.\s*(getItem|setItem|removeItem)\(\s*(['"`])([^'"`\n]+)\3"""),
    re.compile(r"""\.\s*(get|set)(String|Int|Bool|Double|StringList)\(\s*(['"])([^'"\n]+)\3"""),
]
_ENV_RES = [
    re.compile(r"""\bprocess\.env\.([A-Za-z_][\w]*)"""),
    re.compile(r"""\bprocess\.env\[\s*['"]([^'"]+)['"]\s*\]"""),
    re.compile(r"""\bimport\.meta\.env\.([A-Za-z_][\w]*)"""),
    re.compile(r"""\bos\.(?:getenv|Getenv|LookupEnv)\(\s*['"]([^'"]+)"""),
    re.compile(r"""\bos\.environ(?:\.get\(\s*|\[\s*)['"]([^'"]+)"""),
    re.compile(r"""\bString\.fromEnvironment\(\s*['"]([^'"]+)"""),
    re.compile(r"""\bdefine(?:Secret|String|Int|Boolean|Float|List)\(\s*['"]([^'"]+)"""),
    re.compile(r"""\bENV\[\s*['"]([^'"]+)"""),
    re.compile(r"""\bEnvironment\.GetEnvironmentVariable\(\s*"([^"]+)"""),
    re.compile(r"""\bSystem\.getenv\(\s*"([^"]+)"""),
]
_VER_RE = re.compile(r"[?&](?:v|ver|version|cb|rev|build)=([^&#\s\"']*)", re.I)
_DYN_LOAD_RE = re.compile(
    r"""(?:(?<![\w$])\w*[Ll]oad\w*\(\s*|\.src\s*=\s*|\.href\s*=\s*|setAttribute\(\s*['"](?:src|href)['"]\s*,\s*)"""
    r"""(['"`])([^'"`\s]+?\.(?:m?js|css))((?:\?[^'"`]*)?)\1""")
_GLOBAL_RE = re.compile(r"""(?<![\w$.])(?:window|globalThis|self)\.([A-Za-z_$][\w$]*)\s*=(?!=)""")
_PATHSTR_RE = re.compile(r"""(['"])((?:https?://|/)[^\s'"`<>\\]*)\1""")
_TPLSTR_RE = re.compile(r"""`([^`\n]*)`""")
_TPL_EXPR = re.compile(r"\$\{[^}]*\}")
_LIMIT_RE = re.compile(r"(?<![\w$])\.?\s*limit\s*\(")
_DOM_EVENTS = set("""click dblclick change input submit load unload beforeunload DOMContentLoaded
keydown keyup keypress focus blur focusin focusout resize scroll wheel contextmenu
mousedown mouseup mousemove mouseover mouseout mouseenter mouseleave
touchstart touchend touchmove touchcancel pointerdown pointerup pointermove pointerover
pointerout pointerenter pointerleave pointercancel popstate hashchange storage message
error online offline visibilitychange transitionend animationend animationstart dragstart
drag dragend dragover dragenter dragleave drop paste copy cut select reset invalid abort
progress readystatechange open close connection data end finish request response
selectionchange orientationchange pageshow pagehide play pause ended timeupdate
loadedmetadata canplay seeked volumechange fullscreenchange beforeinstallprompt
appinstalled devicemotion deviceorientation toggle search show cancel
value child_added child_changed child_removed""".split())


def _norm_tpl(s):
    return _TPL_EXPR.sub("{}", s)


def _first_arg(nc, p, limit=500):
    """Raw text of the first call argument starting at p (just after '(')."""
    n = min(len(nc), p + limit)
    depth = 0
    i = p
    while i < n:
        ch = nc[i]
        if ch in "'\"`":
            e = _end_quote(nc, i + 1, ch) if ch != "`" else nc.find("`", i + 1)
            if e < 0:
                return nc[p:n]
            i = e + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return nc[p:i]
            depth -= 1
        elif ch == "," and depth == 0:
            return nc[p:i]
        i += 1
    return nc[p:n]


def _split_args(nc, p, limit=800):
    """All top-level call arguments starting at p (just after '(')."""
    n = min(len(nc), p + limit)
    args = []
    depth = 0
    i = p
    start = p
    while i < n:
        ch = nc[i]
        if ch in "'\"`":
            e = _end_quote(nc, i + 1, ch) if ch != "`" else nc.find("`", i + 1)
            if e < 0:
                break
            i = e + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                args.append(nc[start:i])
                return args
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(nc[start:i])
            start = i + 1
        i += 1
    return args


_LIT_RE = re.compile(r"""'((?:\\.|[^'\\\n])*)'|"((?:\\.|[^"\\\n])*)"|`((?:\\.|[^`\\])*)`""")


def _literal(expr):
    """If expr is a single string literal, return its normalised value."""
    e = expr.strip()
    m = _LIT_RE.fullmatch(e)
    if not m:
        return None
    if m.group(3) is not None:
        return _norm_tpl(m.group(3))
    return m.group(1) if m.group(1) is not None else m.group(2)


def _norm_url_expr(raw):
    """Turn  `${BASE}/orders/${id}`  or  BASE + '/orders'  into  {}/orders/{} ."""
    out = []
    last = 0
    for m in _LIT_RE.finditer(raw):
        between = raw[last:m.start()].strip().strip("+").strip()
        if between:
            out.append("{}")
        if m.group(3) is not None:
            out.append(_norm_tpl(m.group(3)))
        else:
            out.append(m.group(1) if m.group(1) is not None else m.group(2))
        last = m.end()
    tail = raw[last:].strip().strip("+").strip()
    if tail:
        out.append("{}")
    s = "".join(out)
    s = re.sub(r"(\{\})+", "{}", s)
    return s or "{}"


def _stmt_bounds_back(nc, s, limit=300):
    a = max(0, s - limit)
    seg = nc[a:s]
    k = max(seg.rfind(";"), seg.rfind("{"), seg.rfind("}"))
    return seg[k + 1:] if k >= 0 else seg


def _stmt_fwd(nc, e, limit=700):
    seg = nc[e:e + limit]
    k = seg.find(";")
    if k >= 0:
        seg = seg[:k]
    k = seg.find("\n\n")
    if k >= 0:
        seg = seg[:k]
    return seg


def _coll_ops(nc, s, e, api):
    fwd = _stmt_fwd(nc, e)
    parent = False
    cut = re.search(r"\.\s*(?:collection|collectionGroup|collection_group)\s*\(", fwd)
    if cut:
        fwd = fwd[:cut.start()]
        parent = True
    ops = []
    for m in _FWD_OPS.finditer(fwd):
        if m.group(1) not in ops:
            ops.append(m.group(1))
    back = _stmt_bounds_back(nc, s)
    for rx in (_BACK_OPS, _BACK_CHAIN):
        for m in rx.finditer(back):
            if m.group(1) not in ops:
                ops.append(m.group(1))
    flags = []
    if re.search(r"\.\s*where\s*\(|(?<![\w$])where\s*\(", fwd):
        flags.append("where")
    has_limit = bool(_LIMIT_RE.search(fwd) or _LIMIT_RE.search(back))
    if has_limit:
        flags.append("limit")
    is_doc = api in ("doc", "document") or bool(re.search(r"\.\s*doc\s*\(", fwd))
    if is_doc:
        flags.append("doc")
    if parent and not ops:
        ops = ["path"]
    reads = [o for o in ops if o in _READ_OPS]
    if reads and not is_doc and not has_limit and api != "doc":
        flags.append("no-limit-seen")
    return ops, flags


def _coll_names(path):
    segs = [x for x in path.strip("/").split("/")]
    return [x for x in segs[0::2] if x and x != "{}"]


def common_facts(nc, ns):
    """Facts from comment-free text `nc` (strings kept) and code-only text `ns`."""
    out = []  # (pos, kind, value, extra)

    def add_coll(pos, endpos, api, path):
        path = _norm_tpl(path)
        ops, flags = _coll_ops(nc, pos, endpos, api)
        for name in _coll_names(path) or ([path] if path else []):
            ex = {"api": api}
            if ops:
                ex["ops"] = ops
            if flags:
                ex["flags"] = flags
            if path != name:
                ex["path"] = path
            out.append((pos, "collection", name, ex))

    for m in _COLL_RE.finditer(nc):

        if ns[m.start()] != nc[m.start()]:

            continue  # inside a string literal, not code
        api = "collectionGroup" if m.group(1) != "collection" else "collection"
        add_coll(m.start(), m.end(), api, m.group(3))
    for m in _COLL_MOD_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        args = _split_args(nc, m.end())
        lits = []
        for a in args:
            v = _literal(a)
            lits.append(v if v is not None else "{}")
        if not lits or lits[0] == "{}":
            continue
        add_coll(m.start(), m.end(), m.group(1), "/".join(lits))
    for m in _DOCPATH_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        add_coll(m.start(), m.end(), "doc", m.group(3))
    for m in _FROM_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        ops, flags = _coll_ops(nc, m.start(), m.end(), "from")
        ex = {"api": "from"}
        if ops:
            ex["ops"] = ops
        out.append((m.start(), "collection", m.group(2), ex))

    # HTTP clients
    for m in _HTTP_CALL_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        via = m.group(1)
        raw = _first_arg(nc, m.end())
        url = _norm_url_expr(raw)
        method = None
        if "." in via and via.split(".")[-1].lower() in ("get", "post", "put", "patch", "delete", "head"):
            method = via.split(".")[-1].upper()
        else:
            tail = nc[m.end():m.end() + 600]
            mm = re.search(r"""method\s*:\s*['"](\w+)['"]""", tail)
            if mm:
                method = mm.group(1).upper()
        ex = {"via": via}
        if method:
            ex["method"] = method
        if url == "{}":
            ex["expr"] = raw.strip()[:120]
        out.append((m.start(), "http", url, ex))
    for m in _XHR_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        args = _split_args(nc, nc.find("(", m.start()) + 1)
        if len(args) >= 2:
            out.append((m.start(), "http", _norm_url_expr(args[1]), {"via": "xhr", "method": m.group(2).upper()}))

    # server routes
    for m in _ROUTE_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        recv = m.group(1)
        meth = m.group(2).lower()
        path = _norm_tpl(m.group(4))
        if recv.lower() in _CLIENT_RECEIVERS and meth not in ("handlefunc", "handle"):
            if meth in ("get", "post", "put", "patch", "delete"):
                out.append((m.start(), "http", path, {"via": recv + "." + meth, "method": meth.upper()}))
            continue
        if meth == "del":
            meth = "delete"
        if meth in ("handlefunc", "handle", "route"):
            meth = "any"
        out.append((m.start(), "route", path, {"method": meth.upper(), "app": recv}))
    for m in _SPRING_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        meth = m.group(1).upper()
        out.append((m.start(), "route", "/" + m.group(2).lstrip("/"), {"method": "ANY" if meth == "REQUEST" else meth}))
    for m in _CSHARP_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        meth = (m.group(1) or "any").upper()
        out.append((m.start(), "route", "/" + m.group(2).lstrip("/"), {"method": meth}))
    for m in _CLOUDFN_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        out.append((m.start(), "cloudfn", m.group(1), {"trigger": m.group(2)}))

    # events
    for rx in _EMIT_RES:
        for m in rx.finditer(nc):
            if ns[m.start()] != nc[m.start()]:
                continue  # inside a string literal, not code
            out.append((m.start(), "emit", m.group(2), None))
    for rx in _LISTEN_RES:
        for m in rx.finditer(nc):
            if ns[m.start()] != nc[m.start()]:
                continue  # inside a string literal, not code
            out.append((m.start(), "listen", m.group(2), None))

    # storage keys
    for m in _STORAGE_RES[0].finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        op = {"getItem": "get", "setItem": "set", "removeItem": "remove"}[m.group(2)]
        out.append((m.start(), "storage", _norm_tpl(m.group(4)), {"store": m.group(1), "op": op}))
    for m in _STORAGE_RES[1].finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue  # inside a string literal, not code
        out.append((m.start(), "storage", m.group(4), {"store": "prefs", "op": m.group(1)}))

    # env
    for rx in _ENV_RES:
        for m in rx.finditer(nc):
            if ns[m.start()] != nc[m.start()]:
                continue  # inside a string literal, not code
            out.append((m.start(), "env", m.group(1), None))

    # scripts/styles loaded at runtime: loadScript('../js/x.js?v=' + Date.now()), el.src = '...'
    for m in _DYN_LOAD_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue
        q = m.group(3)
        ex = {"tag": "dynamic"}
        vm = _VER_RE.search(q)
        if vm:
            ex["v"] = vm.group(1) or "(computed)"
        out.append((m.start(), "load", m.group(2) + q, ex))

    # globals (code text only)
    for m in _GLOBAL_RE.finditer(ns):
        out.append((m.start(), "global", m.group(1), None))

    # path-like string literals (used to link pages/clients to routes)
    seen = set()
    for m in _PATHSTR_RE.finditer(nc):
        v = m.group(2)
        if not re.search(r"[A-Za-z]", v) or v in seen:
            continue
        seen.add(v)
        out.append((m.start(), "pathstr", v, None))
    for m in _TPLSTR_RE.finditer(nc):
        v = _norm_tpl(m.group(1))
        if not (v.startswith("/") or v.startswith("{}/") or v.startswith("http")):
            continue
        if " " in v or not re.search(r"[A-Za-z]", v) or v in seen:
            continue
        seen.add(v)
        out.append((m.start(), "pathstr", v, None))
    return out


# --------------------------------------------------------------------------
# JavaScript / TypeScript
# --------------------------------------------------------------------------

_IDENT = r"[A-Za-z_$][\w$]*"
_ARROWISH = r"(?:async\s+)?(?:function\b|\([^()]*\)\s*(?::[^=\n]+)?=>|" + _IDENT + r"\s*=>)"
JS_DEF_RES = [
    (re.compile(r"(?<![\w$.])(?:async\s+)?function\s*\*?\s*(" + _IDENT + r")\s*(?:<[^>\n]*>)?\s*\("), "function"),
    (re.compile(r"(?<![\w$.])class\s+(" + _IDENT + r")"), "class"),
    (re.compile(r"(?<![\w$.])(?:const|let|var)\s+(" + _IDENT + r")\s*(?::[^=\n]+)?=\s*" + _ARROWISH), "function"),
    (re.compile(r"(?<![\w$.])((?:" + _IDENT + r"\.)+" + _IDENT + r")\s*=(?!=)\s*" + _ARROWISH), "function"),
    (re.compile(r"(?:^|(?<=[,{\s]))(" + _IDENT + r")\s*:\s*" + _ARROWISH, re.M), "method"),
    (re.compile(r"^[ \t]*(?:(?:public|private|protected|readonly|static|async|override|abstract|get|set)\s+)*"
                r"\*?[ \t]*([A-Za-z_$#][\w$]*)[ \t]*(?:<[^>\n]*>)?[ \t]*\([^()\n]*(?:\([^()\n]*\)[^()\n]*)*\)\s*"
                r"(?::\s*[^{;=\n]+)?\{", re.M), "method"),
    (re.compile(r"(?<![\w$.])(?:module\.)?exports\.(" + _IDENT + r")\s*=(?!=)"), "export"),
]
JS_KEYWORDS = set("""if for while switch catch function return typeof new delete void throw
case do else yield await in of instanceof with super import export class extends const let
var try finally default break continue debugger this static get set async true false null
undefined""".split())
JS_CALL_SKIP = JS_KEYWORDS | {"require"}
_JS_CALL_RE = re.compile(r"(?<![\w$#])(" + _IDENT + r")\s*\(")
_RECV_RE = re.compile(r"(" + _IDENT + r")\s*\??\.\s*$")
_FUNC_KW_TAIL = re.compile(r"(?<![\w$])function\s*\*?\s*$")
_JS_IMPORT_RES = [
    re.compile(r"""(?<![\w$.])import\s+(?:[\w*{}\s,$]+?\s+from\s+)?(['"])([^'"\n]+)\1"""),
    re.compile(r"""(?<![\w$.])export\s+[\w*{}\s,$]+?\s+from\s+(['"])([^'"\n]+)\1"""),
    re.compile(r"""(?<![\w$.])require\(\s*(['"])([^'"\n]+)\1\s*\)"""),
    re.compile(r"""(?<![\w$.])import\(\s*(['"])([^'"\n]+)\1"""),
    re.compile(r"""(?<![\w$.])importScripts\(\s*(['"])([^'"\n]+)\1"""),
]
_JS_REF_RES = [
    (re.compile(r"""addEventListener\(\s*(['"`])[^'"`\n]*\1\s*,\s*(""" + _IDENT + r"""(?:\.""" + _IDENT + r""")*)\s*[,)]"""), 2),
    (re.compile(r"""(?<![\w$.])(?:setTimeout|setInterval|requestAnimationFrame|queueMicrotask|requestIdleCallback)\(\s*(""" + _IDENT + r"""(?:\.""" + _IDENT + r""")*)\s*[,)]"""), 1),
    (re.compile(r"""\.\s*(?:then|catch|finally|forEach|map|filter|reduce|some|every|find|findIndex|sort|flatMap)\(\s*(""" + _IDENT + r"""(?:\.""" + _IDENT + r""")*)\s*\)"""), 1),
    (re.compile(r"""\.\s*on[a-z]+\s*=(?!=)\s*(""" + _IDENT + r""")\s*(?:;|$)""", re.M), 1),
]
_ROUTE_HANDLERS_RE = re.compile(
    r"""\.\s*(?:get|post|put|patch|delete|all|use)\(\s*(['"`])[^'"`\n]*\1\s*,\s*((?:""" + _IDENT +
    r"""(?:\.""" + _IDENT + r""")*\s*,\s*)*""" + _IDENT + r"""(?:\.""" + _IDENT + r""")*)\s*[,)]""")
_ROUTE_CB_RE = re.compile(
    r"""(?<![\w$])([A-Za-z_$][\w$]*)\s*\.\s*(get|post|put|patch|delete|all|use)\(\s*(['"`])(/[^'"`\n]*)\3\s*,""")
_LISTEN_CB_RE = re.compile(
    r"""(?<![\w$])(?:([A-Za-z_$][\w$]*)\s*\.\s*)?(addEventListener|on|once|handle|handleOnce)\(\s*(['"`])([^'"`\n]+)\3\s*,""")
_CB_START_RE = re.compile(r"(?:async\s+)?(?:function\b[^(]*\(|\([^()]*\)\s*=>|" + _IDENT + r"\s*=>)")


def _callback_defs(nc, ns, braces):
    """Anonymous callbacks passed to routes / listeners, named after what they handle."""
    out = []
    for rx, is_route in ((_ROUTE_CB_RE, True), (_LISTEN_CB_RE, False)):
        for m in rx.finditer(nc):
            recv = (m.group(1) or "")
            if is_route and recv.lower() in _CLIENT_RECEIVERS:
                continue
            op = nc.find("(", m.start(2))
            close = _skip_balanced(ns, op, "(", ")")
            cb = _CB_START_RE.search(ns, m.end(), close)
            if not cb:
                continue
            if ns[m.end():cb.start()].strip(" \t\r\n,").count("(") > 2:
                continue
            if cb.group().rstrip().endswith("=>"):
                end = _scope_end(ns, cb.end(), braces, after_arrow=True)
            else:
                end = _scope_end(ns, cb.end() - 1, braces)
            if is_route:
                name = "%s %s" % (m.group(2).upper(), _norm_tpl(m.group(4)))
            else:
                ev = m.group(4)
                name = "on %s%s" % (ev, (" (" + recv + ")") if recv and recv not in ("window", "document") else "")
            out.append([cb.start(), end, cb.start(), name, name, "handler"])
    return out


_INLINE_HANDLER_RE = re.compile(r"""\bon[a-z]{3,}\s*=\s*\\?(["'])(.*?)\\?\1""")
_LITERAL_WORDS = {"true", "false", "null", "undefined", "this", "NaN", "Infinity"}


def _js_qual(full):
    parts = full.split(".")
    if "prototype" in parts:
        parts = [p for p in parts if p != "prototype"]
    while len(parts) > 1 and parts[0] in ("window", "globalThis", "self", "this"):
        parts = parts[1:]
    if len(parts) > 2 and parts[0] == "module" and parts[1] == "exports":
        parts = parts[1:]
    return ".".join(parts)


def _finalize(L, raw_defs, call_items, fact_items, imports, flat=False):
    """raw_defs: [start, end, npos, name, qual, kind]. call_items: (pos, name, recv).
    fact_items: (pos, kind, value, extra). imports: (pos, spec)."""
    raw_defs.sort(key=lambda d: (d[0], -d[1]))
    if flat:
        for k, d in enumerate(raw_defs):
            d[1] = raw_defs[k + 1][0] - 1 if k + 1 < len(raw_defs) else L.total
    parents = [-1] * len(raw_defs)
    stack = []
    for k, d in enumerate(raw_defs):
        while stack and raw_defs[stack[-1]][1] < d[0]:
            stack.pop()
        parents[k] = stack[-1] if stack else -1
        stack.append(k)
    defs = []
    for k, d in enumerate(raw_defs):
        name, qual, kind = d[3], d[4], d[5]
        par = parents[k]
        if par >= 0 and raw_defs[par][5] == "class" and "." not in qual:
            qual = defs[par][1] + "." + name
        defs.append([name, qual, kind, L.line(d[2]), L.line(max(d[0], min(d[1], L.total))), par])
    ranges = [(d[0], d[1]) for d in raw_defs]
    cenc = _enclosing(ranges, [c[0] for c in call_items])
    calls = [[c[1], L.line(c[0]), c[2], cenc[k]] for k, c in enumerate(call_items)]
    fenc = _enclosing(ranges, [f[0] for f in fact_items])
    facts = []
    for k, f in enumerate(fact_items):
        facts.append([f[1], f[2], L.line(f[0]), f[3], fenc[k]])
    imps = [[spec, L.line(pos)] for pos, spec in imports]
    return {"defs": defs, "calls": calls, "facts": facts, "imports": imps}


def extract_js(src, handler_ranges=None):
    comments, strings = lex(src, js=True, backtick="template")
    nc = _blank(src, list(comments))
    ns = _blank(src, comments + strings)
    L = Lines(src)
    braces = _brace_map(ns)
    raw_defs = []
    seen = set()
    for rx, kind in JS_DEF_RES:
        for m in rx.finditer(ns):
            full = m.group(1)
            name = full.rsplit(".", 1)[-1]
            if name in JS_KEYWORDS or not name:
                continue
            npos = m.start(1) + (len(full) - len(name))
            if npos in seen:
                continue
            seen.add(npos)
            start = m.start(1) if kind == "method" else m.start()
            tail = ns[m.end() - 2:m.end()]
            if tail == "=>":
                end = _scope_end(ns, m.end(), braces, after_arrow=True)
            else:
                p = m.end() - 1 if ns[m.end() - 1] in "({" else m.end()
                end = _scope_end(ns, p, braces)
            qual = _js_qual(full)
            raw_defs.append([start, end, npos, name, qual, kind])
    raw_defs.extend(_callback_defs(nc, ns, braces))
    call_items = []
    for m in _JS_CALL_RE.finditer(ns):
        name = m.group(1)
        s = m.start(1)
        if name in JS_CALL_SKIP or s in seen:
            continue
        pre = ns[max(0, s - 60):s]
        if _FUNC_KW_TAIL.search(pre):
            continue
        recv = ""
        mm = _RECV_RE.search(pre)
        if mm:
            recv = mm.group(1)
        elif re.search(r"[\)\]]\s*\??\.\s*$", pre):
            recv = "(expr)"
        if handler_ranges and any(a <= s < b for a, b in handler_ranges):
            recv = "@html"
        call_items.append((s, name, recv))
    for rx, g in _JS_REF_RES:
        for m in rx.finditer(ns):
            name = m.group(g).rsplit(".", 1)[-1]
            if name in _LITERAL_WORDS or name in JS_KEYWORDS:
                continue
            call_items.append((m.start(g), name, "@ref"))
    for m in _ROUTE_HANDLERS_RE.finditer(ns):
        base = m.start(2)
        for part in re.finditer(_IDENT + r"(?:\." + _IDENT + r")*", m.group(2)):
            name = part.group().rsplit(".", 1)[-1]
            if name not in _LITERAL_WORDS and name not in JS_KEYWORDS:
                call_items.append((base + part.start(), name, "@ref"))
    # onclick="fn()" inside JS strings / template literals (HTML built in JS)
    for m in _INLINE_HANDLER_RE.finditer(nc):
        p = m.start()
        if ns[p] != " " or nc[p] == " ":
            continue
        code = m.group(2)
        for c in _JS_CALL_RE.finditer(code):
            if c.group(1) in JS_CALL_SKIP:
                continue
            pre = code[:c.start(1)]
            if re.search(r"\.\s*$", pre) and not re.search(r"(?:window)\.\s*$", pre):
                continue
            call_items.append((m.start(2) + c.start(1), c.group(1), "@html"))
    imports = []
    for rx in _JS_IMPORT_RES:
        for m in rx.finditer(nc):
            imports.append((m.start(), m.group(2)))
    fact_items = common_facts(nc, ns)
    return _finalize(L, raw_defs, call_items, fact_items, imports)


# --------------------------------------------------------------------------
# HTML / Vue / Svelte
# --------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
_LINK_RE = re.compile(r"<link\b([^>]*)>", re.I)
_HANDLER_ATTR_RE = re.compile(r"""\s(on[a-z]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_JS_TYPES = {"", "text/javascript", "module", "application/javascript", "text/babel",
             "text/jsx", "application/x-javascript", "ts", "typescript"}


def _attr(attrs, name):
    m = re.search(r"""(?<![\w-])%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % re.escape(name), attrs, re.I)
    if not m:
        return None
    return next(g for g in m.groups() if g is not None)


def extract_html(src):
    src2 = _blank(src, [(m.start(), m.end()) for m in _HTML_COMMENT_RE.finditer(src)])
    keep = []
    loads = []
    for m in _SCRIPT_RE.finditer(src2):
        attrs = m.group(1)
        s = _attr(attrs, "src")
        typ = (_attr(attrs, "type") or "").lower().strip()
        lang = (_attr(attrs, "lang") or "").lower().strip()
        if s:
            loads.append((m.start(), "script", s))
        if typ not in _JS_TYPES and lang not in _JS_TYPES:
            continue
        keep.append((m.start(2), m.end(2)))
    script_ranges = list(keep)
    handlers = []
    for m in _HANDLER_ATTR_RE.finditer(src2):
        if any(a <= m.start() < b for a, b in script_ranges):
            continue
        g = 2 if m.group(2) is not None else 3
        keep.append((m.start(g), m.end(g)))
        handlers.append((m.start(g), m.end(g), m.group(1).lower(), m.group(g)))
    for m in _LINK_RE.finditer(src2):
        rel = (_attr(m.group(1), "rel") or "").lower()
        href = _attr(m.group(1), "href")
        if href and any(r in rel for r in ("stylesheet", "preload", "modulepreload", "manifest")):
            loads.append((m.start(), "link", href))
    js_text = _keep_only(src2, keep)
    r = extract_js(js_text, handler_ranges=[(a, b) for a, b, _, _ in handlers])
    L = Lines(src)
    for pos, tag, url in loads:
        ex = {"tag": tag}
        vm = _VER_RE.search(url)
        if vm:
            ex["v"] = vm.group(1)
        r["facts"].append(["load", url, L.line(pos), ex, -1])
    for a, b, ev, code in handlers:
        r["facts"].append(["handler", ev, L.line(a), {"code": " ".join(code.split())[:160]}, -1])
    return r


# --------------------------------------------------------------------------
# Python (real parser via ast)
# --------------------------------------------------------------------------

_PY_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "route", "api_route", "websocket", "head", "options"}
_PY_HTTP_RECV = {"requests", "httpx", "session", "client", "aiohttp", "s"}


def _py_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{}")
        return re.sub(r"(\{\})+", "{}", "".join(parts))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _py_str(node.left), _py_str(node.right)
        if a is None and b is None:
            return None
        return re.sub(r"(\{\})+", "{}", (a if a is not None else "{}") + (b if b is not None else "{}"))
    return None


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def extract_py(src):
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return extract_basic(src, "pyfb")
    L = Lines(src)
    defs, calls, facts, imports = [], [], [], []
    stack = []

    def pos_of(node):
        return L.starts[node.lineno - 1] + node.col_offset

    def enc():
        return stack[-1] if stack else -1

    def add_fact(node, kind, value, extra=None, enclosing=None):
        facts.append([kind, value, node.lineno, extra, enc() if enclosing is None else enclosing])

    def handle(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parent = enc()
            q = node.name if parent < 0 else defs[parent][1] + "." + node.name
            if isinstance(node, ast.ClassDef):
                kind = "class"
            elif parent >= 0 and defs[parent][2] == "class":
                kind = "method"
            else:
                kind = "function"
            defs.append([node.name, q, kind, node.lineno, getattr(node, "end_lineno", None) or node.lineno, parent])
            idx = len(defs) - 1
            decos = set()
            for d in node.decorator_list:
                decos.add(id(d))
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr.lower() in _PY_ROUTE_METHODS and d.args):
                    path = _py_str(d.args[0])
                    if path is not None and path.startswith("/"):
                        meth = d.func.attr.lower()
                        if meth in ("route", "api_route"):
                            ms = []
                            for kw in d.keywords:
                                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                                    ms = [_py_str(e) or "" for e in kw.value.elts]
                            meth = ",".join(m.upper() for m in ms if m) or "ANY"
                        add_fact(d, "route", path, {"method": meth.upper()}, enclosing=idx)
                handle(d)
            stack.append(idx)
            for child in ast.iter_child_nodes(node):
                if id(child) not in decos:
                    handle(child)
            stack.pop()
            return
        if isinstance(node, ast.Call):
            f = node.func
            name = recv = None
            if isinstance(f, ast.Name):
                name, recv = f.id, ""
            elif isinstance(f, ast.Attribute):
                name = f.attr
                recv = f.value.id if isinstance(f.value, ast.Name) else "(expr)"
            if name:
                calls.append([name, node.lineno, recv, enc()])
            dotted = _dotted(f) or ""
            arg0 = _py_str(node.args[0]) if node.args else None
            if name in ("collection", "collection_group", "document") and arg0:
                api = "collectionGroup" if name == "collection_group" else ("doc" if name == "document" else "collection")
                if api != "doc" or "/" in arg0:
                    s = pos_of(node)
                    e = L.starts[node.end_lineno - 1] + node.end_col_offset if getattr(node, "end_lineno", None) else s
                    ops, flags = _coll_ops(src, s, e, api)
                    for cname in _coll_names(arg0) or [arg0]:
                        ex = {"api": api}
                        if ops:
                            ex["ops"] = ops
                        if flags:
                            ex["flags"] = flags
                        if arg0 != cname:
                            ex["path"] = arg0
                        add_fact(node, "collection", cname, ex)
            if dotted in ("os.getenv", "os.environ.get", "environ.get", "getenv") and arg0:
                add_fact(node, "env", arg0)
            if recv in _PY_HTTP_RECV and name in ("get", "post", "put", "patch", "delete", "head", "request") and node.args:
                url = arg0 if name != "request" else (_py_str(node.args[1]) if len(node.args) > 1 else None)
                add_fact(node, "http", url or "{}", {"via": dotted or name, "method": name.upper()})
            if name in ("path", "re_path") and recv == "" and len(node.args) >= 2 and arg0 is not None:
                add_fact(node, "route", "/" + arg0.lstrip("^/").rstrip("$"), {"method": "ANY"})
        elif isinstance(node, ast.Subscript):
            if _dotted(node.value) in ("os.environ", "environ"):
                sl = node.slice
                if hasattr(ast, "Index") and isinstance(sl, getattr(ast, "Index")):
                    sl = sl.value
                v = _py_str(sl)
                if v:
                    add_fact(node, "env", v)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imports.append([a.name, node.lineno])
            else:
                mod = "." * (node.level or 0) + (node.module or "")
                imports.append([mod, node.lineno])
        elif isinstance(node, ast.JoinedStr):
            v = _py_str(node)
            if v and (v.startswith("/") or v.startswith("{}/") or v.startswith("http")) and " " not in v:
                add_fact(node, "pathstr", v)
            return
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if (v.startswith("/") or v.startswith("http://") or v.startswith("https://")) and " " not in v \
                    and "\n" not in v and re.search(r"[A-Za-z]", v) and len(v) < 300:
                add_fact(node, "pathstr", v)
        for child in ast.iter_child_nodes(node):
            handle(child)

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 20000))
    try:
        for child in ast.iter_child_nodes(tree):
            handle(child)
    finally:
        sys.setrecursionlimit(old)
    # de-duplicate path strings per file
    seen = set()
    out_facts = []
    for f in facts:
        if f[0] == "pathstr":
            if f[1] in seen:
                continue
            seen.add(f[1])
        out_facts.append(f)
    return {"defs": defs, "calls": calls, "facts": out_facts, "imports": imports}


# --------------------------------------------------------------------------
# Other languages (basic tier: definitions, calls, imports, facts)
# --------------------------------------------------------------------------

_M = re.M
_CLASS_DEF = (re.compile(r"(?<![\w$])(?:class|interface|struct|enum|object|mixin|extension|record|trait|protocol)\s+([A-Za-z_]\w*)"), "class")
_CMETHOD = (re.compile(
    r"^[ \t]*(?:[\w<>\[\],.?*&:@~]+[ \t]+)+?\*?&?([A-Za-z_~]\w*)\s*\([^;{}()]*(?:\([^()]*\)[^;{}()]*)*\)\s*"
    r"(?:const\s*|async\s*\*?\s*|sync\s*\*\s*|throws\s+[\w.,\s]+?|override\s*|noexcept\s*|final\s*)*(?:\{|=>)", _M), "function")
BASIC_CFG = {
    "go": dict(slash=True, hashc=False, backtick="raw", braces=True, defs=[
        (re.compile(r"^[ \t]*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\(", _M), "function"),
        (re.compile(r"^[ \t]*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b", _M), "class")]),
    "rust": dict(slash=True, braces=True, defs=[
        (re.compile(r"(?<![\w$])fn\s+([A-Za-z_]\w*)"), "function"),
        (re.compile(r"(?<![\w$])(?:struct|enum|trait|union)\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"(?<![\w$])impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([A-Za-z_]\w*)"), "class")]),
    "kotlin": dict(slash=True, braces=True, defs=[
        (re.compile(r"(?<![\w$])fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\("), "function"), _CLASS_DEF]),
    "swift": dict(slash=True, braces=True, defs=[(re.compile(r"(?<![\w$])func\s+([A-Za-z_]\w*)"), "function"), _CLASS_DEF]),
    "scala": dict(slash=True, braces=True, defs=[(re.compile(r"(?<![\w$])def\s+([A-Za-z_]\w*)"), "function"), _CLASS_DEF]),
    "php": dict(slash=True, hashc=True, braces=True, defs=[
        (re.compile(r"(?<![\w$])function\s+&?\s*([A-Za-z_]\w*)\s*\("), "function"), _CLASS_DEF]),
    "ruby": dict(slash=False, hashc=True, braces=False, defs=[
        (re.compile(r"^[ \t]*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)", _M), "function"),
        (re.compile(r"^[ \t]*(?:class|module)\s+([A-Z][\w:]*)", _M), "class")]),
    "pyfb": dict(slash=False, hashc=True, braces=False, defs=[
        (re.compile(r"^[ \t]*(?:async\s+)?def\s+([A-Za-z_]\w*)", _M), "function"),
        (re.compile(r"^[ \t]*class\s+([A-Za-z_]\w*)", _M), "class")]),
}
for _lang in ("java", "csharp", "dart", "c", "cpp"):
    BASIC_CFG[_lang] = dict(slash=True, braces=True, defs=[_CMETHOD, _CLASS_DEF])
BASIC_IMPORTS = {
    "go": [re.compile(r"""^\s*import\s+(?:[\w.]+\s+)?"([^"]+)\"""", _M)],
    "java": [re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)", _M)],
    "kotlin": [re.compile(r"^\s*import\s+([\w.*]+)", _M)],
    "scala": [re.compile(r"^\s*import\s+([\w.*{}, ]+)", _M)],
    "csharp": [re.compile(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;", _M)],
    "dart": [re.compile(r"""^\s*(?:import|export|part)\s+'([^']+)'""", _M)],
    "rust": [re.compile(r"^\s*(?:pub\s+)?use\s+([\w:{}, *]+);", _M), re.compile(r"^\s*(?:pub\s+)?mod\s+(\w+)\s*;", _M)],
    "php": [re.compile(r"""\b(?:require|include)(?:_once)?\s*\(?\s*['"]([^'"]+)"""), re.compile(r"^\s*use\s+([\w\\]+)", _M)],
    "ruby": [re.compile(r"""^\s*require(?:_relative)?\s*\(?\s*['"]([^'"]+)""", _M)],
    "c": [re.compile(r"""^\s*#\s*include\s*[<"]([^>"]+)""", _M)],
    "cpp": [re.compile(r"""^\s*#\s*include\s*[<"]([^>"]+)""", _M)],
    "swift": [re.compile(r"^\s*import\s+(\w+)", _M)],
    "pyfb": [re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", _M)],
}
BASIC_SKIP = set("""if for while switch catch return new else do try sizeof synchronized using lock
foreach when match elif unless until loop fn func fun def function defer go select case typeof
assert print and or not in is lambda with yield await throw throws super this self""".split())
_BASIC_REF_RE = re.compile(
    r"""\.\s*(?:HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE|Any|get|post|put|patch|delete)\(\s*"[^"\n]*"\s*,\s*([A-Za-z_][\w.]*)\s*[,)]""")
_BASIC_CALL_RE = re.compile(r"(?<![\w$])([A-Za-z_]\w*)\s*\(")
_GO_RECV_RE = re.compile(r"func\s+\(\s*\w*\s*\*?\s*([A-Za-z_]\w*)")


def extract_basic(src, lang):
    cfg = BASIC_CFG.get(lang, BASIC_CFG["java"])
    comments, strings = lex(src, js=False, slash=cfg.get("slash", True), hashc=cfg.get("hashc", False),
                            backtick=cfg.get("backtick"))
    nc = _blank(src, list(comments))
    ns = _blank(src, comments + strings)
    L = Lines(src)
    braces = _brace_map(ns) if cfg.get("braces") else {}
    raw_defs = []
    seen = set()
    for rx, kind in cfg["defs"]:
        for m in rx.finditer(ns):
            name = m.group(1)
            if name in BASIC_SKIP:
                continue
            npos = m.start(1)
            if npos in seen:
                continue
            seen.add(npos)
            qual = name
            if lang == "go":
                gm = _GO_RECV_RE.match(m.group().strip())
                if gm:
                    qual = gm.group(1) + "." + name
            if cfg.get("braces"):
                p = m.end() - 1 if ns[m.end() - 1] in "({" else m.end()
                end = _scope_end(ns, p, braces)
            else:
                end = m.end()
            raw_defs.append([m.start(), end, npos, name, qual, kind])
    call_items = []
    for m in _BASIC_CALL_RE.finditer(ns):
        name = m.group(1)
        s = m.start(1)
        if name in BASIC_SKIP or s in seen:
            continue
        pre = ns[max(0, s - 60):s]
        mm = re.search(r"([A-Za-z_]\w*)\s*(?:\.|->|::)\s*$", pre)
        call_items.append((s, name, mm.group(1) if mm else ""))
    for m in _BASIC_REF_RE.finditer(nc):
        if ns[m.start()] != nc[m.start()]:
            continue
        call_items.append((m.start(1), m.group(1).rsplit(".", 1)[-1], "@ref"))
    imports = []
    for rx in BASIC_IMPORTS.get(lang, []):
        for m in rx.finditer(nc):
            spec = next((g for g in m.groups() if g), None)
            if spec:
                imports.append((m.start(), spec.strip()))
    if lang == "go":
        for blk in re.finditer(r"^\s*import\s*\(([^)]*)\)", nc, _M):
            for m in re.finditer(r"""\"([^"]+)\"""", blk.group(1)):
                imports.append((blk.start(1) + m.start(), m.group(1)))
    fact_items = common_facts(nc, ns)
    return _finalize(L, raw_defs, call_items, fact_items, imports, flat=not cfg.get("braces"))


# --------------------------------------------------------------------------
# file dispatch
# --------------------------------------------------------------------------

def lang_of(rel):
    return LANG_BY_EXT.get(os.path.splitext(rel)[1].lower())


def is_minified(rel, text):
    low = rel.lower()
    if low.endswith((".min.js", ".min.css", ".bundle.js", ".chunk.js")):
        return True
    if len(text) > 20000:
        lines = text.count("\n") + 1
        if len(text) / lines > 400:
            return True
    return False


def extract_file(rel, text):
    lang = lang_of(rel)
    if lang == "js":
        r = extract_js(text)
    elif lang == "html":
        r = extract_html(text)
    elif lang == "py":
        r = extract_py(text)
    else:
        r = extract_basic(text, lang)
    r["lang"] = lang
    r["lines"] = text.count("\n") + 1
    return r


# --------------------------------------------------------------------------
# discovery, storage, refresh
# --------------------------------------------------------------------------

def _run(args, cwd=None, timeout=60):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def git_toplevel(path):
    out = _run(["git", "-C", path, "rev-parse", "--show-toplevel"])
    return os.path.normpath(out.strip()) if out else None


def git_head(root):
    out = _run(["git", "-C", root, "rev-parse", "--short", "HEAD"])
    return out.strip() if out else None


def load_ignores(root):
    pats = []
    p = os.path.join(root, ".repofreeignore")
    if os.path.isfile(p):
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if s and not s.startswith("#"):
                    pats.append(s.strip("/"))
    return pats


def is_ignored(rel, pats):
    parts = rel.split("/")
    if any(part in DEFAULT_SKIP_DIRS for part in parts[:-1]):
        return True
    for p in pats:
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*") or any(fnmatch.fnmatch(x, p) for x in parts):
            return True
    return False


def discover(root):
    """All non-ignored files (relative, '/'-separated). Uses git when available."""
    pats = load_ignores(root)
    files = None
    if git_toplevel(root):
        out = _run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"], timeout=120)
        if out is not None:
            files = [f for f in out.split("\0") if f]
    if files is None:
        files = []
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in DEFAULT_SKIP_DIRS]
            for fn in fns:
                files.append(os.path.relpath(os.path.join(dp, fn), root).replace(os.sep, "/"))
    out = []
    for f in sorted(set(files)):
        if is_ignored(f, pats):
            continue
        if os.path.isfile(os.path.join(root, f)):
            out.append(f)
    return out


def default_outdir(root):
    env = os.environ.get("REPOFREE_DIR")
    return os.path.abspath(os.path.expanduser(env)) if env else os.path.join(root, ".repofree")


def _ensure_outdir(outdir, root):
    os.makedirs(outdir, exist_ok=True)
    gi = os.path.join(outdir, ".gitignore")
    if os.path.abspath(outdir).startswith(os.path.abspath(root) + os.sep) and not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write("# created by repofree - keeps this folder out of git\n*\n")


def _atomic_write(path, text):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def empty_map():
    return {"meta": {"tool": "repofree", "version": __version__, "schema": SCHEMA}, "files": {}, "paths": []}


def load_map(outdir):
    p = os.path.join(outdir, "map.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            a = json.load(fh)
        if a.get("meta", {}).get("schema") != SCHEMA:
            return None
        return a
    except (OSError, ValueError):
        return None


def refresh(repomap, root, force=False, progress=None):
    """Re-read only changed files. Returns (changed, removed)."""
    paths = discover(root)
    old = repomap.get("files", {})
    new = {}
    changed = 0
    meta_changed = False
    for rel in paths:
        if not lang_of(rel):
            continue
        full = os.path.join(root, rel)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if st.st_size > MAX_FILE_BYTES:
            continue
        rec = old.get(rel)
        if (rec and not force and rec.get("m") == st.st_mtime_ns and rec.get("s") == st.st_size
                and rec.get("xv") == EXTRACTOR_VERSION):
            new[rel] = rec
            continue
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        sha = hashlib.sha1(data).hexdigest()
        if rec and not force and rec.get("h") == sha and rec.get("xv") == EXTRACTOR_VERSION:
            rec["m"] = st.st_mtime_ns
            rec["s"] = st.st_size
            new[rel] = rec
            meta_changed = True
            continue
        text = data.decode("utf-8", "replace")
        if is_minified(rel, text):
            continue
        try:
            r = extract_file(rel, text)
        except Exception as e:  # never let one odd file break the map
            r = {"lang": lang_of(rel), "lines": text.count("\n") + 1, "defs": [], "calls": [],
                 "facts": [], "imports": [], "err": "%s: %s" % (type(e).__name__, e)}
        r.update({"m": st.st_mtime_ns, "s": st.st_size, "h": sha, "xv": EXTRACTOR_VERSION})
        new[rel] = r
        changed += 1
        if progress:
            progress(changed)
    removed = len(set(old) - set(new))
    paths_changed = repomap.get("paths") != paths
    repomap["files"] = new
    repomap["paths"] = paths
    head = git_head(root)
    if head and head != repomap.get("meta", {}).get("git_head"):
        meta_changed = True  # a new commit: re-save so the map and report show the current commit
    return changed, removed, (meta_changed or paths_changed)


def save_map(repomap, outdir, root):
    _ensure_outdir(outdir, root)
    repomap["meta"].update({"tool": "repofree", "version": __version__, "schema": SCHEMA,
                          "refreshed": _now_iso(), "git_head": git_head(root)})
    repomap["meta"].setdefault("built", repomap["meta"]["refreshed"])
    _atomic_write(os.path.join(outdir, "map.json"), json.dumps(repomap, separators=(",", ":"), ensure_ascii=False))


# --------------------------------------------------------------------------
# index over the repomap
# --------------------------------------------------------------------------

def _segments(p):
    p = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", p)
    p = p.split("?", 1)[0].split("#", 1)[0]
    segs = [s for s in p.split("/") if s]
    out = []
    for s in segs:
        if s.startswith(":") or s.startswith("<") or s.startswith("{") or s == "*" or "{}" in s or s.startswith("$"):
            out.append("{}")
        else:
            out.append(s)
    return out


def _route_matches(route_segs, client_segs):
    """0 = no match, otherwise a score: literal segments that agree (higher = better)."""
    n = len(route_segs)
    if n == 0 or len(client_segs) < n:
        return 0
    tail = client_segs[-n:]
    lit = 0
    for r, c in zip(route_segs, tail):
        if r == "{}" or c == "{}":
            continue
        if r != c:
            return 0
        lit += 1
    return lit


class Index:
    def __init__(self, repomap):
        self.files = repomap["files"]
        self.paths = repomap.get("paths", [])
        self.pathset = set(self.paths)
        self.by_base = defaultdict(list)
        for p in self.paths:
            self.by_base[posixpath.basename(p)].append(p)
        self.defs_by_name = defaultdict(list)
        self.defs_by_qual = defaultdict(list)
        self.calls_by_name = defaultdict(list)
        self.facts_by_kind = defaultdict(list)
        for f, rec in self.files.items():
            for i, d in enumerate(rec.get("defs", [])):
                self.defs_by_name[d[0]].append((f, i))
                if d[1] != d[0]:
                    self.defs_by_qual[d[1]].append((f, i))
            for c in rec.get("calls", []):
                self.calls_by_name[c[0]].append((f, c))
            for fa in rec.get("facts", []):
                self.facts_by_kind[fa[0]].append((f, fa))
        self._importers = None
        self._fwd = None

    # ---- helpers
    def d(self, f, i):
        return self.files[f]["defs"][i]

    def enc_name(self, f, i):
        if i is None or i < 0:
            return "(top level)"
        return self.files[f]["defs"][i][1]

    def lookup(self, sym):
        if sym in self.defs_by_qual:
            return list(self.defs_by_qual[sym])
        hits = list(self.defs_by_name.get(sym, []))
        if not hits and "." in sym:
            hits = list(self.defs_by_name.get(sym.rsplit(".", 1)[-1], []))
        return hits

    def resolve_file(self, q):
        q = q.replace("\\", "/").lstrip("./")
        if q in self.files or q in self.pathset:
            return q
        cands = [p for p in self.paths if p.endswith("/" + q)] or [p for p in self.paths if q in p]
        if len(cands) == 1:
            return cands[0]
        return cands if cands else None

    def _suffix(self, tail):
        tail = tail.lstrip("/")
        if tail in self.pathset:
            return tail
        best = None
        for p in self.by_base.get(posixpath.basename(tail), []):
            if p.endswith("/" + tail) and (best is None or len(p) < len(best)):
                best = p
        return best

    def resolve_ref(self, src, spec, lang):
        s = spec.split("?", 1)[0].split("#", 1)[0].strip()
        if not s:
            return None
        if s.startswith("package:"):
            tail = s.split("/", 1)[1] if "/" in s else ""
            return self._suffix("lib/" + tail) or self._suffix(tail)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", s) or s.startswith("//") or "{}" in s or "${" in s:
            return None
        if lang == "py" and not s.startswith("/"):
            if s.startswith("."):
                lvl = len(s) - len(s.lstrip("."))
                base = posixpath.dirname(src)
                for _ in range(lvl - 1):
                    base = posixpath.dirname(base)
                mod = s.lstrip(".").replace(".", "/")
                cand = posixpath.join(base, mod) if mod else base
                for ext in (".py", "/__init__.py"):
                    if posixpath.normpath(cand + ext) in self.pathset:
                        return posixpath.normpath(cand + ext)
                return None
            mod = s.replace(".", "/")
            return self._suffix(mod + ".py") or self._suffix(mod + "/__init__.py")
        if s.startswith("/"):
            return self._suffix(s)
        base = posixpath.dirname(src)
        cand = posixpath.normpath(posixpath.join(base, s))
        for ext in ("", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", "/index.js", "/index.ts", ".dart"):
            if cand + ext in self.pathset:
                return cand + ext
        if lang == "html":
            # browsers clamp "../" at the site root, and pages are often served from a public/ folder
            return self._suffix(re.sub(r"^(?:\.\.?/)+", "", s))
        return None

    def importers(self):
        """target file -> [(src, line, spec, version or None, kind)]"""
        if self._importers is None:
            imp = defaultdict(list)
            for f, rec in self.files.items():
                lang = rec.get("lang")
                for spec, line in rec.get("imports", []):
                    t = self.resolve_ref(f, spec, lang)
                    if t:
                        imp[t].append((f, line, spec, None, "import"))
                for fa in rec.get("facts", []):
                    if fa[0] == "load":
                        t = self.resolve_ref(f, fa[1].split("?", 1)[0], "html")
                        if t:
                            imp[t].append((f, fa[2], fa[1], (fa[3] or {}).get("v"), "load"))
            self._importers = imp
        return self._importers

    def pages_loading(self, target, depth=3):
        """HTML pages that load target directly or through imports."""
        imp = self.importers()
        seen = {target}
        q = deque([(target, 0)])
        pages = set()
        while q:
            t, dep = q.popleft()
            for src, _l, _s, _v, _k in imp.get(t, []):
                if self.files.get(src, {}).get("lang") == "html":
                    pages.add(src)
                elif src not in seen and dep < depth:
                    seen.add(src)
                    q.append((src, dep + 1))
        return sorted(pages)

    def reach(self, target):
        """Files whose code can see definitions in `target`: the file itself, files that
        import it (transitively), pages that load it, and other scripts on those pages."""
        if not hasattr(self, "_reach"):
            self._reach = {}
        if target in self._reach:
            return self._reach[target]
        imp = self.importers()
        out = {target}
        q = deque([target])
        while q:
            t = q.popleft()
            for src, _l, _s, _v, _k in imp.get(t, []):
                if src not in out:
                    out.add(src)
                    q.append(src)
        page_scripts = defaultdict(set)
        for t, lst in imp.items():
            for src, _l, _s, _v, kind in lst:
                if kind == "load":
                    page_scripts[src].add(t)
        for f in list(out):
            if self.files.get(f, {}).get("lang") == "html":
                out |= page_scripts.get(f, set())
        self._reach[target] = out
        return out

    def family(self, f):
        lang = self.files.get(f, {}).get("lang")
        return "web" if lang in ("js", "html") else lang

    def same_family(self, caller_file, name):
        fams = {self.family(df) for df, _ in self.defs_by_name.get(name, [])}
        return not fams or self.family(caller_file) in fams

    def ambiguous(self, name):
        return len({f for f, _ in self.defs_by_name.get(name, [])}) > 1

    def fwd(self):
        if self._fwd is None:
            g = defaultdict(set)
            for f, rec in self.files.items():
                defs = rec.get("defs", [])
                for c in rec.get("calls", []):
                    if c[3] is not None and c[3] >= 0 and c[0] in self.defs_by_name:
                        g[defs[c[3]][0]].add(c[0])
            self._fwd = g
        return self._fwd

    def routes(self):
        out = []
        for f, fa in self.facts_by_kind.get("route", []):
            if (fa[3] or {}).get("method") == "USE":
                continue
            out.append((f, fa))
        return out

    def services(self):
        """route file -> names of HTTP functions it exports (e.g. a Cloud Function name),
        and client file -> service names that appear in its URLs."""
        if getattr(self, "_svc", None) is None:
            route_files = {f for f, _ in self.routes()}
            exports = defaultdict(set)
            for f, fa in self.facts_by_kind.get("cloudfn", []):
                if f in route_files and (fa[3] or {}).get("trigger") in ("onRequest", "https"):
                    exports[f].add(fa[1])
            names = set().union(*exports.values()) if exports else set()
            mentions = defaultdict(set)
            if names:
                canon = {n.lower(): n for n in names}
                rx = re.compile(r"(?<![A-Za-z0-9_])(%s)(?![A-Za-z0-9_])" % "|".join(
                    re.escape(n) for n in sorted(names, key=len, reverse=True)), re.I)
                for kind in ("http", "pathstr"):
                    for f, fa in self.facts_by_kind.get(kind, []):
                        if "/" not in fa[1]:
                            continue
                        for m in rx.finditer(fa[1]):
                            mentions[f].add(canon[m.group(1).lower()])
            self._svc = (exports, mentions)
        return self._svc

    def client_refs(self):
        out = []
        for kind in ("http", "pathstr"):
            for f, fa in self.facts_by_kind.get(kind, []):
                out.append((f, fa))
        return out


# --------------------------------------------------------------------------
# context + commands
# --------------------------------------------------------------------------

class Ctx:
    def __init__(self, root, outdir, repomap):
        self.root = root
        self.outdir = outdir
        self.repomap = repomap
        self._index = None

    @property
    def I(self):
        if self._index is None:
            self._index = Index(self.repomap)
        return self._index


def _loc(f, line):
    return "%s:%s" % (f, line)


def _limit_rows(rows, limit):
    if limit and len(rows) > limit:
        return rows[:limit], len(rows) - limit
    return rows, 0


def _more(text, extra):
    if extra:
        text.append("  ... %d more (use --limit 0 for all)" % extra)


def cmd_status(ctx, a):
    files = ctx.repomap["files"]
    by_lang = defaultdict(int)
    errs = []
    for f, r in files.items():
        by_lang[r.get("lang")] += 1
        if r.get("err"):
            errs.append((f, r["err"]))
    meta = ctx.repomap["meta"]
    data = {"root": ctx.root, "map_dir": ctx.outdir, "files": len(files), "by_language": dict(by_lang),
            "built": meta.get("built"), "refreshed": meta.get("refreshed"), "git_head": meta.get("git_head"),
            "definitions": sum(len(r.get("defs", [])) for r in files.values()),
            "call_sites": sum(len(r.get("calls", [])) for r in files.values()),
            "errors": [{"file": f, "error": e} for f, e in errs], "version": __version__}
    text = ["repofree %s" % __version__,
            "repo:      %s" % ctx.root,
            "map:       %s" % os.path.join(ctx.outdir, "map.json"),
            "refreshed: %s (git %s)" % (meta.get("refreshed"), meta.get("git_head") or "-"),
            "files:     %d  (%s)" % (len(files), ", ".join("%s %d" % (k, v) for k, v in sorted(by_lang.items(), key=lambda x: -x[1]))),
            "mapped:    %d definitions, %d call sites" % (data["definitions"], data["call_sites"])]
    tiers = [l for l in by_lang if l not in FULL_TIER]
    if tiers:
        text.append("note:      %s use the basic tier (definitions, calls, imports, facts; no nesting for ruby)" % ", ".join(sorted(tiers)))
    for f, e in errs[:10]:
        text.append("could not parse: %s (%s)" % (f, e))
    return data, text


def cmd_find(ctx, a):
    I = ctx.I
    q = a.name
    hits = I.lookup(q)
    fuzzy = False
    if not hits:
        fuzzy = True
        ql = q.lower()
        for name, lst in I.defs_by_name.items():
            if ql in name.lower():
                hits.extend(lst)
        for qual, lst in I.defs_by_qual.items():
            if ql in qual.lower():
                hits.extend(x for x in lst if x not in hits)
    if a.kind:
        hits = [h for h in hits if I.d(*h)[2] == a.kind]
    rows = []
    for f, i in hits:
        d = I.d(f, i)
        rows.append({"name": d[0], "qual": d[1], "kind": d[2], "file": f, "line": d[3], "end": d[4],
                     "call_sites": len(I.calls_by_name.get(d[0], []))})
    rows.sort(key=lambda r: (r["name"].lower() != q.lower(), r["file"], r["line"]))
    shown, extra = _limit_rows(rows, a.limit)
    text = []
    if not rows:
        text.append("No definition found for %r." % q)
        n = len(I.calls_by_name.get(q.rsplit(".", 1)[-1], []))
        if n:
            text.append("But it is CALLED at %d place(s) - see: repofree callers %s" % (n, q))
    else:
        if fuzzy:
            text.append("No exact match for %r; similar names:" % q)
        for r in shown:
            text.append("%-40s %-8s %s-%s   (%d call sites)" % (r["qual"], r["kind"], _loc(r["file"], r["line"]), r["end"], r["call_sites"]))
        _more(text, extra)
    return {"query": q, "fuzzy": fuzzy, "results": rows}, text


def cmd_callers(ctx, a):
    I = ctx.I
    name = a.name.rsplit(".", 1)[-1]
    sites = I.calls_by_name.get(name, [])
    rows = []
    other_lang = 0
    for f, c in sites:
        if a.receiver and c[2] != a.receiver:
            continue
        if not I.same_family(f, name):
            other_lang += 1
            continue
        via = "callback/ref" if c[2] == "@ref" else ("html handler" if c[2] == "@html" else "call")
        row = {"file": f, "line": c[1], "in": I.enc_name(f, c[3]), "receiver": c[2] if c[2] not in ("@ref", "@html") else "",
               "via": via}
        if I.ambiguous(name):
            vis = set()
            for df, _ in I.defs_by_name.get(name, []):
                if f in I.reach(df):
                    vis.add(df)
            row["resolves_to"] = sorted(vis)
        rows.append(row)
    rows.sort(key=lambda r: (r["file"], r["line"]))
    defs = [{"file": f, "line": I.d(f, i)[3], "qual": I.d(f, i)[1]} for f, i in I.lookup(a.name)]
    shown, extra = _limit_rows(rows, a.limit)
    text = []
    if defs:
        text.append("%s is defined at: %s" % (a.name, ", ".join(_loc(d["file"], d["line"]) for d in defs[:6])))
    else:
        text.append("%s: no definition found in this repo (library/builtin, or defined dynamically)." % a.name)
    text.append("%d call site(s):" % len(rows))
    last = None
    for r in shown:
        if r["file"] != last:
            text.append("  %s" % r["file"])
            last = r["file"]
        recv = (r["receiver"] + ".") if r["receiver"] else ""
        extra_via = "" if r["via"] == "call" else "  [%s]" % r["via"]
        if "resolves_to" in r:
            extra_via += "  [-> %s]" % (", ".join(r["resolves_to"]) if r["resolves_to"] else "no visible definition")
        text.append("    :%-6s in %s  -> %s%s(%s" % (r["line"], r["in"], recv, name, extra_via and ")" + extra_via or ")"))
    _more(text, extra)
    if other_lang:
        text.append("(%d same-named call(s) in other languages left out)" % other_lang)
    text.append("note: calls made through variables or computed names are not visible to the map.")
    return {"name": a.name, "definitions": defs, "callers": rows}, text


def cmd_callees(ctx, a):
    I = ctx.I
    hits = I.lookup(a.name)
    out = []
    text = []
    if not hits:
        return {"name": a.name, "results": []}, ["No definition found for %r." % a.name]
    for f, i in hits:
        defs = I.files[f]["defs"]
        scope = {i}
        for k in range(i + 1, len(defs)):
            if defs[k][5] in scope:
                scope.add(k)
        counts = defaultdict(list)
        for c in I.files[f]["calls"]:
            if c[3] in scope:
                counts[c[0]].append(c[1])
        rows = []
        for name, lines in counts.items():
            where = I.defs_by_name.get(name, [])
            if not where and not a.all:
                continue
            rows.append({"name": name, "count": len(lines), "lines": sorted(lines),
                         "defined_at": [_loc(wf, I.d(wf, wi)[3]) for wf, wi in where[:3]]})
        rows.sort(key=lambda r: r["lines"][0])
        d = defs[i]
        out.append({"function": d[1], "file": f, "line": d[3], "calls": rows})
        text.append("%s  (%s:%s-%s) calls:" % (d[1], f, d[3], d[4]))
        shown, extra = _limit_rows(rows, a.limit)
        for r in shown:
            text.append("  %-34s x%-3d at lines %s   %s" % (r["name"], r["count"], ",".join(map(str, r["lines"][:8])),
                                                      ("-> " + ", ".join(r["defined_at"])) if r["defined_at"] else "(not defined in repo)"))
        _more(text, extra)
        if not a.all:
            text.append("  (only functions defined in this repo; add --all to include library/builtin calls)")
    return {"name": a.name, "results": out}, text


def cmd_impact(ctx, a):
    I = ctx.I
    target = a.target
    names = []
    files_hit = set()
    rf = None
    if target not in I.defs_by_name and target not in I.defs_by_qual:
        rf = I.resolve_file(target)
        if isinstance(rf, list):
            return {"target": target, "ambiguous": rf}, ["%r matches several files:" % target] + ["  " + x for x in rf[:20]]
    text = []
    data = {"target": target, "levels": []}
    if rf and rf in I.files:
        names = sorted({d[0] for d in I.files[rf]["defs"]})
        files_hit.add(rf)
        data["file"] = rf
        text.append("Impact of changing file %s (%d definitions)" % (rf, len(names)))
        imp = I.importers().get(rf, [])
        if imp:
            text.append("Loaded/imported by:")
            for src, line, spec, v, kind in sorted(imp):
                text.append("  %s  %s%s" % (_loc(src, line), spec, "" if not v else ""))
        data["loaded_by"] = [{"file": s, "line": l, "spec": sp, "version": v} for s, l, sp, v, k in imp]
    else:
        hits = I.lookup(target)
        if not hits:
            return {"target": target}, ["No function or file found for %r." % target]
        names = [target.rsplit(".", 1)[-1]]
        for f, i in hits:
            files_hit.add(f)
        text.append("Impact of changing %s (defined at %s)" % (target, ", ".join(_loc(f, I.d(f, i)[3]) for f, i in hits[:5])))
    seen = set(names)
    frontier = list(names)
    origin = {}
    for n in names:
        origin[n] = {rf} if rf else {f for f, _ in I.lookup(target)}
    skipped = 0
    for depth in range(1, a.depth + 1):
        level = []
        nxt = []
        for n in frontier:
            visible = set()
            if I.ambiguous(n):
                for of in origin.get(n, set()):
                    visible |= I.reach(of)
            for f, c in I.calls_by_name.get(n, []):
                if rf and f == rf:
                    continue
                if (visible and f not in visible) or not I.same_family(f, n):
                    skipped += 1
                    continue
                if c[3] is not None and c[3] >= 0:
                    d = I.d(f, c[3])
                    level.append({"caller": d[1], "file": f, "line": c[1], "calls": n})
                    files_hit.add(f)
                    if d[0] not in seen:
                        seen.add(d[0])
                        nxt.append(d[0])
                        origin[d[0]] = {f}
                else:
                    level.append({"caller": "(top level)", "file": f, "line": c[1], "calls": n})
                    files_hit.add(f)
        if not level:
            break
        data["levels"].append(level)
        uniq = {}
        for r in level:
            uniq.setdefault((r["caller"], r["file"]), r)
        text.append("Level %d - %d caller(s):" % (depth, len(uniq)))
        shown, extra = _limit_rows(sorted(uniq.values(), key=lambda r: (r["file"], r["line"])), a.limit)
        for r in shown:
            text.append("  %-40s %s   (calls %s)" % (r["caller"], _loc(r["file"], r["line"]), r["calls"]))
        _more(text, extra)
        frontier = nxt
    pages = set()
    for f in files_hit:
        if I.files.get(f, {}).get("lang") == "html":
            pages.add(f)
        pages.update(I.pages_loading(f))
    data["files"] = sorted(files_hit)
    data["pages"] = sorted(pages)
    text.append("Files involved: %d" % len(files_hit))
    if skipped:
        text.append("(%d call site(s) of same-named functions in unrelated files were left out)" % skipped)
    if pages:
        text.append("Pages that load affected code (%d): %s" % (len(pages), ", ".join(sorted(pages)[:30])))
    text.append("note: pattern-based; dynamic calls are not visible. Confirm in the code before changing.")
    return data, text


def cmd_path(ctx, a):
    I = ctx.I
    src = a.src.rsplit(".", 1)[-1]
    dst = a.dst.rsplit(".", 1)[-1]
    g = I.fwd()
    prev = {src: None}
    q = deque([src])
    while q:
        n = q.popleft()
        if n == dst:
            break
        for m in g.get(n, ()):
            if m not in prev:
                prev[m] = n
                q.append(m)
    if dst not in prev:
        return {"from": src, "to": dst, "path": None}, ["No call chain found from %s to %s (within what the map can see)." % (src, dst)]
    chain = []
    n = dst
    while n is not None:
        chain.append(n)
        n = prev[n]
    chain.reverse()
    rows = []
    for n in chain:
        locs = I.defs_by_name.get(n, [])
        rows.append({"name": n, "defined_at": [_loc(f, I.d(f, i)[3]) for f, i in locs[:3]]})
    text = ["Call chain (%d steps):" % (len(chain) - 1)]
    for k, r in enumerate(rows):
        text.append("  %s%s  %s" % ("  " * k, r["name"], ", ".join(r["defined_at"])))
    text.append("note: names are matched by function name; same-named functions are merged.")
    return {"from": src, "to": dst, "path": rows}, text


def cmd_file(ctx, a):
    I = ctx.I
    rf = I.resolve_file(a.path)
    if rf is None:
        return {"path": a.path}, ["No file matches %r." % a.path]
    if isinstance(rf, list):
        return {"path": a.path, "ambiguous": rf}, ["%r matches several files:" % a.path] + ["  " + x for x in rf[:30]]
    rec = I.files.get(rf)
    imp = I.importers().get(rf, [])
    text = ["%s" % rf]
    data = {"file": rf, "loaded_by": [{"file": s, "line": l, "spec": sp, "version": v} for s, l, sp, v, k in imp]}
    if imp:
        text.append("Loaded/imported by (%d):" % len(imp))
        for s, l, sp, v, k in sorted(imp)[: a.limit or None]:
            text.append("  %s   %s" % (_loc(s, l), sp))
    if not rec:
        text.append("(not a source file the map reads)")
        return data, text
    data.update({"lang": rec.get("lang"), "lines": rec.get("lines"), "definitions": rec.get("defs", []),
                 "imports": rec.get("imports", []), "facts": rec.get("facts", [])})
    text.append("%s, %s lines, %d definitions, %d call sites" % (rec.get("lang"), rec.get("lines"), len(rec.get("defs", [])), len(rec.get("calls", []))))
    if rec.get("imports"):
        text.append("Imports:")
        for spec, line in rec["imports"]:
            t = I.resolve_ref(rf, spec, rec.get("lang"))
            text.append("  :%-6s %s%s" % (line, spec, ("  -> " + t) if t and t != spec else ""))
    defs = rec.get("defs", [])
    if defs:
        text.append("Definitions:")
        rows, extra = _limit_rows(defs, a.limit)
        for d in rows:
            text.append("  :%-6s %-8s %s  (to line %s)" % (d[3], d[2], d[1], d[4]))
        _more(text, extra)
    facts = [f for f in rec.get("facts", []) if f[0] != "pathstr"]
    if facts:
        text.append("Facts:")
        for kind in ("route", "cloudfn", "http", "collection", "emit", "listen", "storage", "env", "global", "load", "handler"):
            fs = [f for f in facts if f[0] == kind]
            if not fs:
                continue
            for f in fs[: a.limit or None]:
                text.append("  :%-6s %-10s %s%s" % (f[2], kind, f[1], _fmt_extra(f[3])))
    return data, text


def _fmt_extra(ex):
    if not ex:
        return ""
    parts = []
    for k in ("method", "ops", "flags", "api", "via", "store", "op", "trigger", "tag", "v", "code", "path", "expr"):
        if k in ex:
            v = ex[k]
            parts.append("%s=%s" % (k, ",".join(v) if isinstance(v, list) else v))
    return "  [" + " ".join(parts) + "]" if parts else ""


def cmd_uses(ctx, a):
    I = ctx.I
    t = a.text.lower()
    rows = []
    for f, rec in I.files.items():
        for fa in rec.get("facts", []):
            hay = (fa[1] + " " + json.dumps(fa[3] or {}, ensure_ascii=False)).lower()
            if t in hay:
                rows.append({"kind": fa[0], "value": fa[1], "file": f, "line": fa[2], "in": I.enc_name(f, fa[4]), "extra": fa[3]})
        for spec, line in rec.get("imports", []):
            if t in spec.lower():
                rows.append({"kind": "import", "value": spec, "file": f, "line": line, "in": "", "extra": None})
    for name, lst in I.defs_by_name.items():
        if t in name.lower():
            for f, i in lst:
                d = I.d(f, i)
                rows.append({"kind": "definition", "value": d[1], "file": f, "line": d[3], "in": "", "extra": None})
    order = ["definition", "route", "cloudfn", "http", "collection", "emit", "listen", "storage", "env", "global",
             "load", "import", "handler", "pathstr"]
    rows.sort(key=lambda r: (order.index(r["kind"]) if r["kind"] in order else 99, r["file"], r["line"]))
    shown, extra = _limit_rows(rows, a.limit)
    text = ["%d match(es) for %r:" % (len(rows), a.text)]
    last = None
    for r in shown:
        if r["kind"] != last:
            text.append("[%s]" % r["kind"])
            last = r["kind"]
        text.append("  %-48s %s%s%s" % (r["value"][:48], _loc(r["file"], r["line"]),
                                        ("  in " + r["in"]) if r["in"] and r["in"] != "(top level)" else "", _fmt_extra(r["extra"])))
    _more(text, extra)
    return {"text": a.text, "results": rows}, text


def _endpoint_pairs(I):
    """[(route_file, route_fact, [(client_file, client_fact), ...])] - each client goes to its best route(s)."""
    cached = getattr(I, "_ep_pairs", None)
    if cached is not None:
        return cached
    refs = []
    for f, fa in I.client_refs():
        refs.append((f, fa, _segments(fa[1])))
    route_sites = {(f, fa[2]) for f, fa in I.facts_by_kind.get("route", [])}
    refs = [r for r in refs if (r[0], r[1][2]) not in route_sites]
    exports, mentions = I.services()
    all_routes = I.routes()
    # score every (route, client) pair, then give each client only to its best-matching route(s)
    scores = defaultdict(dict)
    best = defaultdict(int)
    for ri, (f, fa) in enumerate(all_routes):
        rs = _segments(fa[1])
        for ci, (cf, cfa, cs) in enumerate(refs):
            sc = _route_matches(rs, cs)
            if not sc:
                continue
            if exports.get(f) and mentions.get(cf) and not (exports[f] & mentions[cf]):
                continue  # the calling file talks to a different service
            scores[ri][ci] = sc
            best[ci] = max(best[ci], sc)
    out = []
    for ri, (f, fa) in enumerate(all_routes):
        clients = []
        seen_c = set()
        for ci, sc in sorted(scores[ri].items()):
            cf, cfa, cs = refs[ci]
            if sc < best[ci] or (cf, cfa[2]) in seen_c:
                continue
            seen_c.add((cf, cfa[2]))
            clients.append((cf, cfa))
        out.append((f, fa, clients))
    I._ep_pairs = out
    return out


def _route_handler(I, f, fa):
    """(handler name, def index or None) for a route fact."""
    meth = (fa[3] or {}).get("method", "ANY")
    hname = "%s %s" % (meth, fa[1])
    for i, d in enumerate(I.files[f].get("defs", [])):
        if d[0] == hname and abs(d[3] - fa[2]) <= 3:
            return hname, i
    for i, d in enumerate(I.files[f].get("defs", [])):
        if d[0] == hname:
            return hname, i
    enc = fa[4] if fa[4] is not None and fa[4] >= 0 else None
    return I.enc_name(f, enc), enc


def cmd_endpoints(ctx, a):
    I = ctx.I
    exports, _mentions = I.services()
    rows = []
    for f, fa, cl in _endpoint_pairs(I):
        path = fa[1]
        if a.text and a.text.lower() not in path.lower() and a.text.lower() not in f.lower():
            continue
        clients = [{"file": cf, "line": cfa[2], "value": cfa[1], "kind": cfa[0], "in": I.enc_name(cf, cfa[4])}
                   for cf, cfa in cl]
        meth = (fa[3] or {}).get("method", "ANY")
        handler = _route_handler(I, f, fa)[0]
        rows.append({"method": meth, "path": path, "file": f, "line": fa[2],
                     "service": sorted(exports.get(f, [])), "handler": handler, "clients": clients})
    rows.sort(key=lambda r: (r["file"], r["line"]))
    text = []
    if a.unmatched:
        matched = set()
        for r in rows:
            for c in r["clients"]:
                matched.add((c["file"], c["line"], c["value"]))
        um = []
        for f, fa in I.facts_by_kind.get("http", []):
            if (f, fa[2], fa[1]) not in matched:
                um.append({"file": f, "line": fa[2], "url": fa[1], "extra": fa[3]})
        text.append("%d HTTP call(s) with no matching route in this repo (external APIs or dynamic URLs):" % len(um))
        shown, extra = _limit_rows(um, a.limit)
        for u in shown:
            text.append("  %-50s %s%s" % (u["url"][:50], _loc(u["file"], u["line"]), _fmt_extra(u["extra"])))
        _more(text, extra)
        return {"unmatched": um}, text
    shown, extra = _limit_rows(rows, a.limit)
    text.append("%d route(s)%s:" % (len(rows), (" matching %r" % a.text) if a.text else ""))
    for r in shown:
        text.append("%-6s %-44s %s%s" % (r["method"], r["path"], _loc(r["file"], r["line"]),
                                         ("   [" + ",".join(r["service"]) + "]") if r["service"] else ""))
        cl, cextra = _limit_rows(r["clients"], 8 if not a.text else 0)
        for c in cl:
            text.append("         <- %s   %s" % (_loc(c["file"], c["line"]), c["value"][:70]))
        if cextra:
            text.append("         <- ... %d more (narrow with: endpoints %s)" % (cextra, r["path"]))
        if not r["clients"]:
            text.append("         <- (no caller found in this repo)")
    _more(text, extra)
    text.append("note: callers are matched by URL path text (and by service name when the URL contains one);")
    text.append("      --unmatched lists HTTP calls with no route in this repo.")
    return {"routes": rows}, text


def cmd_collections(ctx, a):
    I = ctx.I
    sites = I.facts_by_kind.get("collection", [])
    if a.name:
        rows = []
        for f, fa in sites:
            if fa[1].lower() != a.name.lower() and not (a.name.lower() in fa[1].lower() and a.fuzzy):
                continue
            ex = fa[3] or {}
            ops = ex.get("ops", [])
            mode = _mode(ops)
            if a.writes and mode not in ("write", "read+write"):
                continue
            rows.append({"collection": fa[1], "file": f, "line": fa[2], "in": I.enc_name(f, fa[4]), "mode": mode,
                         "ops": ops, "flags": ex.get("flags", []), "api": ex.get("api"), "path": ex.get("path")})
        rows.sort(key=lambda r: (r["file"], r["line"]))
        shown, extra = _limit_rows(rows, a.limit)
        text = ["%d site(s) for collection %r:" % (len(rows), a.name)]
        for r in shown:
            text.append("  %-10s %s  in %s  %s%s" % (r["mode"], _loc(r["file"], r["line"]), r["in"],
                                                  ",".join(r["ops"]) or "", ("  [" + ",".join(r["flags"]) + "]") if r["flags"] else ""))
        _more(text, extra)
        text.append("modes: read/write from the call chain on that statement; 'ref' = reference built here, used elsewhere.")
        return {"collection": a.name, "sites": rows}, text
    agg = {}
    for f, fa in sites:
        r = agg.setdefault(fa[1], {"collection": fa[1], "files": set(), "read": 0, "write": 0, "ref": 0, "no_limit": 0})
        r["files"].add(f)
        m = _mode((fa[3] or {}).get("ops", []))
        if "read" in m:
            r["read"] += 1
        if "write" in m:
            r["write"] += 1
        if m in ("ref", "path"):
            r["ref"] += 1
        if "no-limit-seen" in (fa[3] or {}).get("flags", []):
            r["no_limit"] += 1
    rows = sorted(agg.values(), key=lambda r: (-len(r["files"]), r["collection"]))
    for r in rows:
        r["files"] = sorted(r["files"])
    shown, extra = _limit_rows(rows, a.limit)
    text = ["%d collection(s)/table(s):" % len(rows), "  %-34s %5s %5s %5s %5s %8s" % ("name", "files", "read", "write", "ref", "no-limit")]
    for r in shown:
        text.append("  %-34s %5d %5d %5d %5d %8d" % (r["collection"][:34], len(r["files"]), r["read"], r["write"], r["ref"], r["no_limit"]))
    _more(text, extra)
    text.append("details: repofree collections NAME   (no-limit = read with no .limit() seen on that statement)")
    return {"collections": rows}, text


def _mode(ops):
    r = any(o in _READ_OPS for o in ops)
    w = any(o in _WRITE_OPS for o in ops)
    if r and w:
        return "read+write"
    if r:
        return "read"
    if w:
        return "write"
    if ops == ["path"]:
        return "path"
    return "ref"


def cmd_events(ctx, a):
    I = ctx.I
    emits = defaultdict(list)
    listens = defaultdict(list)
    for f, fa in I.facts_by_kind.get("emit", []):
        emits[fa[1]].append({"file": f, "line": fa[2], "in": I.enc_name(f, fa[4])})
    for f, fa in I.facts_by_kind.get("listen", []):
        listens[fa[1]].append({"file": f, "line": fa[2], "in": I.enc_name(f, fa[4])})
    names = set(emits) | {n for n in listens if n not in _DOM_EVENTS or a.all}
    if a.name:
        names = {n for n in names | set(listens) if a.name.lower() in n.lower() and (a.all or n not in _DOM_EVENTS or n in emits)}
    rows = []
    for n in sorted(names):
        rows.append({"event": n, "emitted_by": emits.get(n, []), "listened_by": listens.get(n, []),
                     "dom": n in _DOM_EVENTS})
    shown, extra = _limit_rows(rows, a.limit)
    text = ["%d event(s)%s:" % (len(rows), "" if a.all else " (custom; --all adds DOM events)")]
    for r in shown:
        flag = ""
        if not r["emitted_by"] and not r["dom"]:
            flag = "  [listened, never fired here]"
        elif not r["listened_by"]:
            flag = "  [fired, nobody listens here]"
        text.append("%s%s" % (r["event"], flag))
        for e in r["emitted_by"][:10]:
            text.append("   fires  %s  in %s" % (_loc(e["file"], e["line"]), e["in"]))
        for e in r["listened_by"][:10]:
            text.append("   listen %s  in %s" % (_loc(e["file"], e["line"]), e["in"]))
        more = len(r["emitted_by"]) + len(r["listened_by"]) - min(len(r["emitted_by"]), 10) - min(len(r["listened_by"]), 10)
        if more > 0:
            text.append("   ... %d more (repofree events %s --json)" % (more, r["event"]))
    _more(text, extra)
    return {"events": rows}, text


def _simple_fact_cmd(ctx, a, kind, title, key_label):
    I = ctx.I
    groups = defaultdict(list)
    for f, fa in I.facts_by_kind.get(kind, []):
        if a.name and a.name.lower() not in fa[1].lower():
            continue
        groups[fa[1]].append({"file": f, "line": fa[2], "in": I.enc_name(f, fa[4]), "extra": fa[3]})
    rows = [{key_label: k, "sites": v} for k, v in sorted(groups.items())]
    shown, extra = _limit_rows(rows, a.limit)
    text = ["%d %s:" % (len(rows), title)]
    for r in shown:
        text.append("%s   (%d site%s)" % (r[key_label], len(r["sites"]), "" if len(r["sites"]) == 1 else "s"))
        cap = 0 if a.name else 6
        sites = r["sites"] if not cap else r["sites"][:cap]
        for s in sites:
            text.append("   %s  in %s%s" % (_loc(s["file"], s["line"]), s["in"], _fmt_extra(s["extra"])))
        if cap and len(r["sites"]) > cap:
            text.append("   ... %d more" % (len(r["sites"]) - cap))
    _more(text, extra)
    return {kind: rows}, text


def cmd_storage(ctx, a):
    return _simple_fact_cmd(ctx, a, "storage", "storage key(s)", "key")


def cmd_env(ctx, a):
    return _simple_fact_cmd(ctx, a, "env", "environment variable(s)/secret(s)", "name")


def cmd_globals(ctx, a):
    return _simple_fact_cmd(ctx, a, "global", "global(s) assigned on window/globalThis", "name")


def cmd_services(ctx, a):
    I = ctx.I
    rows = []
    for f, fa in I.facts_by_kind.get("cloudfn", []):
        rows.append({"name": fa[1], "trigger": (fa[3] or {}).get("trigger"), "file": f, "line": fa[2]})
    per_file = defaultdict(int)
    for f, fa in I.routes():
        per_file[f] += 1
    rows.sort(key=lambda r: (r["file"], r["line"]))
    text = ["%d exported cloud/serverless function(s):" % len(rows)]
    for r in rows[: a.limit or None]:
        text.append("  %-34s %-20s %s" % (r["name"], r["trigger"], _loc(r["file"], r["line"])))
    text.append("Files defining HTTP routes:")
    for f, n in sorted(per_file.items(), key=lambda x: -x[1]):
        text.append("  %-60s %d route(s)" % (f, n))
    return {"functions": rows, "route_files": dict(per_file)}, text


def _git_changed(root):
    out = _run(["git", "-C", root, "status", "--porcelain", "-z", "--untracked-files=all"])
    if out is None:
        return None
    changed = set()
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        p = parts[i]
        if len(p) > 3:
            st, path = p[:2], p[3:]
            changed.add(path)
            if "R" in st:
                i += 1
        i += 1
    return changed


def cmd_versions(ctx, a):
    I = ctx.I
    refs = defaultdict(list)
    for f, rec in I.files.items():
        for fa in rec.get("facts", []):
            if fa[0] != "load":
                continue
            raw = fa[1]
            if re.match(r"^(?:[a-z]+:)?//", raw, re.I):
                continue
            t = I.resolve_ref(f, raw, "html") or ("(missing) " + raw.split("?")[0])
            refs[t].append({"page": f, "line": fa[2], "v": (fa[3] or {}).get("v"), "raw": raw})
    text = []
    data = {}
    if a.changed:
        ch = _git_changed(ctx.root)
        if ch is None:
            return {}, ["--changed needs git."]
        rows = []
        for t in sorted(ch):
            if t not in refs:
                continue
            for r in refs[t]:
                page = r["page"]
                if page in ch:
                    diff = _run(["git", "-C", ctx.root, "diff", "-U0", "HEAD", "--", page]) or ""
                    base = posixpath.basename(t)
                    bumped = any(l.startswith("+") and base in l and _VER_RE.search(l) for l in diff.splitlines())
                    if not diff and page in ch:
                        bumped = True  # new untracked page
                else:
                    bumped = False
                rows.append({"file": t, "page": page, "line": r["line"], "version": r["v"], "bumped": bumped})
        data["changed"] = rows
        bad = [r for r in rows if not r["bumped"]]
        text.append("Changed files referenced by pages: %d reference(s), %d NOT bumped" % (len(rows), len(bad)))
        for r in rows:
            text.append("  %-9s %s  in %s  (v=%s)" % ("ok" if r["bumped"] else "NOT BUMPED", r["file"], _loc(r["page"], r["line"]), r["version"]))
        if not rows:
            text.append("  (no changed file is loaded by a page)")
        return data, text
    rows = []
    for t, lst in sorted(refs.items()):
        if a.file and a.file not in t:
            continue
        vs = sorted({r["v"] or "(none)" for r in lst})
        issue = None
        if t.startswith("(missing)"):
            issue = "file not found"
        elif len(vs) > 1:
            issue = "different versions" if "(none)" not in vs else "some pages have no version"
        rows.append({"file": t, "versions": vs, "pages": lst, "issue": issue})
    issues = [r for r in rows if r["issue"]]
    show = rows if (a.all or a.file) else issues
    text.append("%d loaded file(s); %d with an issue%s:" % (len(rows), len(issues), "" if (a.all or a.file) else " (--all shows every file)"))
    shown, extra = _limit_rows(show, a.limit)
    for r in shown:
        text.append("%s   versions: %s%s" % (r["file"], ", ".join(r["versions"]), ("   <- " + r["issue"]) if r["issue"] else ""))
        for p in r["pages"][: (0 if a.file else 12) or None]:
            text.append("   %s  v=%s" % (_loc(p["page"], p["line"]), p["v"] or "(none)"))
    _more(text, extra)
    data["files"] = rows
    return data, text


def _apply_map(name, maps):
    for old, new in maps:
        for o, n in ((old, new), (old.lower(), new.lower()), (old.capitalize(), new.capitalize()), (old.upper(), new.upper())):
            name = name.replace(o, n)
    return name


def _side(ctx, sel):
    I = ctx.I
    sel = sel.replace("\\", "/")
    files = [f for f in I.files if fnmatch.fnmatch(f, sel) or f.startswith(sel.rstrip("/") + "/") or f == sel]
    out = {k: {} for k in ("functions", "routes", "collections", "events", "storage", "globals")}
    for f in files:
        rec = I.files[f]
        for d in rec.get("defs", []):
            out["functions"].setdefault(d[0], _loc(f, d[3]))
        for fa in rec.get("facts", []):
            k = {"route": "routes", "collection": "collections", "emit": "events", "listen": "events",
                 "storage": "storage", "global": "globals"}.get(fa[0])
            if not k:
                continue
            if fa[0] == "listen" and fa[1] in _DOM_EVENTS:
                continue
            val = fa[1] if fa[0] != "route" else "%s %s" % ((fa[3] or {}).get("method", "ANY"), fa[1])
            out[k].setdefault(val, _loc(f, fa[2]))
    return files, out


def cmd_compare(ctx, a):
    maps = []
    for m in a.map or []:
        if "=" in m:
            o, n = m.split("=", 1)
            maps.append((o, n))
    fa, A = _side(ctx, a.a)
    fb, B = _side(ctx, a.b)
    text = ["A = %s (%d files)   B = %s (%d files)%s" % (a.a, len(fa), a.b, len(fb),
                                                        ("   map: " + ", ".join("%s->%s" % m for m in maps)) if maps else "")]
    data = {"a": a.a, "b": a.b, "a_files": len(fa), "b_files": len(fb), "sections": {}}
    if not fa or not fb:
        text.append("One side matched no files. Use a folder prefix (e.g. src/admin) or a glob (e.g. 'public/js/*.js').")
        return data, text
    for sec in ("functions", "routes", "collections", "events", "storage", "globals"):
        amap = {}
        for k, loc in A[sec].items():
            amap.setdefault(_apply_map(k, maps), (k, loc))
        bkeys = set(B[sec])
        only_a = sorted((k for k in amap if k not in bkeys), key=str.lower)
        rev = {}
        for k in amap:
            rev[k] = True
        only_b = sorted((k for k in bkeys if k not in rev), key=str.lower)
        common = len(set(amap) & bkeys)
        data["sections"][sec] = {"only_in_a": [{"name": amap[k][0], "as": k, "at": amap[k][1]} for k in only_a],
                                 "only_in_b": [{"name": k, "at": B[sec][k]} for k in only_b], "in_both": common}
        if not only_a and not only_b and not common:
            continue
        text.append("")
        text.append("%s: %d in both, %d only in A, %d only in B" % (sec.upper(), common, len(only_a), len(only_b)))
        sa, ea = _limit_rows(only_a, a.limit)
        for k in sa:
            orig, loc = amap[k]
            text.append("  A only  %-40s %s%s" % (orig, loc, ("   (looked for %s)" % k) if k != orig else ""))
        _more(text, ea)
        if a.both_ways:
            sb, eb = _limit_rows(only_b, a.limit)
            for k in sb:
                text.append("  B only  %-40s %s" % (k, B[sec][k]))
            _more(text, eb)
    if not a.both_ways:
        text.append("")
        text.append("(add --both-ways to also list what B has and A lacks)")
    return data, text


# --------------------------------------------------------------------------
# report / export
# --------------------------------------------------------------------------

def build_report(ctx, full=False):
    I = ctx.I
    files = I.files
    meta = ctx.repomap["meta"]
    L = []
    L.append("# Code map: %s" % os.path.basename(ctx.root.rstrip("/")))
    L.append("")
    L.append("Generated by repofree %s on %s (git %s). Refreshes itself before every `repofree` command." % (
        __version__, meta.get("refreshed"), meta.get("git_head") or "-"))
    L.append("This is a navigation aid: confirm details in the source before changing code. "
             "It is pattern-based and cannot see calls made through variables or computed names.")
    L.append("")
    by_lang = defaultdict(lambda: [0, 0])
    for f, r in files.items():
        by_lang[r.get("lang")][0] += 1
        by_lang[r.get("lang")][1] += r.get("lines", 0)
    L.append("## Overview")
    L.append("")
    L.append("| language | files | lines |")
    L.append("|---|---:|---:|")
    for k, (n, ln) in sorted(by_lang.items(), key=lambda x: -x[1][1]):
        L.append("| %s | %d | %d |" % (k, n, ln))
    L.append("")
    dirs = defaultdict(lambda: [0, 0])
    for f, r in files.items():
        top = f.split("/")[0] if "/" in f else "(root)"
        sub = "/".join(f.split("/")[:2]) if f.count("/") >= 2 else top
        dirs[sub][0] += 1
        dirs[sub][1] += r.get("lines", 0)
    L.append("## Folders")
    L.append("")
    for k, (n, ln) in sorted(dirs.items(), key=lambda x: -x[1][1])[:40]:
        L.append("- `%s` - %d files, %d lines" % (k, n, ln))
    L.append("")
    L.append("## Largest files")
    L.append("")
    for f, r in sorted(files.items(), key=lambda x: -x[1].get("lines", 0))[:20]:
        L.append("- `%s` - %d lines, %d definitions" % (f, r.get("lines", 0), len(r.get("defs", []))))
    L.append("")
    # most-called: count each call only for the definition it resolves to; app code only
    sites = defaultdict(int)
    caller_files = defaultdict(set)
    for f, c, targets, maybe in resolved_calls(I):
        if maybe or _is_test_path(f):
            continue
        for t in targets:
            if _is_test_path(t[0]) or (c[3] == t[1] and f == t[0]):
                continue
            sites[t] += 1
            caller_files[t].add(f)
    hub = sorted(sites.items(), key=lambda x: (-x[1], x[0]))
    L.append("## Most-called functions")
    L.append("")
    L.append("App code only (test files left out). Each call is counted for the definition it resolves to.")
    L.append("")
    for (f, i), n in hub[:30]:
        nf = len(caller_files[(f, i)])
        L.append("- `%s` - %d call site%s in %d file%s - `%s:%d`" % (
            I.d(f, i)[1], n, "" if n == 1 else "s", nf, "" if nf == 1 else "s", f, I.d(f, i)[3]))
    L.append("")
    cf = I.facts_by_kind.get("cloudfn", [])
    if cf:
        L.append("## Cloud / serverless functions")
        L.append("")
        for f, fa in sorted(cf, key=lambda x: (x[0], x[1][2])):
            L.append("- `%s` (%s) - `%s:%d`" % (fa[1], (fa[3] or {}).get("trigger"), f, fa[2]))
        L.append("")
    routes = I.routes()
    if routes:
        L.append("## HTTP routes")
        L.append("")
        per = defaultdict(list)
        for f, fa in routes:
            per[f].append(fa)
        for f in sorted(per):
            L.append("### `%s` (%d)" % (f, len(per[f])))
            L.append("")
            for fa in per[f][: (None if full else 80)]:
                L.append("- `%s %s` - line %d" % ((fa[3] or {}).get("method", "ANY"), fa[1], fa[2]))
            if not full and len(per[f]) > 80:
                L.append("- ... %d more (`repofree endpoints`)" % (len(per[f]) - 80))
            L.append("")
    colls = I.facts_by_kind.get("collection", [])
    if colls:
        agg = defaultdict(lambda: {"files": set(), "read": 0, "write": 0})
        for f, fa in colls:
            r = agg[fa[1]]
            r["files"].add(f)
            m = _mode((fa[3] or {}).get("ops", []))
            r["read"] += "read" in m
            r["write"] += "write" in m
        L.append("## Database collections / tables")
        L.append("")
        L.append("| name | files | read sites | write sites |")
        L.append("|---|---:|---:|---:|")
        for k, r in sorted(agg.items(), key=lambda x: -len(x[1]["files"]))[: (None if full else 60)]:
            L.append("| `%s` | %d | %d | %d |" % (k, len(r["files"]), r["read"], r["write"]))
        L.append("")
    emits = {fa[1] for f, fa in I.facts_by_kind.get("emit", [])}
    listens = {fa[1] for f, fa in I.facts_by_kind.get("listen", []) if fa[1] not in _DOM_EVENTS}
    dom_fired = sorted(e for e in emits if e in _DOM_EVENTS)
    emits = {e for e in emits if e not in _DOM_EVENTS}
    if emits:
        L.append("## Custom events (fired in this repo)")
        L.append("")
        for e in sorted(emits)[: (None if full else 80)]:
            L.append("- `%s`%s" % (e, "" if e in listens else " (nobody listens here)"))
        L.append("")
    outside = sorted(listens - emits)
    if outside:
        L.append("Also listened for, but fired by the browser, a framework or a library (%d): %s" % (
            len(outside), ", ".join("`%s`" % e for e in outside[: (None if full else 80)])))
        L.append("")
    if dom_fired:
        L.append("Standard browser events this code fires itself (e.g. to re-run a listener): %s" % ", ".join("`%s`" % e for e in dom_fired))
        L.append("")
    for kind, title in (("storage", "Browser storage keys"), ("env", "Environment variables / secrets"),
                        ("global", "Window globals")):
        vals = sorted({fa[1] for f, fa in I.facts_by_kind.get(kind, [])})
        short = []
        if kind == "global":
            short = [v for v in vals if len(v.strip("_$")) <= 2]
            vals = [v for v in vals if v not in short]
        if vals or short:
            L.append("## %s (%d)" % (title, len(vals) + len(short)))
            L.append("")
            if vals:
                L.append(", ".join("`%s`" % v for v in vals[: (None if full else 150)]))
            if short:
                L.append("")
                L.append("Short one- or two-letter names (often debug handles): %s" % ", ".join("`%s`" % v for v in short))
            L.append("")
    pages = sorted(f for f, r in files.items() if r.get("lang") == "html")
    if pages:
        imp_by_page = defaultdict(list)
        for t, lst in I.importers().items():
            for src, line, spec, v, kind in lst:
                if kind == "load":
                    imp_by_page[src].append((line, t))

        def page_line(p):
            scripts = [t for _l, t in sorted(imp_by_page.get(p, [])) if lang_of(t)]
            seen, dups, names = set(), set(), []
            for t in scripts:
                if t in seen:
                    dups.add(t)
                    continue
                seen.add(t)
                names.append(posixpath.basename(t))
            more = (" - loads " + ", ".join("`%s`" % n for n in names[:12]) + (" ..." if len(names) > 12 else "")) if names else ""
            if dups:
                more += " - **loads %s more than once**" % ", ".join("`%s`" % posixpath.basename(d) for d in sorted(dups))
            return "- `%s`%s" % (p, more)

        aux = [p for p in pages if _is_aux_path(p)]
        app = [p for p in pages if p not in aux]
        L.append("## Pages (%d)" % len(app))
        L.append("")
        for p in app[: (None if full else 120)]:
            L.append(page_line(p))
        L.append("")
        if aux:
            L.append("### Test, report and other non-app pages (%d)" % len(aux))
            L.append("")
            for p in aux[: (None if full else 60)]:
                L.append(page_line(p))
            L.append("")
    L.append("## Ask the map")
    L.append("")
    L.append("```")
    L.append(GUIDE.split("COMMANDS\n", 1)[1].split("\nOUTPUT LOCATION", 1)[0].rstrip())
    L.append("```")
    return "\n".join(L) + "\n"


def cmd_report(ctx, a):
    _ensure_outdir(ctx.outdir, ctx.root)
    p = os.path.join(ctx.outdir, "REPOFREE.md")
    _atomic_write(p, build_report(ctx))
    return {"report": p}, ["Wrote %s" % p]


def cmd_export(ctx, a):
    I = ctx.I
    parts = [build_report(ctx, full=True)]
    parts.append("\n## Definitions by file\n")
    for f in sorted(I.files):
        defs = I.files[f].get("defs", [])
        if not defs:
            continue
        parts.append("### `%s`" % f)
        parts.append(", ".join("%s:%d" % (d[1], d[3]) for d in defs))
        parts.append("")
    parts.append("\n## Endpoints and their callers\n")
    data, txt = cmd_endpoints(ctx, argparse.Namespace(text=None, unmatched=False, limit=0))
    parts.append("```")
    parts.extend(txt)
    parts.append("```")
    parts.append("\n## Collection sites\n")
    for f, fa in sorted(I.facts_by_kind.get("collection", []), key=lambda x: (x[1][1], x[0], x[1][2])):
        ex = fa[3] or {}
        parts.append("- `%s` %s `%s:%d` in %s" % (fa[1], _mode(ex.get("ops", [])), f, fa[2], I.enc_name(f, fa[4])))
    out = "\n".join(parts) + "\n"
    limit = a.max_kb * 1024 if a.max_kb else 0
    truncated = False
    if limit and len(out.encode("utf-8")) > limit:
        out = out.encode("utf-8")[:limit].decode("utf-8", "ignore") + "\n\n(truncated at %d KB; use --max-kb 0 for all)\n" % a.max_kb
        truncated = True
    path = a.output or os.path.join(ctx.outdir, "REPOFREE-export.md")
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if os.path.abspath(path).startswith(os.path.abspath(ctx.outdir)):
        _ensure_outdir(ctx.outdir, ctx.root)
    _atomic_write(os.path.abspath(path), out)
    kb = len(out.encode("utf-8")) // 1024
    return {"export": os.path.abspath(path), "kb": kb, "truncated": truncated}, [
        "Wrote %s (%d KB%s)." % (os.path.abspath(path), kb, ", truncated" if truncated else ""),
        "Upload it to a chat assistant (ChatGPT, Claude.ai, Gemini) as context for questions about this repo."]


def cmd_build(ctx, a):
    return cmd_status(ctx, a)


def cmd_agent_help(ctx, a):
    return {"guide": GUIDE}, GUIDE.rstrip("\n").split("\n")


# --------------------------------------------------------------------------
# docs: per-function documentation + interactive graph (static, offline)
# --------------------------------------------------------------------------

DOC_KINDS = {"function", "method", "class", "export"}
_LIB_RECEIVERS = set("""console Math JSON Object Array Promise Number String Boolean Date RegExp Error Map Set
navigator location history localStorage sessionStorage document res req response Intl Reflect Symbol URL URLSearchParams
Buffer process fs path os crypto util""".split())
_COMMON_METHODS = set("""get set add update delete remove push pop shift unshift map filter forEach reduce some every
then catch finally find findIndex includes indexOf join split replace trim slice splice concat sort reverse keys
values entries has toString send json status on off emit once open close write read end""".split())
_LINE_PREFIX = {"py": ("#",), "rb": ("#",), "ruby": ("#",), "php": ("//", "#")}
_DECORATOR_RE = re.compile(r"^\s*(?:@[\w$.]+|#\[|\[[A-Z]\w*(?:\(|\]))")
_TOOLING_RE = re.compile(
    r"^\s*(?:eslint[- ]|@ts-|prettier-ignore|istanbul\s|c8\s|jshint|jscs:|globals?\s+[\w$]|exported\s|"
    r"noinspection|#?region\b|#?endregion\b|pragma\b|type:\s*ignore|noqa|nolint|@flow\b|tslint:|"
    r"webpackChunkName|@vite-ignore|<reference\s|@jsx\s|@vitest|@jest-environment|-\*-|coding[:=]|"
    r"pylint:|mypy:|fmt:\s*(?:off|on))", re.I)
_SEPARATOR_RE = re.compile(r"^[\s=\-*#~_+/<>|.:\u2500-\u257f\u2580-\u259f]*$")
_SECTION_RE = re.compile(r"^\s*(?:[=\-*#~_+]{3,}|[\u2500-\u257f]{2,})|(?:[=\-*#~_+]{3,}|[\u2500-\u257f]{2,})\s*$")
_CODEISH_RE = re.compile(
    r"(?:^\s*(?:const|let|var)\s+[\w$]+\s*=|^\s*(?:if|for|while|switch)\s*\(|^\s*\}|"
    r"^\s*(?:await\s+)?[\w$.]+\([^)]*\)\s*;?\s*$|[;{]\s*$|^\s*return\b.*;\s*$|^\s*(?:import|export)\s+\S.*[;'\"]\s*$)")
_PARAM_TAG_RE = re.compile(r"^@(?:param|arg|argument|prop|property)\s+(?:\{([^}]*)\}\s*)?(\[[^\]]*\]|[\w$.]+)\s*(?:-\s*)?(.*)$", re.S)
_RETURN_TAG_RE = re.compile(r"^@returns?\s+(?:\{([^}]*)\}\s*)?(.*)$", re.S)
_SIG_START_RE = re.compile(
    r"\s*(?:[?!])?(?::\s*[^=(\n]{0,80}?)?(?:=\s*)?(?:async\s+)?(?:function\s*\*?\s*[\w$]*\s*)?(?:<[^>\n]{0,80}>)?\s*\(")
_SIG_ARROW1_RE = re.compile(r"\s*(?::\s*[^=\n]{0,80}?)?=\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*=>")


def _clean_comment_lines(raw_lines):
    """Comment body lines -> text lines, dropping tooling directives, separators and commented-out code."""
    out = []
    for ln in raw_lines:
        t = ln.rstrip()
        if _TOOLING_RE.match(t):
            continue
        if _SEPARATOR_RE.match(t):
            out.append("")
            continue
        if _CODEISH_RE.search(t):
            continue
        out.append(t)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    # common indentation
    ind = min((len(x) - len(x.lstrip()) for x in out if x.strip()), default=0)
    return [x[ind:] for x in out]


def _parse_doc_block(lines, src):
    """Comment text lines -> {"text", "params", "returns", "tags", "src"} or None."""
    lines = _clean_comment_lines(lines)
    if not any(x.strip() for x in lines):
        return None
    desc, tags = [], []
    for ln in lines:
        s = ln.strip()
        if s.startswith("@") and re.match(r"@[A-Za-z]", s):
            tags.append(s)
        elif tags:
            tags[-1] += "\n" + s
        else:
            desc.append(ln)
    params, returns, other = [], "", []
    for t in tags:
        m = _PARAM_TAG_RE.match(t)
        if m:
            params.append([m.group(2).strip("[]").split("=")[0], (m.group(1) or "").strip(), " ".join(m.group(3).split())])
            continue
        m = _RETURN_TAG_RE.match(t)
        if m:
            returns = ((("{" + m.group(1) + "} ") if m.group(1) else "") + " ".join(m.group(2).split())).strip()
            continue
        tag, _, rest = t.partition(" ")
        other.append([tag[1:], " ".join(rest.split())])
    text = "\n".join(desc).strip()
    if not text and not params and not returns and not other:
        return None
    doc = {"text": text[:3000], "src": src}
    if params:
        doc["params"] = params[:30]
    if returns:
        doc["returns"] = returns[:300]
    if other:
        doc["tags"] = other[:20]
    return doc


def _comment_above(lines, line_no, lang):
    """Doc comment that sits directly above line `line_no` (1-based), or None."""
    i = line_no - 2
    while i >= 0 and _DECORATOR_RE.match(lines[i]) and not lines[i].lstrip().startswith(("//", "/*")):
        i -= 1
    if i >= 1 and not lines[i].strip() and lines[i - 1].strip():
        i -= 1  # allow one blank line between the comment and the definition
    if i < 0:
        return None
    s = lines[i].strip()
    if lang not in ("py", "rb", "ruby") and s.endswith("*/"):
        j = i
        while j >= 0 and "/*" not in lines[j]:
            j -= 1
        if j < 0 or i - j > 400 or not lines[j].lstrip().startswith("/*"):
            return None
        block = lines[j:i + 1]
        is_jsdoc = block[0].lstrip().startswith("/**")
        body = []
        for k, ln in enumerate(block):
            t = ln
            if k == 0:
                t = t.split("/*", 1)[1].lstrip("*!")
            if k == len(block) - 1:
                t = t.rsplit("*/", 1)[0]
            t2 = t.lstrip()
            if k > 0 and t2.startswith("*"):
                t = t2[1:]
                if t.startswith(" "):
                    t = t[1:]
            body.append(t)
        if len(block) == 1 and _SECTION_RE.search(body[0]):
            return None
        return _parse_doc_block(body, "jsdoc" if is_jsdoc else "comment")
    prefixes = _LINE_PREFIX.get(lang, ("//",))
    body = []
    while i >= 0:
        s = lines[i].strip()
        p = next((p for p in prefixes if s.startswith(p)), None)
        if not p or s.startswith("#!") or (p == "#" and lang not in ("py", "rb", "ruby", "php")):
            break
        t = s[len(p):].lstrip("/!")
        if _SECTION_RE.search(t):
            break  # a section header belongs to the code below it, not to this function
        body.append(t[1:] if t.startswith(" ") else t)
        i -= 1
    if not body:
        return None
    body.reverse()
    return _parse_doc_block(body, "comment")


def _py_docstrings(text):
    out = {}
    try:
        tree = ast.parse(text)
    except Exception:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ds = ast.get_docstring(node)
            if ds:
                out[node.lineno] = ds
    return out


def _signature(lines, d, lang):
    """Short signature text for a definition, e.g. 'name(a, b = 1)'."""
    name, kind, line = d[0], d[2], d[3]
    if kind == "handler" or line < 1 or line > len(lines):
        return ""
    chunk = "\n".join(lines[line - 1:line + 12])
    if kind == "class":
        first = lines[line - 1].strip()
        return re.split(r"\s*[{:]\s*$", first)[0][:140]
    pos = -1
    for m in re.finditer(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", chunk):
        pos = m.end()
        break
    if pos < 0:
        return ""
    rest = chunk[pos:pos + 800]
    m = _SIG_START_RE.match(rest)
    if m:
        k = m.end() - 1
        depth = 0
        for q in range(k, len(rest)):
            ch = rest[q]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    params = " ".join(rest[k + 1:q].split())
                    return ("%s(%s)" % (name, params))[:200]
        return ""
    m = _SIG_ARROW1_RE.match(rest)
    if m:
        return "%s(%s)" % (name, m.group(1))
    return ""


def _is_documentable(d):
    return d[2] in DOC_KINDS or (d[2] == "handler" and not d[0].startswith("on "))


def resolved_calls(I):
    """Every call site with the definition(s) it most likely means:
    [(file, call, [(def_file, def_index), ...], maybe)]. An empty list = library/builtin/unknown.
    Shared by `docs` and the report; cached on the index."""
    cached = getattr(I, "_resolved", None)
    if cached is not None:
        return cached
    def is_nested(x):
        d = I.d(*x)
        par = d[5]
        return d[2] == "function" and par is not None and par >= 0 and I.d(x[0], par)[2] != "class"

    def chain(f, i):
        out = []
        defs = I.files[f]["defs"]
        while i is not None and i >= 0 and len(out) < 50:
            out.append(i)
            i = defs[i][5]
        return out

    def resolve(f, name, has_recv, enc):
        """Definitions a call in file f most likely means. A definition only counts when f can see it
        (same file, imports it, or shares a page with it); otherwise a lone, receiver-less match is a 'maybe'.
        Inside one file, a helper defined inside another function is only visible within that function."""
        cands = [x for x in I.defs_by_name.get(name, []) if I.family(x[0]) == I.family(f)]
        # a helper defined inside another function can't be called from a different file
        cands = [x for x in cands if x[0] == f or not is_nested(x)]
        # app code never calls helpers that live in test files
        if not _is_test_path(f):
            cands = [x for x in cands if not _is_test_path(x[0])]
        if not cands:
            return [], False
        local = [x for x in cands if x[0] == f]
        if local:
            anc = chain(f, enc)
            scoped, open_ = [], []
            for x in local:
                d = I.d(*x)
                par = d[5]
                nested = d[2] == "function" and par is not None and par >= 0 and I.d(f, par)[2] != "class"
                if not nested:
                    open_.append(x)
                elif par in anc:
                    scoped.append((anc.index(par), x))
            if scoped:
                return [min(scoped)[1]], False
            if open_:
                return open_, len(open_) > 1
            return [], False
        vis = [x for x in cands if f in I.reach(x[0])]
        if vis:
            return vis, len(vis) > 1
        if not has_recv and len({x[0] for x in cands}) == 1:
            return cands[:2], True
        return [], False

    def library_like(c):
        recv = c[2]
        if not recv or recv in ("@ref", "@html", "this", "self", "cls"):
            return False
        if recv in _LIB_RECEIVERS:
            return True
        return c[0] in _COMMON_METHODS and all(I.d(df, di)[2] == "function" for df, di in I.defs_by_name.get(c[0], []))

    out = []
    res_cache = {}
    for f in sorted(I.files):
        for c in I.files[f].get("calls", []):
            name = c[0]
            if name not in I.defs_by_name or library_like(c):
                out.append((f, c, [], False))
                continue
            has_recv = bool(c[2]) and c[2] not in ("@ref", "@html", "this", "self", "cls")
            key = (f, name, has_recv, c[3])
            if key not in res_cache:
                res_cache[key] = ([], False) if (has_recv and name in _COMMON_METHODS) else resolve(f, name, has_recv, c[3])
            targets, maybe = res_cache[key]
            out.append((f, c, targets, maybe))
    I._resolved = out
    return out


def build_docs_data(ctx):
    """Everything the docs viewer needs, as one JSON-able dict with compact arrays."""
    I = ctx.I
    files = sorted(I.files)
    fidx = {f: k for k, f in enumerate(files)}
    fn_id = {}
    fns = []
    for f in files:
        for i, d in enumerate(I.files[f].get("defs", [])):
            fn_id[(f, i)] = len(fns)
            fns.append({"n": d[0], "q": d[1], "k": d[2], "f": fidx[f], "l": d[3], "e": d[4]})
    for f in files:
        for i, d in enumerate(I.files[f].get("defs", [])):
            me = fns[fn_id[(f, i)]]
            if d[5] is not None and d[5] >= 0 and (f, d[5]) in fn_id:
                me["p"] = fn_id[(f, d[5])]
                fns[me["p"]].setdefault("kids", []).append(fn_id[(f, i)])

    # comments + signatures straight from the files on disk
    for f in files:
        rec = I.files[f]
        defs = rec.get("defs", [])
        if not defs:
            continue
        try:
            with open(os.path.join(ctx.root, f), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        lines = text.split("\n")
        lang = rec.get("lang")
        pyds = _py_docstrings(text) if lang == "py" else {}
        for i, d in enumerate(defs):
            me = fns[fn_id[(f, i)]]
            doc = None
            if d[3] in pyds:
                doc = _parse_doc_block(pyds[d[3]].split("\n"), "docstring")
            if doc is None and 0 < d[3] <= len(lines):
                doc = _comment_above(lines, d[3], lang)
            if doc:
                me["doc"] = doc
            sig = _signature(lines, d, lang)
            if sig:
                me["sig"] = sig

    # calls -> resolved definitions
    file_calls = defaultdict(set)
    for f, c, targets, maybe in resolved_calls(I):
        name = c[0]
        if not targets:
            if c[3] is not None and c[3] >= 0 and c[2] not in ("@ref", "@html"):
                fns[fn_id[(f, c[3])]].setdefault("_ext", set()).add(((c[2] + ".") if c[2] else "") + name)
            continue
        via = "ref" if c[2] == "@ref" else ("html" if c[2] == "@html" else "")
        caller = fn_id.get((f, c[3])) if c[3] is not None and c[3] >= 0 else None
        for tf, ti in targets:
            tid = fn_id[(tf, ti)]
            if caller == tid:
                continue
            fns[tid].setdefault("callers", []).append([caller if caller is not None else -1, fidx[f], c[1], via, 1 if maybe else 0])
            if caller is not None:
                fns[caller].setdefault("_callees", {}).setdefault(tid, maybe)
            else:
                file_calls[f].add(tid)
    for fn in fns:
        if "_callees" in fn:
            fn["callees"] = [[t, 1 if m else 0] for t, m in sorted(fn.pop("_callees").items())]
        if "_ext" in fn:
            fn["ext"] = sorted(fn.pop("_ext"))[:60]

    # facts -> per function (or per file for top-level code)
    colls = defaultdict(list)
    events = defaultdict(lambda: {"emit": [], "listen": []})
    storage = defaultdict(list)
    env = defaultdict(list)
    globs = defaultdict(list)
    file_facts = defaultdict(list)
    for f in files:
        for fa in I.files[f].get("facts", []):
            kind = fa[0]
            if kind in ("pathstr", "load", "handler"):
                continue
            owner = fn_id.get((f, fa[4])) if fa[4] is not None and fa[4] >= 0 else None
            ex = fa[3] or {}
            site = [owner if owner is not None else -1, fidx[f], fa[2]]
            if kind == "collection":
                mode = _mode(ex.get("ops", []))
                colls[fa[1]].append(site + [mode, ",".join(ex.get("flags", []))])
                item = ["collection", fa[1], fa[2], mode]
            elif kind in ("emit", "listen"):
                if kind == "listen" and fa[1] in _DOM_EVENTS:
                    continue
                events[fa[1]][kind].append(site)
                item = [kind, fa[1], fa[2], ""]
            elif kind == "storage":
                storage[fa[1]].append(site + [ex.get("op", "")])
                item = ["storage", fa[1], fa[2], ex.get("op", "")]
            elif kind == "env":
                env[fa[1]].append(site)
                item = ["env", fa[1], fa[2], ""]
            elif kind == "global":
                globs[fa[1]].append(site)
                item = ["global", fa[1], fa[2], ""]
            elif kind == "http":
                item = ["http", fa[1], fa[2], ex.get("method", "")]
            elif kind == "cloudfn":
                item = ["cloudfn", fa[1], fa[2], ex.get("trigger", "")]
            elif kind == "route":
                if ex.get("method") == "USE":
                    continue
                item = ["route", fa[1], fa[2], ex.get("method", "")]
            else:
                continue
            if owner is not None:
                fns[owner].setdefault("facts", []).append(item)
            else:
                file_facts[f].append(item)
    for name in list(events):
        if name in _DOM_EVENTS and not events[name]["emit"]:
            del events[name]

    # routes
    exports, _m = I.services()
    routes = []
    for f, fa, cl in _endpoint_pairs(I):
        hname, hi = _route_handler(I, f, fa)
        r = {"m": (fa[3] or {}).get("method", "ANY"), "path": fa[1], "f": fidx[f], "l": fa[2],
             "svc": sorted(exports.get(f, [])), "h": fn_id.get((f, hi), -1) if hi is not None else -1,
             "cl": [[fn_id.get((cf, cfa[4]), -1) if cfa[4] is not None and cfa[4] >= 0 else -1, fidx[cf], cfa[2], cfa[1]]
                    for cf, cfa in cl]}
        routes.append(r)

    # files
    imp = I.importers()
    file_rows = []
    for f in files:
        rec = I.files[f]
        loads = []
        for src, line, spec, v, kind in imp.get(f, []):
            if src in fidx:
                loads.append([fidx[src], line, spec, v or "", kind])
        row = {"p": f, "lang": rec.get("lang"), "lines": rec.get("lines", 0),
               "page": rec.get("lang") == "html", "by": sorted(loads)}
        if file_facts.get(f):
            row["facts"] = file_facts[f]
        if file_calls.get(f):
            row["calls"] = sorted(file_calls[f])
        if row["page"] or rec.get("lang") in ("js",):
            pg = I.pages_loading(f)
            if pg:
                row["pages"] = [fidx[x] for x in pg if x in fidx]
        file_rows.append(row)
    for k, f in enumerate(files):
        for src, line, spec, v, kind in imp.get(f, []):
            if src in fidx:
                file_rows[fidx[src]].setdefault("uses", []).append([k, line, kind])

    documentable = [fn for fn in fns if _is_documentable([fn["n"], fn["q"], fn["k"]])]
    with_doc = sum(1 for fn in documentable if fn.get("doc"))
    meta = dict(ctx.repomap.get("meta", {}))
    meta.update({"repo": os.path.basename(ctx.root.rstrip("/\\")) or ctx.root, "root": ctx.root,
                 "generated": _now_iso(), "version": __version__,
                 "documentable": len(documentable), "documented": with_doc})
    return {"meta": meta, "files": file_rows, "fns": fns, "routes": routes,
            "colls": dict(colls), "events": dict(events), "storage": dict(storage), "env": dict(env),
            "globals": dict(globs)}


_TEST_PATH_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|specs|e2e|cypress|playwright|fixtures?)(?:/|$)|[._-](?:test|spec|e2e)\.[a-z]+$|(?:^|/)test_[^/]*$", re.I)


def _is_test_path(p):
    return bool(_TEST_PATH_RE.search(p))


_AUX_DIR_RE = re.compile(r"(?:^|/)[^/]*(?:reports?|docs?|examples?|samples?|demo|mockups?|preview)(?:/|$)", re.I)


def _is_aux_path(p):
    """Test files, generated reports, docs and examples - not part of the running app."""
    return _is_test_path(p) or bool(_AUX_DIR_RE.search(p))


def _missing_rows(data, prefix=None):
    files = data["files"]
    rows = []
    for k, fn in enumerate(data["fns"]):
        if fn.get("doc") or not _is_documentable([fn["n"], fn["q"], fn["k"]]):
            continue
        path = files[fn["f"]]["p"]
        if prefix and not path.startswith(prefix.lstrip("./")):
            continue
        callers = fn.get("callers", [])
        pages = files[fn["f"]].get("pages", [])
        rows.append({"id": k, "name": fn["q"], "kind": fn["k"], "file": path, "line": fn["l"],
                     "callers": len(callers), "caller_files": len({c[1] for c in callers}), "pages": len(pages),
                     "test": _is_test_path(path)})
    rows.sort(key=lambda r: (r["test"], -r["callers"], -r["pages"], r["file"], r["line"]))
    return rows


def _missing_md(data, rows):
    m = data["meta"]
    out = ["# Functions with no comment",
           "",
           "Repository: `%s` · generated %s by repofree %s" % (m["repo"], m["generated"], m["version"]),
           "",
           "%d of %d functions, methods, classes and route handlers have a comment (%d missing)." % (
               m["documented"], m["documentable"], m["documentable"] - m["documented"]),
           "Add a comment directly above each one (JSDoc `/** ... */`, `//` lines, or a Python docstring),",
           "then run `repofree docs` again. Most-used first, so the top of the list helps the docs the most.",
           "",
           "Test files are listed last.",
           "",
           "## Most used", ""]
    for r in [r for r in rows if not r["test"]][:50]:
        out.append("- [ ] `%s` (%s) `%s:%d` · %d caller(s)%s" % (
            r["name"], r["kind"], r["file"], r["line"], r["callers"],
            (", on %d page(s)" % r["pages"]) if r["pages"] else ""))
    out += ["", "## By file", ""]
    by_file = defaultdict(list)
    for r in rows:
        by_file[r["file"]].append(r)
    tests_hdr = False
    for f in sorted(by_file, key=lambda x: (_is_test_path(x), -len(by_file[x]), x)):
        if _is_test_path(f) and not tests_hdr:
            out += ["## Test files", ""]
            tests_hdr = True
        out.append("### `%s` (%d)" % (f, len(by_file[f])))
        out.append("")
        for r in sorted(by_file[f], key=lambda r: r["line"]):
            out.append("- [ ] :%d `%s` · %d caller(s)" % (r["line"], r["name"], r["callers"]))
        out.append("")
    return "\n".join(out) + "\n"


def cmd_docs(ctx, a):
    data = build_docs_data(ctx)
    if a.output:
        out = os.path.abspath(os.path.expanduser(a.output))
    else:
        _ensure_outdir(ctx.outdir, ctx.root)
        out = os.path.join(ctx.outdir, "docs")
    os.makedirs(out, exist_ok=True)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=lambda o: sorted(o) if isinstance(o, set) else str(o))
    blob = blob.replace("</", "<\\/")
    _atomic_write(os.path.join(out, "data.js"), "window.REPOFREE_DOCS=" + blob + ";\n")
    title = "%s docs" % data["meta"]["repo"]
    for name, start in (("index.html", "home"), ("graph.html", "graph")):
        html = DOCS_HTML.replace("__TITLE__", _html_escape(title)).replace("__START__", start)
        _atomic_write(os.path.join(out, name), html)
    rows = _missing_rows(data)
    _atomic_write(os.path.join(out, "missing-comments.md"), _missing_md(data, rows))
    m = data["meta"]
    shown_rows = _missing_rows(data, a.missing) if a.missing else rows
    text = ["Docs written to %s (open index.html)" % out,
            "%d functions · %d have comments · %d missing" % (m["documentable"], m["documented"], m["documentable"] - m["documented"]),
            ""]
    if shown_rows:
        text.append("Missing comments%s (most-used first):" % ((" under " + a.missing) if a.missing else ""))
        lim = a.limit if a.missing else min(a.limit or 15, 15)
        shown, extra = _limit_rows(shown_rows, lim)
        for r in shown:
            loc = "%s:%d" % (r["file"], r["line"])
            text.append("  %-48s %-30s (%d caller%s%s)" % (loc, r["name"][:30], r["callers"], "" if r["callers"] == 1 else "s",
                                                       (", %d page%s" % (r["pages"], "" if r["pages"] == 1 else "s")) if r["pages"] else ""))
        _more(text, extra)
        text.append("Full list: %s" % os.path.join(out, "missing-comments.md"))
        if not a.missing:
            text.append("One folder only: repofree docs --missing path/to/folder/")
    else:
        text.append("Every function%s has a comment." % ((" under " + a.missing) if a.missing else ""))
    text.append("Docs show what the code says about itself; links come from the map (pattern-based, dynamic calls not seen).")
    return {"docs": out, "index": os.path.join(out, "index.html"), "graph": os.path.join(out, "graph.html"),
            "documentable": m["documentable"], "documented": m["documented"], "missing": shown_rows}, text


def _html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


DOCS_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#f7f7f5;--panel:#ffffff;--ink:#1c1c1e;--muted:#6a6a70;--faint:#9a9aa0;--line:#e3e3e0;--hover:#f0f0ec;
  --accent:#2457d6;--accent-soft:#e8eefc;--warn-bg:#fff6e5;--warn-ink:#7a4b00;--warn-line:#f0d8a8;
  --ok:#1f7a45;--ok-soft:#e5f4ea;--bad:#b42318;--bad-soft:#fdecea;--code:#f2f2ee;
  --n-fn:#2457d6;--n-file:#6a6a70;--n-page:#0f7b6c;--n-route:#c2410c;--n-coll:#15803d;--n-event:#7c3aed;--n-store:#0e7490;
  --e-calls:#8aa4e6;--e-struct:#c9c9c4;--e-load:#7fb8ad;--e-http:#eaa077;--e-read:#7cc196;--e-write:#e58a7f;--e-event:#b79cf0;--e-store:#7ab9c9;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#141416;--panel:#1c1c1f;--ink:#ececee;--muted:#a1a1a8;--faint:#6f6f76;--line:#2e2e33;--hover:#25252a;
  --accent:#7aa2ff;--accent-soft:#1e2740;--warn-bg:#2b2213;--warn-ink:#f2c879;--warn-line:#4a3a1a;
  --ok:#5fd08f;--ok-soft:#14291d;--bad:#ff8a80;--bad-soft:#331a18;--code:#26262b;
  --n-fn:#7aa2ff;--n-file:#a1a1a8;--n-page:#3fc6b0;--n-route:#fb923c;--n-coll:#4ade80;--n-event:#b794ff;--n-store:#38bdf8;
  --e-calls:#3d5590;--e-struct:#3a3a40;--e-load:#2f6a60;--e-http:#8a4a24;--e-read:#2c6b44;--e-write:#8a3a33;--e-event:#56408f;--e-store:#2a5f6e;
}}
:root[data-theme="dark"]{
  --bg:#141416;--panel:#1c1c1f;--ink:#ececee;--muted:#a1a1a8;--faint:#6f6f76;--line:#2e2e33;--hover:#25252a;
  --accent:#7aa2ff;--accent-soft:#1e2740;--warn-bg:#2b2213;--warn-ink:#f2c879;--warn-line:#4a3a1a;
  --ok:#5fd08f;--ok-soft:#14291d;--bad:#ff8a80;--bad-soft:#331a18;--code:#26262b;
  --n-fn:#7aa2ff;--n-file:#a1a1a8;--n-page:#3fc6b0;--n-route:#fb923c;--n-coll:#4ade80;--n-event:#b794ff;--n-store:#38bdf8;
  --e-calls:#3d5590;--e-struct:#3a3a40;--e-load:#2f6a60;--e-http:#8a4a24;--e-read:#2c6b44;--e-write:#8a3a33;--e-event:#56408f;--e-store:#2a5f6e;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
code{background:var(--code);padding:1px 5px;border-radius:4px}
header{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line)}
.bar{display:flex;align-items:center;gap:14px;padding:10px 16px;flex-wrap:wrap}
.brand{font-weight:650;white-space:nowrap}
.brand small{font-weight:400;color:var(--muted);margin-left:6px}
.search{position:relative;flex:1;min-width:200px;max-width:560px}
.search input{width:100%;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
.search input:focus{outline:2px solid var(--accent-soft);border-color:var(--accent)}
.results{position:absolute;top:100%;left:0;right:0;margin-top:4px;background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.12);max-height:60vh;overflow:auto;display:none}
.results a{display:flex;gap:8px;align-items:baseline;padding:6px 10px;color:var(--ink);border-bottom:1px solid var(--line)}
.results a:last-child{border-bottom:0}
.results a.on,.results a:hover{background:var(--hover);text-decoration:none}
.results .sub{color:var(--muted);font-size:12px;margin-left:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:50%}
nav{display:flex;gap:2px;padding:0 12px;overflow-x:auto}
nav a{padding:8px 10px;color:var(--muted);border-bottom:2px solid transparent;white-space:nowrap}
nav a.on{color:var(--ink);border-bottom-color:var(--accent)}
nav a:hover{text-decoration:none;color:var(--ink)}
.ed-pick{color:var(--muted);font-size:12px;white-space:nowrap}
.ed-pick select{font:inherit;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:2px 4px}
main{max-width:1100px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 4px;word-break:break-word}
h2{font-size:15px;margin:26px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--line)}
h2 .count{color:var(--muted);font-weight:400}
.sub-h{color:var(--muted);margin-bottom:12px}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;background:var(--hover);color:var(--muted);vertical-align:middle;margin-left:4px;font-weight:500}
.badge.ok{background:var(--ok-soft);color:var(--ok)}
.badge.warn{background:var(--warn-bg);color:var(--warn-ink)}
.badge.maybe{background:var(--warn-bg);color:var(--warn-ink)}
.badge.w{background:var(--bad-soft);color:var(--bad)}
.badge.r{background:var(--ok-soft);color:var(--ok)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.doc p{margin:0 0 8px}
.doc p:last-child{margin-bottom:0}
.missing{background:var(--warn-bg);border:1px solid var(--warn-line);color:var(--warn-ink);border-radius:10px;padding:12px 14px}
.sig{display:block;background:var(--code);padding:8px 12px;border-radius:8px;margin:10px 0;overflow-x:auto;white-space:pre-wrap;word-break:break-word}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:500}
tr:hover td{background:var(--hover)}
td.num,th.num{text-align:right;white-space:nowrap}
.rows .grp{margin:10px 0 2px;font-weight:550}
.rows .row{padding:3px 0 3px 14px;display:flex;gap:10px;flex-wrap:wrap;align-items:baseline}
.ln{color:var(--muted)}
.loc{white-space:nowrap}
.ed{font-size:12px;opacity:.7}
.muted{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chips span{background:var(--code);border-radius:6px;padding:1px 7px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat b{display:block;font-size:20px}
.stat span{color:var(--muted);font-size:12px}
.meter{height:8px;background:var(--hover);border-radius:99px;overflow:hidden;min-width:60px}
.meter i{display:block;height:100%;background:var(--ok)}
.filter{padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit;width:100%;max-width:420px;margin:4px 0 10px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit;font-size:13px;cursor:pointer}
.btn:hover{background:var(--hover);text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.note{color:var(--muted);font-size:12.5px;margin-top:30px}
dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0}
dl.kv dt{color:var(--muted)}
dl.kv dd{margin:0;word-break:break-word}
/* graph */
#graphwrap{display:none;position:fixed;left:0;right:0;bottom:0;top:var(--hh,92px)}
#graphwrap.on{display:flex}
#gcanvas{flex:1;position:relative;overflow:hidden;touch-action:none;background:var(--bg)}
#gsvg{width:100%;height:100%;display:block;cursor:grab;user-select:none}
#gsvg.panning{cursor:grabbing}
#gpanel{width:340px;max-width:45vw;border-left:1px solid var(--line);background:var(--panel);overflow:auto;padding:14px}
.gtools{position:absolute;left:12px;top:12px;display:flex;gap:6px;flex-wrap:wrap;z-index:2}
.gsearch{position:relative;width:260px}
.gsearch input{width:100%;padding:5px 9px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit;font-size:13px}
.legend{position:absolute;left:12px;bottom:12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:12px;z-index:2}
.legend label{display:flex;align-items:center;gap:6px;cursor:pointer;line-height:1.9}
.legend .dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.gempty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
.gempty .card{pointer-events:auto;max-width:520px}
.node circle{stroke:var(--panel);stroke-width:2}
.node text{font-size:11px;fill:var(--ink);paint-order:stroke;stroke:var(--bg);stroke-width:3px;stroke-linejoin:round;pointer-events:none}
.node .ic{font-size:9px;font-weight:700;fill:#fff;stroke:none;text-anchor:middle;dominant-baseline:central}
.node{cursor:pointer}
.node.sel circle{stroke:var(--ink);stroke-width:3}
.node.pinned circle{stroke-dasharray:3 2}
.edge{stroke-width:1.4;fill:none}
.edge.maybe{stroke-dasharray:4 3}
svg.focus .node.dim{opacity:.25}
svg.focus .edge.dim{opacity:.12}
svg.focus .edge.hl{stroke-width:2.4}
.conn{margin:6px 0}
.conn .t{font-size:12px;color:var(--muted);margin:10px 0 2px;display:flex;justify-content:space-between;align-items:center}
.conn a.it{display:block;padding:2px 6px;border-radius:5px;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conn a.it:hover{background:var(--hover);text-decoration:none}
.conn a.it.in-g::before{content:"● ";color:var(--accent)}
.conn a.it.out-g::before{content:"+ ";color:var(--muted)}
.linkbtn{background:none;border:0;color:var(--accent);cursor:pointer;font:inherit;font-size:12px;padding:0}
@media (max-width:720px){
  #graphwrap.on{flex-direction:column}
  #gpanel{width:auto;max-width:none;height:40%;border-left:0;border-top:1px solid var(--line)}
  .gsearch{width:180px}
  .legend{display:none}
  .ed-pick{display:none}
  .loc{white-space:normal;overflow-wrap:anywhere}
  .search{min-width:0}
  main{overflow-wrap:anywhere}
  td{word-break:break-word}
  .results .sub{display:none}
}
</style>
</head>
<body>
<header id="hdr">
  <div class="bar">
    <div class="brand" id="brand"></div>
    <div class="search"><input id="q" type="search" placeholder="Search functions, files, routes, collections…  ( / )" autocomplete="off"><div class="results" id="qr"></div></div>
    <label class="ed-pick">Open in <select id="edsel"><option value="vscode">VS Code</option><option value="cursor">Cursor</option><option value="none">(no links)</option></select></label>
  </div>
  <nav id="nav">
    <a href="#/" data-v="home">Overview</a>
    <a href="#/list/functions" data-v="functions">Functions</a>
    <a href="#/list/pages" data-v="pages">Pages</a>
    <a href="#/list/files" data-v="files">Files</a>
    <a href="#/list/routes" data-v="routes">API routes</a>
    <a href="#/list/collections" data-v="collections">Collections</a>
    <a href="#/list/events" data-v="events">Events</a>
    <a href="#/list/storage" data-v="storage">Storage &amp; env</a>
    <a href="#/missing" data-v="missing">Missing comments</a>
    <a href="#/graph" data-v="graph">Graph</a>
  </nav>
</header>
<main id="main"></main>
<div id="graphwrap">
  <div id="gcanvas">
    <svg id="gsvg"><defs id="gdefs"></defs><g id="gview"><g id="gedges"></g><g id="gnodes"></g></g></svg>
    <div class="gtools">
      <div class="gsearch"><input id="gq" type="search" placeholder="Add to graph…" autocomplete="off"><div class="results" id="gqr"></div></div>
      <button class="btn" id="gfit" title="Fit everything in view">Fit</button>
      <button class="btn" id="gclear" title="Remove everything">Clear</button>
    </div>
    <div class="legend" id="glegend"></div>
    <div class="gempty" id="gempty"></div>
  </div>
  <aside id="gpanel"></aside>
</div>
<script src="data.js"></script>
<script>
(function(){
'use strict';
var D = window.REPOFREE_DOCS;
var main = document.getElementById('main');
if (!D) { main.innerHTML = '<div class="missing">data.js was not found next to this page. Run <code>repofree docs</code> again and open the new index.html.</div>'; return; }
var START = '__START__';
var F = D.files, FN = D.fns, R = D.routes;
function $(s){ return document.querySelector(s); }
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
var enc = encodeURIComponent;
function base(p){ var i = p.lastIndexOf('/'); return i < 0 ? p : p.slice(i + 1); }
function plural(n, w){ return n + ' ' + w + (n === 1 ? '' : 's'); }

// ---------- derived indexes
var routesByHandler = {}, routeByClient = {}, routesByFile = {}, fnsByFile = {};
R.forEach(function(r, i){
  if (r.h >= 0) (routesByHandler[r.h] = routesByHandler[r.h] || []).push(i);
  (routesByFile[r.f] = routesByFile[r.f] || []).push(i);
  r.cl.forEach(function(c){ var k = c[1] + ':' + c[2]; (routeByClient[k] = routeByClient[k] || []).push(i); });
});
FN.forEach(function(fn, i){ (fnsByFile[fn.f] = fnsByFile[fn.f] || []).push(i); });
function docable(fn){ return ['function','method','class','export'].indexOf(fn.k) >= 0 || (fn.k === 'handler' && fn.n.indexOf('on ') !== 0); }
function nCallers(i){ return (FN[i].callers || []).length; }
var TEST_RE = /(?:^|\/)(?:tests?|__tests__|spec|specs|e2e|cypress|playwright|fixtures?)(?:\/|$)|[._-](?:test|spec|e2e)\.[a-z]+$|(?:^|\/)test_[^\/]*$/i;
function isTest(fi){ return TEST_RE.test(F[fi].p) ? 1 : 0; }

// ---------- editor links
var editor = 'vscode';
try { editor = localStorage.getItem('repofree-editor') || 'vscode'; } catch (e) {}
var edsel = document.getElementById('edsel');
edsel.value = editor;
edsel.addEventListener('change', function(){ editor = edsel.value; try { localStorage.setItem('repofree-editor', editor); } catch (e) {} route(); });
function edHref(fi, line){
  if (editor === 'none' || !D.meta.root) return null;
  var root = D.meta.root.replace(/\\/g, '/');
  if (root[0] !== '/') root = '/' + root;
  return editor + '://file' + encodeURI(root + '/' + F[fi].p) + ':' + (line || 1);
}

// ---------- link helpers
var L = {
  fn: function(i, label){ return '<a href="#/fn/' + i + '">' + esc(label || FN[i].q) + '</a>'; },
  file: function(i, label){ return '<a href="#/file/' + i + '">' + esc(label || F[i].p) + '</a>'; },
  route: function(i){ return '<a href="#/route/' + i + '"><b>' + esc(R[i].m) + '</b> ' + esc(R[i].path) + '</a>'; },
  coll: function(n){ return '<a href="#/coll/' + enc(n) + '">' + esc(n) + '</a>'; },
  event: function(n){ return '<a href="#/event/' + enc(n) + '">' + esc(n) + '</a>'; },
  storage: function(n){ return '<a href="#/storage/' + enc(n) + '">' + esc(n) + '</a>'; },
  env: function(n){ return '<a href="#/env/' + enc(n) + '">' + esc(n) + '</a>'; },
  global: function(n){ return '<a href="#/global/' + enc(n) + '">window.' + esc(n) + '</a>'; }
};
function loc(fi, line, short){
  var h = edHref(fi, line);
  return '<span class="loc">' + L.file(fi, short ? base(F[fi].p) : null) + '<span class="ln">:' + line + '</span>' +
    (h ? ' <a class="ed" href="' + esc(h) + '" title="Open in editor">↗</a>' : '') + '</span>';
}
function owner(o, fi){ return o >= 0 ? L.fn(o) : '<span class="muted">top-level code in</span> ' + L.file(fi, base(F[fi].p)); }
function modeBadge(m){
  if (m === 'read') return '<span class="badge r">read</span>';
  if (m === 'write') return '<span class="badge w">write</span>';
  if (m === 'read+write') return '<span class="badge w">read+write</span>';
  return '<span class="badge">' + esc(m || 'ref') + '</span>';
}

// ---------- comment text -> html
function inline(s){
  return esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\{@link\s+([^}\s|]+)[^}]*\}/g, '<code>$1</code>');
}
function docHtml(text){
  var paras = String(text || '').split(/\n\s*\n/);
  return paras.map(function(p){
    var lines = p.split('\n'), out = '';
    lines.forEach(function(ln, k){
      var t = ln.trim();
      if (!t) return;
      if (k > 0 && /^([-*•]|\d+[.)])\s/.test(t)) out += '<br>';
      else if (k > 0) out += ' ';
      out += inline(t);
    });
    return '<p>' + out + '</p>';
  }).join('');
}
function firstLine(fn){
  if (!fn.doc || !fn.doc.text) return '';
  var t = fn.doc.text.split(/\n\s*\n/)[0].replace(/\s+/g, ' ').trim();
  var m = t.match(/^(.{20,}?[.!?])\s/);
  t = m ? m[1] : t;
  return t.length > 140 ? t.slice(0, 137) + '…' : t;
}

// ---------- search
var SEARCH = null;
function buildSearch(){
  SEARCH = [];
  FN.forEach(function(fn, i){ SEARCH.push({t: 'fn', k: fn.q.toLowerCase(), n: fn.n.toLowerCase(), i: i, w: nCallers(i)}); });
  F.forEach(function(f, i){ SEARCH.push({t: f.page ? 'page' : 'file', k: f.p.toLowerCase(), n: base(f.p).toLowerCase(), i: i, w: 5}); });
  R.forEach(function(r, i){ SEARCH.push({t: 'route', k: (r.m + ' ' + r.path).toLowerCase(), n: r.path.toLowerCase(), i: i, w: 5}); });
  Object.keys(D.colls).forEach(function(n){ SEARCH.push({t: 'coll', k: n.toLowerCase(), n: n.toLowerCase(), i: n, w: 8}); });
  Object.keys(D.events).forEach(function(n){ SEARCH.push({t: 'event', k: n.toLowerCase(), n: n.toLowerCase(), i: n, w: 4}); });
  Object.keys(D.storage).forEach(function(n){ SEARCH.push({t: 'storage', k: n.toLowerCase(), n: n.toLowerCase(), i: n, w: 2}); });
  Object.keys(D.env).forEach(function(n){ SEARCH.push({t: 'env', k: n.toLowerCase(), n: n.toLowerCase(), i: n, w: 1}); });
}
var TLABEL = {fn: 'function', page: 'page', file: 'file', route: 'route', coll: 'collection', event: 'event', storage: 'storage key', env: 'env var'};
function search(q, max){
  if (!SEARCH) buildSearch();
  q = q.trim().toLowerCase();
  if (!q) return [];
  var out = [];
  for (var k = 0; k < SEARCH.length; k++) {
    var s = SEARCH[k], score = 0;
    if (s.n === q || s.k === q) score = 100;
    else if (s.n.indexOf(q) === 0) score = 60;
    else if (s.k.indexOf(q) >= 0) score = 30;
    else continue;
    out.push([score + Math.min(Math.log(1 + s.w) * 3, 15), s]);
  }
  out.sort(function(a, b){ return b[0] - a[0]; });
  return out.slice(0, max || 30).map(function(x){ return x[1]; });
}
function hitHref(s){
  switch (s.t) {
    case 'fn': return '#/fn/' + s.i;
    case 'page': case 'file': return '#/file/' + s.i;
    case 'route': return '#/route/' + s.i;
    case 'coll': return '#/coll/' + enc(s.i);
    case 'event': return '#/event/' + enc(s.i);
    case 'storage': return '#/storage/' + enc(s.i);
    case 'env': return '#/env/' + enc(s.i);
  }
}
function hitLabel(s){
  switch (s.t) {
    case 'fn': return [FN[s.i].q, F[FN[s.i].f].p + ':' + FN[s.i].l];
    case 'page': case 'file': return [base(F[s.i].p), F[s.i].p];
    case 'route': return [R[s.i].m + ' ' + R[s.i].path, F[R[s.i].f].p];
    default: return [s.i, ''];
  }
}
function wireSearch(input, box, onPick){
  var hits = [], on = 0;
  function render(){
    if (!hits.length) { box.style.display = input.value.trim() ? 'block' : 'none'; box.innerHTML = input.value.trim() ? '<a class="muted">No matches</a>' : ''; return; }
    box.innerHTML = hits.map(function(s, k){
      var lb = hitLabel(s);
      return '<a href="' + hitHref(s) + '" data-k="' + k + '"' + (k === on ? ' class="on"' : '') + '><span class="badge">' + TLABEL[s.t] + '</span> ' + esc(lb[0]) + '<span class="sub">' + esc(lb[1]) + '</span></a>';
    }).join('');
    box.style.display = 'block';
  }
  input.addEventListener('input', function(){ hits = search(input.value, 30); on = 0; render(); });
  input.addEventListener('keydown', function(e){
    if (e.key === 'ArrowDown') { on = Math.min(on + 1, hits.length - 1); render(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { on = Math.max(on - 1, 0); render(); e.preventDefault(); }
    else if (e.key === 'Enter' && hits[on]) { pick(hits[on]); e.preventDefault(); }
    else if (e.key === 'Escape') { box.style.display = 'none'; input.blur(); }
  });
  box.addEventListener('click', function(e){
    var a = e.target.closest('a[data-k]');
    if (!a) return;
    e.preventDefault();
    pick(hits[+a.getAttribute('data-k')]);
  });
  input.addEventListener('blur', function(){ setTimeout(function(){ box.style.display = 'none'; }, 150); });
  input.addEventListener('focus', function(){ if (input.value.trim()) render(); });
  function pick(s){ box.style.display = 'none'; input.blur(); onPick(s); }
}
wireSearch(document.getElementById('q'), document.getElementById('qr'), function(s){ location.hash = hitHref(s); });
document.addEventListener('keydown', function(e){
  if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); document.getElementById('q').focus(); }
});

// ---------- generic filtered list
function listBlock(id, items, rowHtml, keyOf, head, opts){
  opts = opts || {};
  var html = (opts.noFilter ? '' : '<input class="filter" id="' + id + '-f" placeholder="Filter…">') +
    '<table><thead>' + head + '</thead><tbody id="' + id + '-b"></tbody></table><div id="' + id + '-m" class="actions"></div>';
  setTimeout(function(){
    var body = document.getElementById(id + '-b'), more = document.getElementById(id + '-m'), inp = document.getElementById(id + '-f');
    if (!body) return;
    var cap = 300;
    function draw(){
      var q = inp ? inp.value.trim().toLowerCase() : '';
      var rows = q ? items.filter(function(x){ return keyOf(x).toLowerCase().indexOf(q) >= 0; }) : items;
      body.innerHTML = rows.slice(0, cap).map(rowHtml).join('') || '<tr><td colspan="9" class="muted">Nothing here.</td></tr>';
      more.innerHTML = rows.length > cap ? '<button class="btn">Show all ' + rows.length + '</button>' : (rows.length > 20 ? '<span class="muted">' + rows.length + ' rows</span>' : '');
      var b = more.querySelector('button');
      if (b) b.onclick = function(){ cap = 1e9; draw(); };
    }
    if (inp) inp.addEventListener('input', function(){ cap = 300; draw(); });
    draw();
  }, 0);
  return html;
}

// ---------- views
function vHome(){
  var m = D.meta, pct = m.documentable ? Math.round(100 * m.documented / m.documentable) : 100;
  var pages = F.filter(function(f){ return f.page; }).length;
  var folders = {};
  FN.forEach(function(fn){
    if (!docable(fn)) return;
    var p = F[fn.f].p.split('/'), k = p.length > 2 ? p[0] + '/' + p[1] + '/' : (p.length > 1 ? p[0] + '/' : '(root)');
    var o = folders[k] = folders[k] || {k: k, n: 0, d: 0};
    o.n++; if (fn.doc) o.d++;
  });
  var frows = Object.keys(folders).map(function(k){ return folders[k]; }).sort(function(a, b){ return b.n - a.n; });
  var top = FN.map(function(fn, i){ return i; }).filter(function(i){ return docable(FN[i]) && !isTest(FN[i].f); })
    .sort(function(a, b){ return nCallers(b) - nCallers(a); }).slice(0, 12);
  return '<h1>' + esc(m.repo) + '</h1><div class="sub-h">Generated ' + esc(m.generated) + (m.git_head ? ' · commit <code>' + esc(String(m.git_head).slice(0, 10)) + '</code>' : '') + ' · repofree ' + esc(m.version) + '</div>' +
    '<div class="grid">' +
      stat(m.documentable, 'functions & classes') + stat(F.length, 'source files') + stat(pages, 'pages') + stat(R.length, 'API routes') +
      stat(Object.keys(D.colls).length, 'collections') + stat(Object.keys(D.events).length, 'events') +
    '</div>' +
    '<h2>Comment coverage</h2>' +
    '<div class="card"><div style="display:flex;align-items:center;gap:12px"><div class="meter" style="flex:1"><i style="width:' + pct + '%"></i></div><b>' + pct + '%</b></div>' +
    '<div class="muted" style="margin-top:6px">' + m.documented + ' of ' + m.documentable + ' have a comment. Descriptions come only from comments in the code. ' +
    '<a href="#/missing">See the ' + (m.documentable - m.documented) + ' without one →</a></div></div>' +
    '<h2>By folder</h2><table><thead><tr><th>Folder</th><th class="num">Functions</th><th class="num">Commented</th><th style="width:30%"></th></tr></thead><tbody>' +
    frows.slice(0, 25).map(function(r){ var p = Math.round(100 * r.d / r.n); return '<tr><td><a href="#/missing/' + enc(r.k) + '">' + esc(r.k) + '</a></td><td class="num">' + r.n + '</td><td class="num">' + p + '%</td><td><div class="meter"><i style="width:' + p + '%"></i></div></td></tr>'; }).join('') +
    '</tbody></table>' +
    '<h2>Most used functions</h2><table><tbody>' + top.map(function(i){ var fn = FN[i]; return '<tr><td>' + L.fn(i) + (fn.doc ? '' : ' <span class="badge warn">no comment</span>') + '<div class="muted">' + esc(firstLine(fn)) + '</div></td><td class="num">' + nCallers(i) + ' callers</td><td>' + loc(fn.f, fn.l, true) + '</td></tr>'; }).join('') + '</tbody></table>' +
    '<p class="note">How to read these docs: descriptions are copied from the comments in the code. Links between functions, pages, routes and collections come from repofree\'s map, which is pattern-based: calls made through variables or computed names are not seen, so "no callers" means "none found", not "unused". A <span class="badge maybe">maybe</span> link means a name is defined in several places and the map could not tell which one is meant.</p>';
}
function stat(n, l){ return '<div class="stat"><b>' + n + '</b><span>' + l + '</span></div>'; }

function vFn(i){
  var fn = FN[i];
  if (!fn) return notFound();
  var f = F[fn.f], h = [];
  h.push('<h1>' + esc(fn.q) + ' <span class="badge">' + esc(fn.k) + '</span>' + (docable(fn) ? (fn.doc ? ' <span class="badge ok">commented</span>' : ' <span class="badge warn">no comment</span>') : '') + '</h1>');
  h.push('<div class="sub-h">' + loc(fn.f, fn.l) + (fn.e && fn.e !== fn.l ? ' <span class="muted">to line ' + fn.e + ' (' + (fn.e - fn.l + 1) + ' lines)</span>' : '') +
    (fn.p != null ? ' · inside ' + L.fn(fn.p) : '') + '</div>');
  if (fn.sig) h.push('<code class="sig">' + esc(fn.sig) + '</code>');
  h.push('<div class="actions"><a class="btn primary" href="#/graph/f' + i + '">Show in graph</a>' + (edHref(fn.f, fn.l) ? '<a class="btn" href="' + esc(edHref(fn.f, fn.l)) + '">Open in editor</a>' : '') + '</div>');
  h.push('<h2>Purpose</h2>');
  if (fn.doc) {
    var d = fn.doc, x = '<div class="card doc">' + (d.text ? docHtml(d.text) : '<p class="muted">(comment has tags only)</p>');
    if (d.params) x += '<h3 style="font-size:13px;margin:12px 0 4px">Parameters</h3><table><tbody>' + d.params.map(function(p){ return '<tr><td><code>' + esc(p[0]) + '</code></td><td class="muted">' + esc(p[1]) + '</td><td>' + inline(p[2]) + '</td></tr>'; }).join('') + '</tbody></table>';
    if (d.returns) x += '<p style="margin-top:10px"><b>Returns</b> ' + inline(d.returns) + '</p>';
    if (d.tags) x += '<p class="muted" style="margin-top:8px">' + d.tags.map(function(t){ return '<b>@' + esc(t[0]) + '</b> ' + inline(t[1]); }).join('<br>') + '</p>';
    x += '<div class="muted" style="font-size:12px;margin-top:10px">From the ' + (d.src === 'docstring' ? 'docstring' : (d.src === 'jsdoc' ? 'JSDoc comment' : 'comment')) + ' in the code.</div></div>';
    h.push(x);
  } else {
    h.push('<div class="missing"><b>No description.</b> Add a comment directly above this ' + (fn.k === 'class' ? 'class' : 'function') + ' at <code>' + esc(f.p) + ':' + fn.l + '</code>, then run <code>repofree docs</code> again.</div>');
  }
  var hr = routesByHandler[i] || [];
  if (hr.length) h.push('<h2>Handles API route' + (hr.length > 1 ? 's' : '') + '</h2>' + hr.map(function(ri){ return '<div>' + L.route(ri) + ' <span class="muted">· ' + plural(R[ri].cl.length, 'caller') + ' in the repo</span></div>'; }).join(''));
  // callers
  var cs = fn.callers || [];
  h.push('<h2>Called by <span class="count">(' + cs.length + ')</span></h2>');
  if (!cs.length) h.push('<div class="muted">No callers found by the map. It may be called dynamically, from outside this repo, or not at all. Check before removing it.</div>');
  else h.push(groupByFile(cs, function(c){
    return '<span class="ln">:' + c[2] + '</span> ' + owner(c[0], c[1]) + (c[3] === 'ref' ? ' <span class="badge">passed as callback</span>' : '') + (c[3] === 'html' ? ' <span class="badge">HTML handler</span>' : '') + (c[4] ? ' <span class="badge maybe">maybe</span>' : '');
  }));
  // callees
  var ce = fn.callees || [];
  h.push('<h2>Calls <span class="count">(' + ce.length + ' in this repo)</span></h2>');
  h.push(ce.length ? '<div class="rows">' + ce.map(function(c){ var t = FN[c[0]]; return '<div class="row">' + L.fn(c[0]) + (c[1] ? ' <span class="badge maybe">maybe</span>' : '') + ' <span class="muted">' + esc(firstLine(t)) + '</span> <span class="muted" style="margin-left:auto">' + loc(t.f, t.l, true) + '</span></div>'; }).join('') + '</div>' : '<div class="muted">No calls to functions defined in this repo.</div>');
  if (fn.ext && fn.ext.length) h.push('<div style="margin-top:10px" class="muted">Also calls (library, browser or outside this repo):</div><div class="chips" style="margin-top:4px">' + fn.ext.map(function(n){ return '<span>' + esc(n) + '</span>'; }).join('') + '</div>');
  if (fn.kids && fn.kids.length) h.push('<h2>Inner functions <span class="count">(' + fn.kids.length + ')</span></h2><div class="rows">' + fn.kids.map(function(k){ return '<div class="row">' + L.fn(k) + ' <span class="ln">:' + FN[k].l + '</span> <span class="muted">' + esc(firstLine(FN[k])) + '</span></div>'; }).join('') + '</div>');
  h.push(factsHtml(fn.facts || [], fn.f));
  var pages = (f.page ? [fn.f] : []).concat(f.pages || []);
  if (pages.length) h.push('<h2>Pages that load this code <span class="count">(' + pages.length + ')</span></h2><div class="rows">' + pages.map(function(p){ return '<div class="row">' + L.file(p) + '</div>'; }).join('') + '</div>');
  return h.join('');
}

function groupByFile(sites, rowFn){
  var by = {}, order = [];
  sites.forEach(function(s){ if (!by[s[1]]) { by[s[1]] = []; order.push(s[1]); } by[s[1]].push(s); });
  return '<div class="rows">' + order.map(function(fi){
    return '<div class="grp">' + L.file(fi) + ' <span class="muted">(' + by[fi].length + ')</span></div>' +
      by[fi].sort(function(a, b){ return a[2] - b[2]; }).map(function(s){ var hh = edHref(fi, s[2]); return '<div class="row">' + rowFn(s) + (hh ? ' <a class="ed" href="' + esc(hh) + '">↗</a>' : '') + '</div>'; }).join('');
  }).join('') + '</div>';
}

function factsHtml(facts, fi){
  if (!facts.length) return '';
  var g = {collection: [], http: [], route: [], emit: [], listen: [], storage: [], env: [], global: [], cloudfn: []};
  facts.forEach(function(x){ if (g[x[0]]) g[x[0]].push(x); });
  var h = '<h2>Data and wiring</h2><div class="rows">';
  function sec(title, arr, fmt){ if (!arr.length) return; h += '<div class="grp">' + title + '</div>' + arr.map(function(x){ return '<div class="row"><span class="ln">:' + x[2] + '</span> ' + fmt(x) + '</div>'; }).join(''); }
  sec('Database collections', g.collection, function(x){ return L.coll(x[1]) + ' ' + modeBadge(x[3]); });
  sec('HTTP calls', g.http, function(x){
    var rs = routeByClient[fi + ':' + x[2]] || [];
    return '<code>' + esc(x[3] || '') + ' ' + esc(x[1]) + '</code>' + (rs.length ? ' → ' + rs.map(L.route).join(', ') : ' <span class="muted">(no route in this repo)</span>');
  });
  sec('Defines routes', g.route, function(x){ return '<code>' + esc(x[3]) + ' ' + esc(x[1]) + '</code>'; });
  sec('Cloud functions', g.cloudfn, function(x){ return '<code>' + esc(x[1]) + '</code> <span class="muted">' + esc(x[3]) + '</span>'; });
  sec('Fires events', g.emit, function(x){ return L.event(x[1]); });
  sec('Listens for events', g.listen, function(x){ return L.event(x[1]); });
  sec('Storage keys', g.storage, function(x){ return L.storage(x[1]) + ' <span class="muted">' + esc(x[3]) + '</span>'; });
  sec('Environment variables', g.env, function(x){ return L.env(x[1]); });
  sec('Sets globals', g.global, function(x){ return L.global(x[1]); });
  return h + '</div>';
}

function vFile(fi){
  var f = F[fi];
  if (!f) return notFound();
  var ids = fnsByFile[fi] || [], h = [];
  var dd = ids.filter(function(i){ return docable(FN[i]); }), dn = dd.filter(function(i){ return FN[i].doc; }).length;
  h.push('<h1>' + esc(f.p) + (f.page ? ' <span class="badge">page</span>' : '') + '</h1>');
  h.push('<div class="sub-h">' + esc(f.lang) + ' · ' + plural(f.lines, 'line') + ' · ' + plural(ids.length, 'definition') + (dd.length ? ' · ' + dn + '/' + dd.length + ' commented' : '') + '</div>');
  h.push('<div class="actions"><a class="btn primary" href="#/graph/F' + fi + '">Show in graph</a>' + (edHref(fi, 1) ? '<a class="btn" href="' + esc(edHref(fi, 1)) + '">Open in editor</a>' : '') + '</div>');
  var uses = f.uses || [];
  if (uses.length) h.push('<h2>' + (f.page ? 'Scripts and styles this page loads' : 'Imports') + ' <span class="count">(' + uses.length + ')</span></h2><div class="rows">' + uses.map(function(u){ return '<div class="row"><span class="ln">:' + u[1] + '</span> ' + L.file(u[0]) + ' <span class="muted">' + (u[2] === 'load' ? '' : 'import') + '</span></div>'; }).join('') + '</div>');
  if (f.by && f.by.length) h.push('<h2>Loaded or imported by <span class="count">(' + f.by.length + ')</span></h2><div class="rows">' + f.by.map(function(b){ return '<div class="row">' + loc(b[0], b[1]) + ' <code>' + esc(b[2]) + '</code></div>'; }).join('') + '</div>');
  if (!f.page && f.pages && f.pages.length) h.push('<h2>Pages that end up running this file <span class="count">(' + f.pages.length + ')</span></h2><div class="rows">' + f.pages.map(function(p){ return '<div class="row">' + L.file(p) + '</div>'; }).join('') + '</div>');
  var rs = routesByFile[fi] || [];
  if (rs.length) h.push('<h2>API routes defined here <span class="count">(' + rs.length + ')</span></h2><div class="rows">' + rs.map(function(ri){ return '<div class="row"><span class="ln">:' + R[ri].l + '</span> ' + L.route(ri) + ' <span class="muted">' + plural(R[ri].cl.length, 'caller') + '</span></div>'; }).join('') + '</div>');
  if (ids.length) {
    h.push('<h2>Definitions <span class="count">(' + ids.length + ')</span></h2>');
    h.push(listBlock('defs', ids, function(i){ var fn = FN[i]; return '<tr><td class="num ln">' + fn.l + '</td><td>' + L.fn(i) + ' <span class="badge">' + esc(fn.k) + '</span>' + (docable(fn) && !fn.doc ? ' <span class="badge warn">no comment</span>' : '') + '<div class="muted">' + esc(firstLine(fn)) + '</div></td><td class="num">' + nCallers(i) + '</td></tr>'; },
      function(i){ return FN[i].q; }, '<tr><th class="num">Line</th><th>Name</th><th class="num">Callers</th></tr>', {noFilter: ids.length < 15}));
  }
  if (f.calls && f.calls.length) h.push('<h2>Called from top-level code <span class="count">(' + f.calls.length + ')</span></h2><div class="rows">' + f.calls.map(function(c){ return '<div class="row">' + L.fn(c) + '</div>'; }).join('') + '</div>');
  if (f.facts) h.push(factsHtml(f.facts, fi).replace('Data and wiring', 'Data and wiring (top-level code)'));
  return h.join('');
}

function vRoute(ri){
  var r = R[ri];
  if (!r) return notFound();
  var h = ['<h1><span class="badge" style="font-size:13px">' + esc(r.m) + '</span> ' + esc(r.path) + '</h1>'];
  h.push('<div class="sub-h">Defined at ' + loc(r.f, r.l) + (r.svc.length ? ' · service <b>' + esc(r.svc.join(', ')) + '</b>' : '') + '</div>');
  h.push('<div class="actions"><a class="btn primary" href="#/graph/r' + ri + '">Show in graph</a></div>');
  h.push('<h2>Handler</h2>' + (r.h >= 0 ? '<div>' + L.fn(r.h) + '</div>' + (FN[r.h].doc ? '<div class="card doc" style="margin-top:8px">' + docHtml(FN[r.h].doc.text) + '</div>' : '<div class="missing" style="margin-top:8px">The handler has no comment. Add one above line ' + FN[r.h].l + ' of <code>' + esc(F[r.f].p) + '</code>.</div>') : '<div class="muted">Not identified.</div>'));
  h.push('<h2>Called from <span class="count">(' + r.cl.length + ')</span></h2>');
  h.push(r.cl.length ? groupByFile(r.cl, function(c){ return '<span class="ln">:' + c[2] + '</span> ' + owner(c[0], c[1]) + ' <code>' + esc(c[3]) + '</code>'; }) : '<div class="muted">No caller found in this repo (it may be called by another app, a webhook, or a URL built at runtime).</div>');
  return h.join('');
}

function vSites(kind, name){
  var data = {coll: D.colls, event: D.events, storage: D.storage, env: D.env, global: D.globals}[kind], v = data && data[name];
  if (!v) return notFound();
  var title = {coll: 'Collection', event: 'Event', storage: 'Storage key', env: 'Environment variable', global: 'Global'}[kind];
  var h = ['<h1>' + esc(kind === 'global' ? 'window.' + name : name) + ' <span class="badge">' + title + '</span></h1>'];
  var gid = {coll: 'c:', event: 'e:', storage: 's:'}[kind];
  if (gid) h.push('<div class="actions"><a class="btn primary" href="#/graph/' + gid + enc(name) + '">Show in graph</a></div>');
  if (kind === 'event') {
    h.push('<h2>Fired by <span class="count">(' + v.emit.length + ')</span></h2>' + (v.emit.length ? groupByFile(v.emit, function(s){ return '<span class="ln">:' + s[2] + '</span> ' + owner(s[0], s[1]); }) : '<div class="muted">Never fired in this repo.</div>'));
    h.push('<h2>Listened for by <span class="count">(' + v.listen.length + ')</span></h2>' + (v.listen.length ? groupByFile(v.listen, function(s){ return '<span class="ln">:' + s[2] + '</span> ' + owner(s[0], s[1]); }) : '<div class="muted">Nobody listens in this repo.</div>'));
    return h.join('');
  }
  if (kind === 'coll') {
    var r = 0, w = 0, nl = 0;
    v.forEach(function(s){ if (s[3].indexOf('read') >= 0) r++; if (s[3].indexOf('write') >= 0) w++; if (s[4].indexOf('no-limit') >= 0) nl++; });
    h.push('<div class="sub-h">' + plural(v.length, 'site') + ' · ' + r + ' read · ' + w + ' write' + (nl ? ' · <b>' + nl + ' read(s) with no .limit() seen</b>' : '') + '</div>');
  }
  h.push('<h2>Used in <span class="count">(' + v.length + ')</span></h2>');
  h.push(groupByFile(v, function(s){ return '<span class="ln">:' + s[2] + '</span> ' + owner(s[0], s[1]) + (kind === 'coll' ? ' ' + modeBadge(s[3]) + (s[4] ? ' <span class="badge">' + esc(s[4]) + '</span>' : '') : '') + (kind === 'storage' && s[3] ? ' <span class="muted">' + esc(s[3]) + '</span>' : ''); }));
  return h.join('');
}

function vList(kind){
  var h = [];
  if (kind === 'functions') {
    var ids = FN.map(function(x, i){ return i; }).filter(function(i){ return docable(FN[i]); }).sort(function(a, b){ return nCallers(b) - nCallers(a) || (FN[a].q < FN[b].q ? -1 : 1); });
    h.push('<h1>Functions</h1><div class="sub-h">' + ids.length + ' functions, methods, classes and route handlers, most used first. Event-listener callbacks are listed on their file\'s page.</div>');
    h.push(listBlock('fl', ids, function(i){ var fn = FN[i]; return '<tr><td>' + L.fn(i) + (fn.doc ? '' : ' <span class="badge warn">no comment</span>') + '<div class="muted">' + esc(firstLine(fn)) + '</div></td><td class="num">' + nCallers(i) + '</td><td>' + loc(fn.f, fn.l, true) + '</td></tr>'; },
      function(i){ return FN[i].q + ' ' + F[FN[i].f].p; }, '<tr><th>Name</th><th class="num">Callers</th><th>Where</th></tr>'));
  } else if (kind === 'pages' || kind === 'files') {
    var fs = F.map(function(x, i){ return i; }).filter(function(i){ return kind === 'pages' ? F[i].page : !F[i].page; });
    h.push('<h1>' + (kind === 'pages' ? 'Pages' : 'Files') + '</h1>');
    h.push(listBlock('fi', fs, function(i){ var f = F[i], n = (fnsByFile[i] || []).length; return '<tr><td>' + L.file(i) + '</td><td>' + esc(f.lang) + '</td><td class="num">' + f.lines + '</td><td class="num">' + n + '</td><td class="num">' + (kind === 'pages' ? (f.uses || []).length : (f.pages || []).length) + '</td></tr>'; },
      function(i){ return F[i].p; }, '<tr><th>Path</th><th>Lang</th><th class="num">Lines</th><th class="num">Definitions</th><th class="num">' + (kind === 'pages' ? 'Loads' : 'Pages') + '</th></tr>'));
  } else if (kind === 'routes') {
    var rs = R.map(function(x, i){ return i; });
    h.push('<h1>API routes</h1><div class="sub-h">Server routes and how many places in the repo call them.</div>');
    h.push(listBlock('rl', rs, function(i){ var r = R[i]; return '<tr><td>' + L.route(i) + '</td><td>' + esc(r.svc.join(', ')) + '</td><td>' + (r.h >= 0 ? L.fn(r.h) : '') + '</td><td class="num">' + r.cl.length + '</td></tr>'; },
      function(i){ return R[i].m + ' ' + R[i].path + ' ' + R[i].svc.join(' '); }, '<tr><th>Route</th><th>Service</th><th>Handler</th><th class="num">Callers</th></tr>'));
  } else if (kind === 'collections') {
    var cs = Object.keys(D.colls).sort(function(a, b){ return D.colls[b].length - D.colls[a].length; });
    h.push('<h1>Database collections</h1>');
    h.push(listBlock('cl', cs, function(n){ var v = D.colls[n], r = 0, w = 0, nl = 0; v.forEach(function(s){ if (s[3].indexOf('read') >= 0) r++; if (s[3].indexOf('write') >= 0) w++; if (s[4].indexOf('no-limit') >= 0) nl++; });
      return '<tr><td>' + L.coll(n) + '</td><td class="num">' + v.length + '</td><td class="num">' + r + '</td><td class="num">' + w + '</td><td class="num">' + (nl || '') + '</td></tr>'; },
      function(n){ return n; }, '<tr><th>Name</th><th class="num">Sites</th><th class="num">Reads</th><th class="num">Writes</th><th class="num">No limit</th></tr>'));
  } else if (kind === 'events') {
    var es = Object.keys(D.events).sort();
    h.push('<h1>Custom events</h1>');
    h.push(listBlock('el', es, function(n){ var v = D.events[n]; return '<tr><td>' + L.event(n) + (v.emit.length ? '' : ' <span class="badge warn">never fired here</span>') + (v.listen.length ? '' : ' <span class="badge">nobody listens here</span>') + '</td><td class="num">' + v.emit.length + '</td><td class="num">' + v.listen.length + '</td></tr>'; },
      function(n){ return n; }, '<tr><th>Event</th><th class="num">Fired</th><th class="num">Listeners</th></tr>'));
  } else if (kind === 'storage') {
    var ss = Object.keys(D.storage).sort(), ev = Object.keys(D.env).sort(), gl = Object.keys(D.globals).sort();
    h.push('<h1>Storage keys, env vars and globals</h1>');
    h.push('<h2>Browser / app storage keys <span class="count">(' + ss.length + ')</span></h2>' + listBlock('sl', ss, function(n){ return '<tr><td>' + L.storage(n) + '</td><td class="num">' + D.storage[n].length + '</td></tr>'; }, function(n){ return n; }, '<tr><th>Key</th><th class="num">Sites</th></tr>'));
    h.push('<h2>Environment variables <span class="count">(' + ev.length + ')</span></h2>' + listBlock('vl', ev, function(n){ return '<tr><td>' + L.env(n) + '</td><td class="num">' + D.env[n].length + '</td></tr>'; }, function(n){ return n; }, '<tr><th>Name</th><th class="num">Sites</th></tr>'));
    h.push('<h2>Globals (window.*) <span class="count">(' + gl.length + ')</span></h2>' + listBlock('gl', gl, function(n){ return '<tr><td>' + L.global(n) + '</td><td class="num">' + D.globals[n].length + '</td></tr>'; }, function(n){ return n; }, '<tr><th>Name</th><th class="num">Sites</th></tr>'));
  } else return notFound();
  return h.join('');
}

function vMissing(prefix){
  var ids = FN.map(function(x, i){ return i; }).filter(function(i){ var fn = FN[i]; return docable(fn) && !fn.doc && (!prefix || F[fn.f].p.indexOf(prefix) === 0); })
    .sort(function(a, b){ return (isTest(FN[a].f) - isTest(FN[b].f)) || nCallers(b) - nCallers(a) || ((F[FN[b].f].pages || []).length - (F[FN[a].f].pages || []).length); });
  var h = ['<h1>Missing comments' + (prefix ? ' <span class="badge">' + esc(prefix) + '</span>' : '') + '</h1>'];
  h.push('<div class="sub-h">' + ids.length + ' function(s) have no comment' + (prefix ? ' under ' + esc(prefix) + ' · <a href="#/missing">show all folders</a>' : '') + '. Most used first (test files last): commenting the top of this list improves the docs the most. Add a comment directly above each one (JSDoc <code>/** … */</code>, <code>//</code> lines, or a Python docstring), then run <code>repofree docs</code> again. The same list is in <code>missing-comments.md</code>.</div>');
  h.push(listBlock('ml', ids, function(i){ var fn = FN[i]; return '<tr><td>' + L.fn(i) + ' <span class="badge">' + esc(fn.k) + '</span>' + (isTest(fn.f) ? ' <span class="badge">test file</span>' : '') + '</td><td class="num">' + nCallers(i) + '</td><td class="num">' + ((F[fn.f].pages || []).length || '') + '</td><td>' + loc(fn.f, fn.l) + '</td></tr>'; },
    function(i){ return FN[i].q + ' ' + F[FN[i].f].p; }, '<tr><th>Name</th><th class="num">Callers</th><th class="num">Pages</th><th>Add the comment above</th></tr>'));
  return h.join('');
}
function notFound(){ return '<h1>Not found</h1><p class="muted">This item is not in the docs. They may be older than the link; run <code>repofree docs</code> again.</p>'; }

// ---------- router
var current = '';
function route(){
  var hash = location.hash.replace(/^#\/?/, ''), parts = hash.split('/'), v = parts[0] || (START === 'graph' && !current ? 'graph' : 'home');
  var rawArg = parts.slice(1).join('/'), arg = decodeURIComponent(rawArg);
  var navKey = v;
  if (v === 'list') navKey = parts[1];
  if (v === 'fn') navKey = 'functions';
  if (v === 'file') navKey = F[+arg] && F[+arg].page ? 'pages' : 'files';
  if (v === 'route') navKey = 'routes';
  if (v === 'coll') navKey = 'collections';
  if (v === 'event') navKey = 'events';
  if (v === 'storage' || v === 'env' || v === 'global') navKey = 'storage';
  document.querySelectorAll('#nav a').forEach(function(a){ a.classList.toggle('on', a.getAttribute('data-v') === navKey); });
  current = v;
  var gw = document.getElementById('graphwrap');
  if (v === 'graph') {
    main.style.display = 'none'; gw.classList.add('on'); sizeHeader();
    graphShow(rawArg || null);
    document.title = 'Graph · ' + D.meta.repo;
    return;
  }
  gw.classList.remove('on'); main.style.display = '';
  var html;
  switch (v) {
    case 'home': html = vHome(); break;
    case 'fn': html = vFn(+arg); break;
    case 'file': html = vFile(+arg); break;
    case 'route': html = vRoute(+arg); break;
    case 'coll': html = vSites('coll', arg); break;
    case 'event': html = vSites('event', arg); break;
    case 'storage': html = vSites('storage', arg); break;
    case 'env': html = vSites('env', arg); break;
    case 'global': html = vSites('global', arg); break;
    case 'list': html = vList(parts[1]); break;
    case 'missing': html = vMissing(arg); break;
    default: html = notFound();
  }
  main.innerHTML = html;
  var h1 = main.querySelector('h1');
  document.title = (h1 ? h1.textContent + ' · ' : '') + D.meta.repo + ' docs';
  window.scrollTo(0, 0);
}
function sizeHeader(){ document.documentElement.style.setProperty('--hh', document.getElementById('hdr').offsetHeight + 'px'); }
window.addEventListener('resize', sizeHeader);
document.getElementById('brand').innerHTML = esc(D.meta.repo) + '<small>docs</small>';

// =====================================================================
// graph
// =====================================================================
var SVGNS = 'http://www.w3.org/2000/svg';
var NT = {
  f: {label: 'Function', c: '--n-fn', r: 7, ic: 'ƒ'},
  F: {label: 'Script / file', c: '--n-file', r: 10, ic: 'JS'},
  P: {label: 'Page', c: '--n-page', r: 13, ic: '▣'},
  r: {label: 'API route', c: '--n-route', r: 10, ic: '⇄'},
  c: {label: 'Collection', c: '--n-coll', r: 11, ic: 'DB'},
  e: {label: 'Event', c: '--n-event', r: 9, ic: '⚡'},
  s: {label: 'Storage key', c: '--n-store', r: 8, ic: 'K'}
};
var ET = {
  'calls': '--e-calls', 'defines': '--e-struct', 'contains': '--e-struct', 'loads': '--e-load', 'imports': '--e-load',
  'calls API': '--e-http', 'handled by': '--e-http', 'declares': '--e-http', 'read': '--e-read', 'write': '--e-write',
  'read+write': '--e-write', 'ref': '--e-struct', 'path': '--e-struct', 'fires': '--e-event', 'triggers': '--e-event', 'storage': '--e-store'
};
var EORDER = ['calls', 'calls API', 'handled by', 'read', 'write', 'read+write', 'ref', 'path', 'fires', 'triggers', 'loads', 'imports', 'declares', 'defines', 'contains', 'storage'];
function ntype(id){ var t = id[0]; if (t === 'F') return F[+id.slice(1)].page ? 'P' : 'F'; return t; }
function nlabel(id){
  switch (id[0]) {
    case 'f': return FN[+id.slice(1)].q;
    case 'F': return base(F[+id.slice(1)].p);
    case 'r': var r = R[+id.slice(1)]; return r.m + ' ' + r.path;
    default: return decodeURIComponent(id.slice(2));
  }
}
function nhref(id){
  switch (id[0]) {
    case 'f': return '#/fn/' + id.slice(1);
    case 'F': return '#/file/' + id.slice(1);
    case 'r': return '#/route/' + id.slice(1);
    case 'c': return '#/coll/' + id.slice(2);
    case 'e': return '#/event/' + id.slice(2);
    case 's': return '#/storage/' + id.slice(2);
  }
}
function ownerId(o, fi){ return o >= 0 ? 'f' + o : 'F' + fi; }
function nid(kind, name){ return kind + ':' + enc(name); }
var ADJ = {};
function neighbors(id){
  if (ADJ[id]) return ADJ[id];
  var out = [], seen = {};
  function add(other, type, dir, maybe){ var k = other + '|' + type + '|' + dir; if (seen[k] || other === id) return; seen[k] = 1; out.push({id: other, type: type, dir: dir, maybe: !!maybe}); }
  function facts(list, fi){
    (list || []).forEach(function(x){
      if (x[0] === 'collection') add(nid('c', x[1]), x[3] || 'ref', 'out');
      else if (x[0] === 'emit') add(nid('e', x[1]), 'fires', 'out');
      else if (x[0] === 'listen') add(nid('e', x[1]), 'triggers', 'in');
      else if (x[0] === 'storage') add(nid('s', x[1]), 'storage', 'out');
      else if (x[0] === 'http') (routeByClient[fi + ':' + x[2]] || []).forEach(function(ri){ add('r' + ri, 'calls API', 'out'); });
    });
  }
  var t = id[0], k = +id.slice(1);
  if (t === 'f') {
    var fn = FN[k];
    (fn.callers || []).forEach(function(c){ add(ownerId(c[0], c[1]), 'calls', 'in', c[4]); });
    (fn.callees || []).forEach(function(c){ add('f' + c[0], 'calls', 'out', c[1]); });
    (routesByHandler[k] || []).forEach(function(ri){ add('r' + ri, 'handled by', 'in'); });
    facts(fn.facts, fn.f);
    if (fn.p != null) add('f' + fn.p, 'contains', 'in'); else add('F' + fn.f, 'defines', 'in');
    (fn.kids || []).forEach(function(c){ add('f' + c, 'contains', 'out'); });
  } else if (t === 'F') {
    var f = F[k];
    (f.by || []).forEach(function(b){ add('F' + b[0], b[4] === 'load' ? 'loads' : 'imports', 'in'); });
    (f.uses || []).forEach(function(u){ add('F' + u[0], u[2] === 'load' ? 'loads' : 'imports', 'out'); });
    (f.calls || []).forEach(function(c){ add('f' + c, 'calls', 'out'); });
    facts(f.facts, k);
    (routesByFile[k] || []).forEach(function(ri){ add('r' + ri, 'declares', 'out'); });
    (fnsByFile[k] || []).forEach(function(i){ if (FN[i].p == null) add('f' + i, 'defines', 'out'); });
  } else if (t === 'r') {
    var r = R[k];
    if (r.h >= 0) add('f' + r.h, 'handled by', 'out');
    r.cl.forEach(function(c){ add(ownerId(c[0], c[1]), 'calls API', 'in'); });
    add('F' + r.f, 'declares', 'in');
  } else {
    var name = decodeURIComponent(id.slice(2));
    if (t === 'c') (D.colls[name] || []).forEach(function(s){ add(ownerId(s[0], s[1]), s[3] || 'ref', 'in'); });
    if (t === 'e') { var ev = D.events[name] || {emit: [], listen: []}; ev.emit.forEach(function(s){ add(ownerId(s[0], s[1]), 'fires', 'in'); }); ev.listen.forEach(function(s){ add(ownerId(s[0], s[1]), 'triggers', 'out'); }); }
    if (t === 's') (D.storage[name] || []).forEach(function(s){ add(ownerId(s[0], s[1]), 'storage', 'in'); });
  }
  out.sort(function(a, b){ return EORDER.indexOf(a.type) - EORDER.indexOf(b.type); });
  return ADJ[id] = out;
}
function exists(id){
  var t = id[0], k = id.slice(1);
  if (t === 'f') return !!FN[+k];
  if (t === 'F') return !!F[+k];
  if (t === 'r') return !!R[+k];
  var n = decodeURIComponent(id.slice(2));
  return !!(t === 'c' ? D.colls[n] : t === 'e' ? D.events[n] : t === 's' ? D.storage[n] : null);
}

var G = {nodes: {}, edges: {}, sel: null, hidden: {}, alpha: 0, tf: {x: 0, y: 0, k: 1}, raf: 0, built: false};
var svg, gview, gnodes, gedges, panel;
function cssv(name){ return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function graphInit(){
  if (G.built) return;
  G.built = true;
  svg = document.getElementById('gsvg'); gview = document.getElementById('gview'); gnodes = document.getElementById('gnodes'); gedges = document.getElementById('gedges'); panel = document.getElementById('gpanel');
  var defs = document.getElementById('gdefs');
  Object.keys(ET).forEach(function(t){
    var m = document.createElementNS(SVGNS, 'marker');
    m.setAttribute('id', 'ar-' + t.replace(/[^a-zA-Z]/g, '')); m.setAttribute('viewBox', '0 0 10 10'); m.setAttribute('refX', '9'); m.setAttribute('refY', '5');
    m.setAttribute('markerWidth', '7'); m.setAttribute('markerHeight', '7'); m.setAttribute('orient', 'auto-start-reverse');
    var p = document.createElementNS(SVGNS, 'path'); p.setAttribute('d', 'M0,0 L10,5 L0,10 z'); p.style.fill = 'var(' + ET[t] + ')';
    m.appendChild(p); defs.appendChild(m);
  });
  // legend / filters
  var lg = document.getElementById('glegend');
  lg.innerHTML = '<div class="muted" style="margin-bottom:2px">Show</div>' + Object.keys(NT).map(function(t){
    return '<label><input type="checkbox" data-t="' + t + '" checked> <span class="dot" style="background:var(' + NT[t].c + ')"></span>' + NT[t].label + '</label>';
  }).join('') + '<div class="muted" style="margin-top:4px;max-width:170px">Click: details · double-click: expand · drag: move (pins) · scroll: zoom</div>';
  lg.addEventListener('change', function(e){ var t = e.target.getAttribute('data-t'); if (!t) return; if (e.target.checked) delete G.hidden[t]; else G.hidden[t] = 1; applyHidden(); });
  wireSearch(document.getElementById('gq'), document.getElementById('gqr'), function(s){
    var id = ({fn: 'f' + s.i, page: 'F' + s.i, file: 'F' + s.i, route: 'r' + s.i, coll: nid('c', s.i), event: nid('e', s.i), storage: nid('s', s.i)})[s.t];
    if (!id) return;
    addNode(id); select(id); kick(0.8);
  });
  document.getElementById('gfit').onclick = fit;
  document.getElementById('gclear').onclick = function(){ clearGraph(); renderPanel(); emptyState(); };
  // pan / zoom
  var pan = null;
  svg.addEventListener('pointerdown', function(e){
    if (e.target.closest('.node')) return;
    pan = {x: e.clientX, y: e.clientY, tx: G.tf.x, ty: G.tf.y, moved: false};
    svg.classList.add('panning'); svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener('pointermove', function(e){
    if (drag) { dragMove(e); return; }
    if (!pan) return;
    var dx = e.clientX - pan.x, dy = e.clientY - pan.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) pan.moved = true;
    G.tf.x = pan.tx + dx; G.tf.y = pan.ty + dy; applyTf();
  });
  svg.addEventListener('pointerup', function(e){
    if (drag) { dragEnd(e); return; }
    if (pan && !pan.moved) select(null);
    pan = null; svg.classList.remove('panning');
  });
  svg.addEventListener('wheel', function(e){
    e.preventDefault();
    var r = svg.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    var k2 = Math.max(0.15, Math.min(4, G.tf.k * Math.exp(-e.deltaY * 0.0015)));
    G.tf.x = mx - (mx - G.tf.x) * k2 / G.tf.k; G.tf.y = my - (my - G.tf.y) * k2 / G.tf.k; G.tf.k = k2; applyTf();
  }, {passive: false});
  var r0 = svg.getBoundingClientRect();
  G.tf.x = r0.width / 2; G.tf.y = r0.height / 2; applyTf();
}
function applyTf(){ gview.setAttribute('transform', 'translate(' + G.tf.x + ',' + G.tf.y + ') scale(' + G.tf.k + ')'); }

function degree(id){ return neighbors(id).length; }
function addNode(id, near){
  if (G.nodes[id] || !exists(id)) return G.nodes[id];
  var t = ntype(id), base0 = NT[t].r, deg = degree(id);
  var r = t === 'f' ? Math.min(base0 + Math.log(1 + deg) * 1.6, 16) : base0;
  var p = near && G.nodes[near] ? G.nodes[near] : null;
  var ang = Math.random() * Math.PI * 2, dist = p ? 60 + Math.random() * 40 : Math.random() * 30;
  var n = {id: id, t: t, r: r, x: (p ? p.x : 0) + Math.cos(ang) * dist, y: (p ? p.y : 0) + Math.sin(ang) * dist, vx: 0, vy: 0, pin: false};
  var g = document.createElementNS(SVGNS, 'g'); g.setAttribute('class', 'node'); g.setAttribute('data-id', id);
  var c = document.createElementNS(SVGNS, 'circle'); c.setAttribute('r', r); c.style.fill = 'var(' + NT[t].c + ')';
  var ic = document.createElementNS(SVGNS, 'text'); ic.setAttribute('class', 'ic'); ic.textContent = r >= 9 ? NT[t].ic : '';
  var tx = document.createElementNS(SVGNS, 'text'); tx.setAttribute('x', r + 4); tx.setAttribute('y', 4);
  var lb = nlabel(id); tx.textContent = lb.length > 28 ? lb.slice(0, 27) + '…' : lb;
  var ti = document.createElementNS(SVGNS, 'title'); ti.textContent = NT[t].label + ': ' + lb;
  g.appendChild(c); g.appendChild(ic); g.appendChild(tx); g.appendChild(ti);
  g.addEventListener('pointerdown', function(e){ dragStart(e, n); });
  g.addEventListener('dblclick', function(e){ e.stopPropagation(); expand(id); });
  gnodes.appendChild(g);
  n.el = g;
  G.nodes[id] = n;
  // connect to everything already on the canvas
  neighbors(id).forEach(function(nb){ if (G.nodes[nb.id]) addEdge(id, nb); });
  if (G.hidden[t]) g.style.display = 'none';
  document.getElementById('gempty').innerHTML = '';
  return n;
}
function addEdge(id, nb){
  var from = nb.dir === 'out' ? id : nb.id, to = nb.dir === 'out' ? nb.id : id, key = from + '>' + to + '>' + nb.type;
  if (G.edges[key]) return;
  G.nodes[from].deg = (G.nodes[from].deg || 0) + 1; G.nodes[to].deg = (G.nodes[to].deg || 0) + 1;
  var l = document.createElementNS(SVGNS, 'line');
  l.setAttribute('class', 'edge' + (nb.maybe ? ' maybe' : ''));
  l.style.stroke = 'var(' + (ET[nb.type] || '--e-struct') + ')';
  l.setAttribute('marker-end', 'url(#ar-' + nb.type.replace(/[^a-zA-Z]/g, '') + ')');
  var ti = document.createElementNS(SVGNS, 'title'); ti.textContent = nlabel(from) + ' ' + nb.type + ' ' + nlabel(to); l.appendChild(ti);
  gedges.appendChild(l);
  G.edges[key] = {from: from, to: to, type: nb.type, el: l};
}
function removeNode(id){
  var n = G.nodes[id]; if (!n) return;
  n.el.remove(); delete G.nodes[id];
  Object.keys(G.edges).forEach(function(k){ var e = G.edges[k]; if (e.from === id || e.to === id) { e.el.remove(); delete G.edges[k]; } });
  if (G.sel === id) G.sel = null;
}
function clearGraph(){ Object.keys(G.nodes).forEach(removeNode); G.sel = null; }
function visible(n){ return !G.hidden[n.t]; }
function applyHidden(){
  Object.keys(G.nodes).forEach(function(id){ var n = G.nodes[id]; n.el.style.display = visible(n) ? '' : 'none'; });
  Object.keys(G.edges).forEach(function(k){ var e = G.edges[k]; e.el.style.display = visible(G.nodes[e.from]) && visible(G.nodes[e.to]) ? '' : 'none'; });
  kick(0.4); renderPanel();
}
var EXPAND_CAP = 40;
function expand(id, list){
  var nbs = (list || neighbors(id)).filter(function(nb){ return !G.nodes[nb.id] && !G.hidden[ntype(nb.id)]; });
  nbs.slice(0, EXPAND_CAP).forEach(function(nb){ addNode(nb.id, id); });
  kick(0.9); renderPanel();
  return nbs.length - Math.min(nbs.length, EXPAND_CAP);
}
function select(id){
  G.sel = id;
  svg.classList.toggle('focus', !!id);
  var near = {};
  if (id) { near[id] = 1; neighbors(id).forEach(function(nb){ near[nb.id] = 1; }); }
  Object.keys(G.nodes).forEach(function(k){ var n = G.nodes[k]; n.el.classList.toggle('sel', k === id); n.el.classList.toggle('dim', !!id && !near[k]); });
  Object.keys(G.edges).forEach(function(k){ var e = G.edges[k], on = id && (e.from === id || e.to === id); e.el.classList.toggle('hl', !!on); e.el.classList.toggle('dim', !!id && !on); });
  renderPanel();
}
function renderPanel(){
  if (!panel) return;
  var id = G.sel;
  if (!id) {
    var n = Object.keys(G.nodes).length;
    panel.innerHTML = '<h3 style="margin:0 0 6px">Graph</h3><div class="muted">' + (n ? plural(n, 'item') + ' on the canvas. Click one to see its connections; double-click to expand it.' : 'Search above to add a function, page, route, collection or event.') + '</div>' +
      '<div class="muted" style="margin-top:12px;font-size:12px">Arrows point from the caller/user to what it uses. Dashed = "maybe" (same name defined in several places).</div>';
    return;
  }
  var t = ntype(id), h = [];
  h.push('<div class="muted" style="font-size:12px">' + NT[t].label + '</div><h3 style="margin:2px 0 6px;word-break:break-word">' + esc(nlabel(id)) + '</h3>');
  if (t === 'f') {
    var fn = FN[+id.slice(1)];
    h.push('<div class="muted" style="font-size:12px">' + esc(F[fn.f].p) + ':' + fn.l + '</div>');
    h.push(fn.doc && fn.doc.text ? '<div class="doc" style="margin-top:8px">' + docHtml(fn.doc.text.length > 600 ? fn.doc.text.slice(0, 600) + '…' : fn.doc.text) + '</div>' : (docable(fn) ? '<div class="missing" style="margin-top:8px;padding:8px 10px;font-size:12.5px">No comment in the code.</div>' : ''));
  } else if (t === 'F' || t === 'P') {
    h.push('<div class="muted" style="font-size:12px">' + esc(F[+id.slice(1)].p) + '</div>');
  }
  var nbs = neighbors(id), shown = nbs.filter(function(nb){ return !G.hidden[ntype(nb.id)]; }), missingN = shown.filter(function(nb){ return !G.nodes[nb.id]; }).length;
  h.push('<div class="actions"><a class="btn" href="' + nhref(id) + '">Open docs page</a>' + (missingN ? '<button class="btn primary" data-act="expand">Expand (' + Math.min(missingN, EXPAND_CAP) + (missingN > EXPAND_CAP ? ' of ' + missingN : '') + ')</button>' : '') +
    '<button class="btn" data-act="focus" title="Clear the canvas and start from this item">Focus</button><button class="btn" data-act="hide">Hide</button>' + (G.nodes[id] && G.nodes[id].pin ? '<button class="btn" data-act="unpin">Unpin</button>' : '') + '</div>');
  var groups = {}, order = [];
  shown.forEach(function(nb){ var k = nb.type + '|' + nb.dir; if (!groups[k]) { groups[k] = []; order.push(k); } groups[k].push(nb); });
  h.push('<div class="conn">' + (order.length ? '' : '<div class="muted">No connections found.</div>'));
  order.forEach(function(k){
    var p = k.split('|'), list = groups[k], miss = list.filter(function(nb){ return !G.nodes[nb.id]; }).length;
    h.push('<div class="t"><span>' + esc(dirLabel(p[0], p[1], t)) + ' (' + list.length + ')</span>' + (miss ? '<button class="linkbtn" data-add="' + esc(k) + '">add ' + (miss > 60 ? 'first 60' : 'all') + '</button>' : '') + '</div>');
    list.slice(0, 200).forEach(function(nb){ h.push('<a href="#" class="it ' + (G.nodes[nb.id] ? 'in-g' : 'out-g') + '" data-id="' + esc(nb.id) + '" title="' + esc(nlabel(nb.id)) + '">' + esc(nlabel(nb.id)) + (nb.maybe ? ' <span class="badge maybe">maybe</span>' : '') + '</a>'); });
    if (list.length > 200) h.push('<div class="muted">… ' + (list.length - 200) + ' more on the docs page</div>');
  });
  h.push('</div>');
  panel.innerHTML = h.join('');
  panel.onclick = function(e){
    var a = e.target.closest('[data-act],[data-add],a.it');
    if (!a) return;
    if (a.matches('a.it')) { e.preventDefault(); var oid = a.getAttribute('data-id'); if (G.nodes[oid]) select(oid); else { addNode(oid, id); kick(0.6); select(id); } return; }
    if (a.hasAttribute('data-add')) { expand(id, groups[a.getAttribute('data-add')].slice(0, 60)); select(id); return; }
    var act = a.getAttribute('data-act');
    if (act === 'expand') { expand(id); select(id); }
    else if (act === 'focus') focus(id);
    else if (act === 'hide') { removeNode(id); select(null); kick(0.3); }
    else if (act === 'unpin') { G.nodes[id].pin = false; G.nodes[id].el.classList.remove('pinned'); kick(0.3); renderPanel(); }
  };
}
function dirLabel(type, dir, t){
  var m = {
    'calls|in': 'Called by', 'calls|out': 'Calls', 'calls API|out': 'Calls API route', 'calls API|in': 'Called from',
    'handled by|in': 'Handles route', 'handled by|out': 'Handled by', 'declares|out': 'Declares route', 'declares|in': 'Declared in',
    'defines|in': 'Defined in', 'defines|out': 'Defines', 'contains|in': 'Inside', 'contains|out': 'Inner functions',
    'loads|in': 'Loaded by page', 'loads|out': 'Loads', 'imports|in': 'Imported by', 'imports|out': 'Imports',
    'fires|out': 'Fires event', 'fires|in': 'Fired by', 'triggers|in': 'Listens for', 'triggers|out': 'Wakes up listener',
    'storage|out': 'Uses storage key', 'storage|in': 'Used by',
    'read|out': 'Reads collection', 'write|out': 'Writes collection', 'read+write|out': 'Reads + writes', 'ref|out': 'References collection', 'path|out': 'References collection',
    'read|in': 'Read by', 'write|in': 'Written by', 'read+write|in': 'Read + written by', 'ref|in': 'Referenced by', 'path|in': 'Referenced by'
  };
  return m[type + '|' + dir] || (type + ' (' + dir + ')');
}
function focus(id){
  clearGraph();
  var n = addNode(id); if (!n) return;
  n.x = 0; n.y = 0;
  expand(id);
  select(id);
  G.tf.k = 1; var r = svg.getBoundingClientRect(); G.tf.x = r.width / 2; G.tf.y = r.height / 2; applyTf();
  setTimeout(fit, 900);
}
function fit(){
  var ids = Object.keys(G.nodes).filter(function(k){ return visible(G.nodes[k]); });
  if (!ids.length) return;
  var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  ids.forEach(function(k){ var n = G.nodes[k]; x0 = Math.min(x0, n.x - 20); y0 = Math.min(y0, n.y - 20); x1 = Math.max(x1, n.x + 140); y1 = Math.max(y1, n.y + 20); });
  var r = svg.getBoundingClientRect(), k = Math.max(0.2, Math.min(1.6, Math.min(r.width / (x1 - x0), r.height / (y1 - y0)) * 0.92));
  G.tf.k = k; G.tf.x = r.width / 2 - (x0 + x1) / 2 * k; G.tf.y = r.height / 2 - (y0 + y1) / 2 * k; applyTf();
}
// dragging nodes
var drag = null;
function dragStart(e, n){
  e.stopPropagation();
  drag = {n: n, x: e.clientX, y: e.clientY, moved: false};
  svg.setPointerCapture(e.pointerId);
}
function dragMove(e){
  var dx = (e.clientX - drag.x) / G.tf.k, dy = (e.clientY - drag.y) / G.tf.k;
  if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3 / G.tf.k) return;
  drag.moved = true;
  drag.n.x += dx; drag.n.y += dy; drag.x = e.clientX; drag.y = e.clientY;
  drag.n.pin = true; drag.n.el.classList.add('pinned');
  kick(0.3); draw();
}
function dragEnd(){
  var d = drag; drag = null;
  if (!d.moved) select(d.n.id);
}
// physics
function kick(a){ G.alpha = Math.max(G.alpha, a); if (!G.raf) G.raf = requestAnimationFrame(loop); }
function loop(){
  G.raf = 0;
  if (G.alpha < 0.01) return;
  for (var s = 0; s < 2; s++) tick();
  draw();
  G.raf = requestAnimationFrame(loop);
}
function tick(){
  var ns = Object.keys(G.nodes).map(function(k){ return G.nodes[k]; }).filter(visible), n = ns.length, a = G.alpha;
  for (var i = 0; i < n; i++) {
    var p = ns[i];
    for (var j = i + 1; j < n; j++) {
      var q = ns[j], dx = q.x - p.x, dy = q.y - p.y, d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
      if (d2 > 640000) continue;
      var f = 5200 * a / d2, d = Math.sqrt(d2);
      var fx = dx / d * f, fy = dy / d * f;
      p.vx -= fx; p.vy -= fy; q.vx += fx; q.vy += fy;
    }
  }
  Object.keys(G.edges).forEach(function(k){
    var e = G.edges[k], p = G.nodes[e.from], q = G.nodes[e.to];
    if (!p || !q || !visible(p) || !visible(q)) return;
    var dx = q.x - p.x, dy = q.y - p.y, d = Math.sqrt(dx * dx + dy * dy) || 1, want = 110 + p.r + q.r + 6 * Math.sqrt(Math.min(p.deg || 1, q.deg || 1));
    var f = (d - want) * 0.03 * a, fx = dx / d * f, fy = dy / d * f;
    p.vx += fx; p.vy += fy; q.vx -= fx; q.vy -= fy;
  });
  ns.forEach(function(p){
    p.vx -= p.x * 0.004 * a; p.vy -= p.y * 0.004 * a;
    if (p.pin || (drag && drag.n === p)) { p.vx = 0; p.vy = 0; return; }
    p.vx *= 0.62; p.vy *= 0.62;
    p.x += Math.max(-30, Math.min(30, p.vx)); p.y += Math.max(-30, Math.min(30, p.vy));
  });
  G.alpha *= 0.985;
}
function draw(){
  Object.keys(G.nodes).forEach(function(k){ var n = G.nodes[k]; n.el.setAttribute('transform', 'translate(' + n.x.toFixed(1) + ',' + n.y.toFixed(1) + ')'); });
  Object.keys(G.edges).forEach(function(k){
    var e = G.edges[k], p = G.nodes[e.from], q = G.nodes[e.to];
    if (!p || !q) return;
    var dx = q.x - p.x, dy = q.y - p.y, d = Math.sqrt(dx * dx + dy * dy) || 1, ux = dx / d, uy = dy / d;
    e.el.setAttribute('x1', (p.x + ux * p.r).toFixed(1)); e.el.setAttribute('y1', (p.y + uy * p.r).toFixed(1));
    e.el.setAttribute('x2', (q.x - ux * (q.r + 2)).toFixed(1)); e.el.setAttribute('y2', (q.y - uy * (q.r + 2)).toFixed(1));
  });
}
function emptyState(){
  if (Object.keys(G.nodes).length) return;
  var pages = F.map(function(f, i){ return i; }).filter(function(i){ return F[i].page; }).sort(function(a, b){ return (F[b].uses || []).length - (F[a].uses || []).length; }).slice(0, 6);
  var fns = FN.map(function(f, i){ return i; }).filter(function(i){ return docable(FN[i]) && !isTest(FN[i].f); }).sort(function(a, b){ return nCallers(b) - nCallers(a); }).slice(0, 6);
  var colls = Object.keys(D.colls).sort(function(a, b){ return D.colls[b].length - D.colls[a].length; }).slice(0, 4);
  function btn(id){ return '<button class="btn" data-start="' + esc(id) + '" style="margin:3px">' + esc(nlabel(id)) + '</button>'; }
  var el = document.getElementById('gempty');
  el.innerHTML = '<div class="card"><b>Start somewhere</b><div class="muted" style="margin:4px 0 8px">Search at the top left, or pick one. Then click items to see their connections and double-click to grow the graph.</div>' +
    (pages.length ? '<div class="muted" style="font-size:12px;margin-top:6px">Pages</div>' + pages.map(function(i){ return btn('F' + i); }).join('') : '') +
    (fns.length ? '<div class="muted" style="font-size:12px;margin-top:6px">Most used functions</div>' + fns.map(function(i){ return btn('f' + i); }).join('') : '') +
    (colls.length ? '<div class="muted" style="font-size:12px;margin-top:6px">Collections</div>' + colls.map(function(n){ return btn(nid('c', n)); }).join('') : '') + '</div>';
  el.onclick = function(e){ var b = e.target.closest('[data-start]'); if (b) focus(b.getAttribute('data-start')); };
}
function graphShow(id){
  graphInit();
  if (id) {
    if (exists(id)) focus(id);
    history.replaceState(null, '', '#/graph');
  }
  renderPanel();
  emptyState();
}

window.addEventListener('hashchange', route);
route();
})();
</script>
</body>
</html>
"""


COMMANDS = {
    "status": cmd_status, "build": cmd_build, "update": cmd_status, "find": cmd_find,
    "callers": cmd_callers, "callees": cmd_callees, "impact": cmd_impact, "path": cmd_path,
    "file": cmd_file, "uses": cmd_uses, "endpoints": cmd_endpoints, "collections": cmd_collections,
    "events": cmd_events, "storage": cmd_storage, "env": cmd_env, "globals": cmd_globals,
    "services": cmd_services, "versions": cmd_versions, "compare": cmd_compare, "report": cmd_report,
    "export": cmd_export, "docs": cmd_docs, "agent-help": cmd_agent_help,
}


def make_parser():
    S = argparse.SUPPRESS  # so an option given before the command is not reset by the sub-parser
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=S, help="repository root (default: git root of the current folder)")
    common.add_argument("--dir", default=S, help="where the map is stored (default: <repo>/.repofree or $REPOFREE_DIR)")
    common.add_argument("--json", action="store_true", default=S, help="machine-readable output")
    common.add_argument("--limit", type=int, default=S, help="max rows to print (0 = all; default 60)")
    common.add_argument("--no-refresh", action="store_true", default=S, help="skip the automatic refresh")
    common.add_argument("--quiet", action="store_true", default=S, help="no refresh notes on stderr")
    p = argparse.ArgumentParser(prog="repofree", parents=[common],
                                description="A self-refreshing map of a code repository, for people and AI assistants.",
                                epilog="Start with:  repofree status   |   repofree agent-help")
    p.add_argument("--version", action="version", version="repofree " + __version__)
    sub = p.add_subparsers(dest="cmd")

    def add(name, help_):
        return sub.add_parser(name, parents=[common], help=help_, description=help_)

    add("status", "what is mapped and when it was refreshed")
    add("build", "full rebuild of the map (normally never needed)")
    add("update", "refresh now and show status")
    s = add("find", "where a function/class is defined (exact, then fuzzy)")
    s.add_argument("name")
    s.add_argument("--kind", choices=["function", "method", "class", "export"])
    s = add("callers", "every place that calls NAME")
    s.add_argument("name")
    s.add_argument("--receiver", help="only calls made on this object, e.g. --receiver db")
    s = add("callees", "what NAME calls")
    s.add_argument("name")
    s.add_argument("--all", action="store_true", help="include library/builtin calls")
    s = add("impact", "blast radius: callers of callers, files and pages affected")
    s.add_argument("target", help="function name or file path")
    s.add_argument("--depth", type=int, default=3)
    s = add("path", "shortest call chain from one function to another")
    s.add_argument("src")
    s.add_argument("dst")
    s = add("file", "summary of one file")
    s.add_argument("path")
    s = add("uses", "everything that mentions TEXT (routes, collections, keys, imports, names)")
    s.add_argument("text")
    s = add("endpoints", "server routes and who calls them")
    s.add_argument("text", nargs="?")
    s.add_argument("--unmatched", action="store_true", help="HTTP calls with no route in this repo")
    s = add("collections", "database collections/tables: where read and written")
    s.add_argument("name", nargs="?")
    s.add_argument("--writes", action="store_true", help="only write sites")
    s.add_argument("--fuzzy", action="store_true", help="substring match on the name")
    s = add("events", "custom events: who fires, who listens")
    s.add_argument("name", nargs="?")
    s.add_argument("--all", action="store_true", help="include standard DOM events")
    for name, h in (("storage", "localStorage/sessionStorage/prefs keys"), ("env", "environment variables and secrets"),
                    ("globals", "window/globalThis assignments")):
        s = add(name, h)
        s.add_argument("name", nargs="?")
    add("services", "cloud/serverless function exports and route files")
    s = add("versions", "cache-busting ?v= versions on pages")
    s.add_argument("file", nargs="?")
    s.add_argument("--changed", action="store_true", help="changed files whose page references were not bumped")
    s.add_argument("--all", action="store_true", help="list every loaded file, not only issues")
    s = add("compare", "what exists under A but not under B")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--map", action="append", help="rename when comparing, e.g. --map admin=store (repeatable)")
    s.add_argument("--both-ways", action="store_true")
    add("report", "write .repofree/REPOFREE.md")
    s = add("export", "write one Markdown file to give to a chat assistant")
    s.add_argument("-o", "--output")
    s.add_argument("--max-kb", type=int, default=0, help="truncate the file to this size")
    s = add("docs", "write browsable docs (every function, page, route, collection) + an interactive graph")
    s.add_argument("-o", "--output", help="folder to write to (default: .repofree/docs)")
    s.add_argument("--missing", metavar="FOLDER", help="list every function with no comment under FOLDER")
    add("agent-help", "print the usage guide for AI assistants")
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    parser = make_parser()
    a = parser.parse_args(argv)
    for k, v in (("repo", None), ("dir", None), ("json", False), ("limit", 60), ("no_refresh", False), ("quiet", False)):
        if not hasattr(a, k):
            setattr(a, k, v)
    cmd = a.cmd or "status"
    if cmd == "agent-help":
        _, text = cmd_agent_help(None, a)
        print(json.dumps({"guide": GUIDE}) if a.json else "\n".join(text))
        return 0
    start = os.path.abspath(os.path.expanduser(a.repo)) if a.repo else os.getcwd()
    if not os.path.isdir(start):
        print("repofree: not a folder: %s" % start, file=sys.stderr)
        return 2
    root = git_toplevel(start) or start
    outdir = os.path.abspath(os.path.expanduser(a.dir)) if a.dir else default_outdir(root)
    repomap = None if cmd == "build" else load_map(outdir)
    fresh = repomap is None
    if fresh:
        repomap = empty_map()
    if not a.no_refresh or fresh:
        def prog(n):
            if not a.quiet and fresh and n % 200 == 0:
                print("repofree: mapped %d files..." % n, file=sys.stderr)
        changed, removed, meta_changed = refresh(repomap, root, force=(cmd == "build"), progress=prog)
        if changed or removed or meta_changed or fresh:
            save_map(repomap, outdir, root)
            ctx0 = Ctx(root, outdir, repomap)
            try:
                _atomic_write(os.path.join(outdir, "REPOFREE.md"), build_report(ctx0))
            except Exception as e:  # report is a convenience; never block a query on it
                print("repofree: could not write REPOFREE.md (%s)" % e, file=sys.stderr)
            if not a.quiet and (changed or removed):
                print("repofree: %s %d file(s)%s" % ("mapped" if fresh else "refreshed", changed,
                                                     (", %d removed" % removed) if removed else ""), file=sys.stderr)
    ctx = Ctx(root, outdir, repomap)
    data, text = COMMANDS[cmd](ctx, a)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1, default=lambda o: sorted(o) if isinstance(o, set) else str(o)))
    else:
        print("\n".join(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
