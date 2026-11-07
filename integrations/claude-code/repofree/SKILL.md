---
name: repofree
description: Query a self-refreshing code map of the current repository with the repofree command, or generate its docs site and interactive graph. Use when the user asks to use repofree, the code map, the code docs or graph, or which functions lack comments, or asks structural questions - who calls a function, what breaks if a file or function changes, which pages load a script, which code calls an API route, where a database collection, storage key, custom event or env var is used, which ?v= cache-busting versions were not bumped, or what one folder has that another lacks.
---

# repofree: code map

`repofree` maps the repo's functions, calls, imports, page→script links, HTTP routes and their
callers, database collections (read/write), custom events, storage keys, env vars, globals and
`?v=` versions. It refreshes itself before every command, so results match the files on disk.

## Running it

Try in this order and use the first that works:
1. `repofree <command> --json`
2. `python3 ~/.claude/skills/repofree/repofree.py <command> --json` (if the user copied the file there)
3. Ask the user where `repofree.py` is.

Run from inside the repository (or pass `--repo PATH`). The first run builds the map (seconds);
later runs take about 0.2 s.

## Commands

| Need | Command |
|---|---|
| Where is X defined? | `repofree find X` |
| Who calls X? | `repofree callers X` |
| What does X call? | `repofree callees X` |
| What breaks if I change X / file F? | `repofree impact X --depth 2` / `repofree impact path/to/file.js` |
| Call chain A → B | `repofree path A B` |
| Summary of one file | `repofree file path/to/file` |
| Anything mentioning TEXT | `repofree uses TEXT` |
| API routes + their callers | `repofree endpoints [TEXT]`, `--unmatched` |
| DB collection read/write sites | `repofree collections [NAME] [--writes]` |
| Events / storage keys / env / globals | `repofree events`, `storage`, `env`, `globals` |
| Cloud/serverless functions | `repofree services` |
| Cache-busting problems | `repofree versions`, `repofree versions --changed` |
| Parity between two folders | `repofree compare A B --map old=new` |
| Browsable docs + interactive graph | `repofree docs` (then open `.repofree/docs/index.html`) |
| Functions with no comment | `repofree docs --missing FOLDER` |

## Rules

1. Use the map to **find** things, then Read the real file at the given line before editing code or
   stating anything as fact. Quote `file:line` from the real file, not just from the map.
2. It's pattern-based: calls through variables or computed names are invisible. Never say "unused" or
   "safe to delete" based only on "no callers found". Say "no callers found by the map" and check further.
3. When a name is defined in several files, `callers` shows which definition each call resolves to
   (`[-> file]`). Check that before trusting a count.
4. Never edit anything in `.repofree/`.
5. Only use the map when the user asks or when the question is structural. Don't run it before every
   edit by default.
