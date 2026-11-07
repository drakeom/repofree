# Using repofree with AI assistants

repofree is a normal command-line tool, so any assistant that can run terminal commands can use it.
Assistants that can't run commands (chat apps) can read a one-file export.

The idea: the assistant asks the map **where** things are, then reads only those lines of real code.
That's faster and cheaper than reading whole files, and it finds wiring that text search misses
(which page calls which route, who listens to an event, where a collection is written).

**Nothing here forces the assistant to use the map on every step.** It uses it when you ask
("use repofree", "check the map") or when a question is structural. There are no hooks and no
blocking.

---

## 1. Claude Code

**Option A: skill (recommended).** Copy the skill folder:

```bash
# for you, in every project:
mkdir -p ~/.claude/skills && cp -r integrations/claude-code/repofree ~/.claude/skills/
# or only for one project:
mkdir -p .claude/skills && cp -r integrations/claude-code/repofree .claude/skills/
```

Claude Code loads it when you say things like "use repofree", "check the code map", "who calls X",
or "what breaks if I change Y". You can also type `/repofree`.

**Option B: a CLAUDE.md note.** Paste [the snippet](../integrations/AGENTS-snippet.md) into your
project's `CLAUDE.md`.

## 2. OpenAI Codex CLI, Cursor, Windsurf, Aider, Zed, and other `AGENTS.md` readers

Paste [the snippet](../integrations/AGENTS-snippet.md) into `AGENTS.md` at the repo root (create it if
needed). Codex and most agent tools read `AGENTS.md` automatically. For Cursor you can also put it in
`.cursor/rules/repofree.mdc`.

## 3. Gemini CLI

Paste the snippet into `GEMINI.md` at the repo root.

## 4. GitHub Copilot (agent mode in VS Code / JetBrains)

Paste the snippet into `.github/copilot-instructions.md`.

## 5. ChatGPT, Claude.ai, Gemini web, or any chat app

These can't run commands on your computer, so give them the export:

```bash
repofree export                       # writes .repofree/REPOFREE-export.md
repofree export --max-kb 800          # cap the size if your chat app has an upload limit
repofree export -o ~/Desktop/map.md   # write it somewhere else
```

Upload the file and ask your questions. The export contains the overview, every definition with its
line number, all routes with their callers, and every database-collection site. It's a snapshot,
so run `export` again after big changes. With ChatGPT's code-running tool you can also upload
`repofree.py` and a zip of the repo, and ask it to run the commands itself.

## Human-readable docs

`repofree docs` writes an offline website with a page for every function, file, API route and
collection, plus an interactive graph. It also lists functions with no comment. Ask your assistant
things like "run `repofree docs --missing src/checkout/` and write comments for the top ten,
based on reading each function". Then run `repofree docs` again to see them in the docs.

## 6. Anything else (scripts, CI, your own agents)

Every command supports `--json`:

```bash
repofree callers createOrder --json
repofree impact src/payments.js --json --depth 2
repofree versions --changed --json      # e.g. fail CI when a changed script's ?v= was not bumped
```

`repofree agent-help` prints a compact guide an agent can read at the start of a session.

---

## Good prompts

- "Use repofree to find every caller of `applyDiscount`, then read each call site and tell me which ones pass a percentage."
- "Before you change `src/cart.js`, run `repofree impact src/cart.js` and list the pages I should re-test."
- "With repofree, list every place the `orders` collection is written, and check each one sets `updatedAt`."
- "Run `repofree compare src/admin src/store --map admin=store` and tell me which admin features the store side is missing."
- "I edited some scripts. Run `repofree versions --changed` and bump any `?v=` I forgot."

## Ground rules to give your assistant

1. The map is for **finding**. Open the real file at the given line before editing or claiming anything.
2. "No callers found" means "none the map could see". Dynamic calls are invisible to it.
3. The map refreshes itself before every command. Never edit `.repofree/` by hand.
