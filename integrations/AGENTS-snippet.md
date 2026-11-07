<!-- Paste the section below into AGENTS.md / CLAUDE.md / GEMINI.md / .github/copilot-instructions.md -->

## Code map (repofree)

This repo can be queried with `repofree` (or `python3 path/to/repofree.py`). The map refreshes
itself before every command (only changed files are re-read), so it always matches the files on disk.

Use it **when the user asks for it**, or when a question is about structure: who calls something,
what breaks if something changes, which pages load a script, which code calls an API route, or where
a database collection / storage key / event / env var is used. Prefer `--json` output.

- `repofree find NAME`: where it's defined (also says if it's called but never defined)
- `repofree callers NAME` / `repofree callees NAME`
- `repofree impact NAME_OR_FILE --depth 2`: blast radius, plus the pages that load affected code
- `repofree endpoints TEXT`: server routes and their callers (`--unmatched`: calls with no route)
- `repofree collections NAME [--writes]`: database read/write sites
- `repofree events|storage|env|globals [NAME]`
- `repofree versions --changed`: changed scripts/styles whose `?v=` wasn't bumped
- `repofree compare DIR_A DIR_B --map old=new`: what A has that B lacks
- `repofree docs [--missing FOLDER]`: offline docs site + interactive graph; lists functions with no comment
- `repofree agent-help`: full command list

Rules:
1. The map is for finding things. Open the real file at the given line before editing or stating facts.
2. It's pattern-based: calls through variables or computed names are invisible, so "no callers found" does not mean "safe to delete".
3. Never edit `.repofree/` by hand.
