# repofree

**A map of your code that keeps itself up to date, for people and AI assistants.**

Ask questions like *"who calls this function?"*, *"which pages break if I change this file?"*,
*"which screens call this API route?"* or *"where is this database collection written?"*, and get
`file:line` answers in a fraction of a second, without reading the whole codebase.

- **One file, no installs.** Python 3.9+ standard library only. Works on macOS, Linux and Windows.
- **Private.** Everything runs locally. No network, no API keys, no telemetry.
- **Always current.** Before every command it re-reads only the files that changed (edited, added or
  deleted), so answers match what's on disk. No watcher process, no git hooks.
- **Sees the wiring call graphs miss.** It also maps HTTP routes and the code that calls them,
  database collections (read vs write), custom events, browser storage keys, environment variables,
  `window` globals, HTML `onclick` handlers, the scripts each page loads, and `?v=` cache-busting.
- **Works with any assistant.** Claude Code, Codex, Cursor, Gemini CLI, Copilot and others can run it
  as a command. ChatGPT and other chat apps can use the one-file export.

```text
$ repofree callers applyDiscount
applyDiscount is defined at: server/checkout.js:212
4 call site(s):
  server/checkout.js
    :388    in POST /cart/checkout  -> applyDiscount()
    :455    in POST /orders/create  -> applyDiscount()
  web/js/cart.js
    :97     in CartPage.refreshTotals  -> applyDiscount()
    ...
```

---

## Install

Pick one:

```bash
# A) No install: run the single file
python3 repofree.py status

# B) Install the `repofree` command (from this folder or a git URL)
pipx install .            # or:  pip install --user .
repofree status

# C) Copy repofree.py anywhere on your PATH, make it executable, and call it `repofree`
```

You need Python 3.9 or newer (`python3 --version`). Nothing else.

## Quick start

From inside any repository:

```bash
repofree status                      # first run builds the map (seconds for most repos)
repofree find checkout               # where is something defined? (exact, then fuzzy)
repofree callers applyDiscount       # every place that calls it
repofree impact src/cart.js          # blast radius: callers of callers + pages affected
repofree endpoints /orders           # server routes and the frontend code that calls them
repofree collections orders          # where a DB collection is read and written
```

Add `--json` to any command for machine-readable output.

## Commands

| Command | What it answers |
|---|---|
| `status` | What's mapped, when it was last refreshed |
| `find NAME` | Where a function/class/method is defined. Also tells you when something is *called but never defined* |
| `callers NAME` | Every call site, including callbacks (`.then(fn)`, `addEventListener('x', fn)`, Express middleware) and HTML `onclick="fn()"` (even in HTML built inside JS strings) |
| `callees NAME` | What a function calls (`--all` includes library calls) |
| `impact NAME\|FILE` | Callers of callers (`--depth N`), the files involved, and the pages that load the affected code |
| `path FROM TO` | Shortest call chain between two functions |
| `file PATH` | One file: definitions, imports, routes, collections, events, who loads it |
| `uses TEXT` | Anything mentioning TEXT: routes, collections, storage keys, imports, names |
| `endpoints [TEXT]` | Server routes and their callers; `--unmatched` lists HTTP calls with no route in the repo |
| `collections [NAME]` | Database collections/tables, read vs write sites, and reads with no `.limit()` seen |
| `events [NAME]` | Custom events: who fires them, who listens, and which are never fired or never heard |
| `storage [KEY]` | `localStorage` / `sessionStorage` / `AsyncStorage` / Flutter prefs keys |
| `env [NAME]` | Environment variables and secrets used |
| `globals [NAME]` | `window.*` / `globalThis.*` assignments |
| `services` | Exported cloud/serverless functions (Firebase, etc.) with triggers, and which files define routes |
| `versions [FILE]` | `?v=` cache-busting: files loaded with different versions, or with no version |
| `versions --changed` | Files you changed whose page references were **not** bumped (uses git) |
| `compare A B` | What exists under folder A but not under folder B: functions, routes, collections, events, storage keys, globals. `--map old=new` renames while comparing, e.g. `--map admin=store` |
| `report` | (Re)writes `.repofree/REPOFREE.md`, a readable overview of the codebase |
| `export` | Writes one Markdown file you can upload to a chat assistant |
| `docs` | Writes browsable docs and an interactive graph (see below). `--missing FOLDER` lists every function in that folder that has no comment |
| `build` | Full rebuild (you normally never need this) |
| `agent-help` | Prints the short guide for AI assistants |

Shared options (before or after the command): `--repo PATH`, `--dir PATH`, `--json`, `--limit N`
(`0` = no limit), `--no-refresh`, `--quiet`.

## Documentation and interactive graph

```bash
repofree docs                         # writes .repofree/docs/ - open index.html in a browser
repofree docs --missing src/payments/ # every function in that folder that has no comment
repofree docs -o ~/Desktop/mydocs     # write it somewhere else
```

You get a small offline website (no server, nothing loaded from the internet):

- **A page for every function:** its purpose, signature, parameters, who calls it, what it calls,
  inner functions, database collections it reads or writes, API routes it calls or handles, events,
  storage keys, env vars, and the pages that load it.
- **A page for every file and page:** what it loads, who loads it, its definitions, and the API
  routes defined in it.
- **Pages for API routes, collections, events and storage keys,** all cross-linked, with search
  (press `/`).
- **An interactive graph (`graph.html`, or the Graph tab):** pick a function, page, route or
  collection, then click to see its connections and double-click to grow the graph. You can filter
  by type, drag, zoom and jump to the docs page.
- **"Open in editor" links** for VS Code or Cursor, which you can switch off.

**Where "purpose" comes from: your comments, never an AI.** repofree reads the comment directly
above each function (JSDoc `/** ... */` with `@param` and `@returns`, `//` lines, `#` lines) and
Python docstrings. One blank line between the comment and the function is fine. Section headers
(`// ---- Payments ----`), tool directives (`eslint-disable`, `@ts-ignore`) and commented-out code
are not treated as descriptions.

Functions with no comment are marked "No description" in the docs and listed in
`missing-comments.md` and in the terminal, most-used first, so you know where adding a comment
helps most. Test files are listed last. Add the comments, run `repofree docs` again, and the count
goes down.

The docs are a snapshot: run `repofree docs` again after changes (it takes a second or two).
They contain your code's structure and comments, so share them only as you would the code.

## How "always up to date" works

The map is stored in `.repofree/map.json`. On **every** command repofree:

1. lists the repo's files (with `git ls-files`, so `.gitignore` is respected; or a folder walk when there's no git),
2. compares each file's size and modified time with the map,
3. re-reads only new or changed files and drops deleted ones,
4. saves the map (atomically, so several terminals or assistants can use it at once).

A typical query takes around 0.2 s; the first build of a ~300-file repo takes about 10 s.
`.repofree/` contains its own `.gitignore`, so it never shows up in `git status` and can't be
committed by accident. To keep the map outside the repo, use `--dir ~/somewhere` or set
`REPOFREE_DIR`.

## Using it with AI assistants

See **[docs/ai-assistants.md](docs/ai-assistants.md)** for copy-paste setup for Claude Code, Codex,
Cursor, Gemini CLI, GitHub Copilot, ChatGPT and others. In short:

- **Assistants that can run commands** (Claude Code, Codex, Cursor, Gemini CLI, Copilot agent mode):
  add the snippet from [integrations/AGENTS-snippet.md](integrations/AGENTS-snippet.md) to your
  `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`, or install the Claude Code skill from
  [integrations/claude-code/](integrations/claude-code/). The assistant then runs
  `repofree ... --json` when *you* ask for it or when it's useful. Nothing is forced on every step.
- **Chat apps** (ChatGPT, Claude.ai, Gemini web): run `repofree export` and upload
  `.repofree/REPOFREE-export.md`.

## Accuracy and limits (please read)

repofree reads code with a careful pattern scanner. Comments and string contents are recognised,
so commented-out code and text inside strings don't count. Python uses Python's real parser. It's
designed to make **finding** things fast and reliable. It is **not** a compiler or a proof:

- Calls made through variables or computed names (`handlers[name]()`, `obj[method]()`), or functions
  passed around under another name, are not visible. **"No callers found" means "none found by the
  map", not "safe to delete".**
- Functions with the same name in different files are told apart by what can see what (imports,
  and which scripts share a page). That's very good but not perfect; the output says which
  definition a call resolves to.
- Route ↔ caller matching uses URL path text (plus the service name when the URL contains it).
  URLs assembled at runtime from several variables may not match.
- Database read/write is judged from the call chain on the same statement (`.get()`, `.set()`,
  `getDocs(...)`, ...). A reference built in one place and used in another shows as `ref`.

Always open the file at the line given before you change code or make a claim.

## Language support

| Tier | Languages | What you get |
|---|---|---|
| Full | JavaScript, TypeScript, JSX/TSX, HTML (inline scripts, handlers, script/style tags), Vue/Svelte script blocks, Python | Definitions with nesting (`Class.method`), anonymous route/listener handlers named after what they handle, calls, callbacks, imports, all facts |
| Basic | Go, Rust, Java, Kotlin, Scala, C#, PHP, Ruby, Swift, Dart, C, C++ | Definitions, calls, imports, routes, collections, env, storage, events |

Other files (CSS, images and so on) are not read but are still known, so page links to them resolve.
Minified/bundled files and files over 3 MB are skipped.

## Configuration

- `.repofreeignore` in the repo root: one glob per line (`docs/*`, `legacy`, `*.generated.js`).
- Always skipped: `node_modules`, `.git`, `dist`, `build`, `vendor`, virtualenvs, caches and similar.
- `REPOFREE_DIR`: store maps somewhere else.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
