# PairDown feasibility spike

Throwaway spike. It checks two technical risks before any product code is written:

- Risk A: a native Hermes Desktop pane can call a Python `plugin_api.py` backend through `ctx.rest`, and that backend can read a local vault with `pathlib` only, the same way on Windows, Linux and macOS.
- Risk B: a CodeMirror 6 editor can be bundled into a single `desktop/plugin.js` that the Hermes Desktop disk-plugin loader accepts. Disk plugins may only import `@hermes/plugin-sdk`, `react` and `react/jsx-runtime`.

## Verdict

Both risks hold up in every check that can be run without the real Desktop, under the threat model below. Nothing in this report is a claim about the real Desktop, the real gateway or macOS: those were not run (see "Not proven").

Two independent reviews failed earlier versions of this spike. This report lists every finding with its actual status:

| Id | Finding | Status |
|---|---|---|
| F1 | `/read` checked containment on a pathname, then reopened it: a file swapped for an escaping symlink between the two leaked outside bytes | Fixed (round 1), confirmed by the round 2 review |
| F2 | `/list` followed NTFS junctions and symlinked folders that exist when the walk runs | Fixed (round 1) for links that exist at request time. The concurrent-replacement part is R2 |
| F3 | Ignored folder names matched case-sensitively on Windows | Fixed (round 1), confirmed by the round 2 review |
| F4 | The bundle import checker accepted computed and dead-branch imports | Fixed (round 1) for those forms. The loader-compatibility part is R3 |
| F5 | The report overclaimed (checker claims, "all fixed") | Superseded: this document is the rewrite required by R4 |
| R1 | A hard link inside the vault to an outside file is served by `/read` | Out of scope by owner decision (owner approval, 2026-10-19). Not fixed, not defended |
| R2 | `/list` follows a queued folder that is replaced by an outside link during the walk | Out of scope by owner decision (owner approval, 2026-10-19). Not fixed, not defended |
| R3 | The checker accepted import spellings the Desktop loader neither accepts nor rewrites (escaped literal, comment-separated dynamic `import()`) | Fixed (round 2): the Desktop's own loader is now the decisive gate |
| R4 | The report claimed closure that the evidence did not support | Fixed (round 2): this rewrite |

Remaining defects: none known inside the approved threat model. Open work is listed under "Not proven" and in the follow-ups at the end of the "Review round 2" section.

## Backend threat model (approved)

The project owner approved this threat model for `dashboard/plugin_api.py` (owner approval, 2026-10-19). It is what the code is built and tested against, and nothing wider is claimed.

In scope: everything a request can attempt through its parameters.
- `../` segments, absolute paths, drive letters, UNC paths, URL-encoded spellings, NUL and other malformed names.
- Symlinks and NTFS junctions that exist at the time of the request, in the file or in any parent folder.
- A file or parent folder replaced between the check and the read (F1).
- Ignored folders (`.obsidian`, `.trash`, `node_modules`) with filesystem-appropriate case (F3).
- In the product, reads will also be limited to configured vault roots. That is a follow-up, not part of this spike: `root` is still caller-supplied here.

Out of scope: an attacker who can already write inside the vault under the user's own account.
- R1, a hard link inside the vault to a file outside it.
- R2, replacing a directory with a link while a listing is running.

The backend runs as that same user, so such an attacker can already read the user's files without going through the plugin. Because of that these two cases are documented, not fixed. The code comments say the same (`dashboard/plugin_api.py` module docstring and `list_notes` docstring).

## Layout

```
plugin.yaml                  name: pairdown
dashboard/manifest.json      { "name": "pairdown", "api": "plugin_api.py", tab hidden }
dashboard/plugin_api.py      router = APIRouter(); /health /validate /list /read
desktop/plugin.js            built artifact (committed, reproducible)
desktop/src/                 index.jsx (registration), pane.jsx (pane), editor.js (CodeMirror + decoration)
scripts/                     build.mjs, check-bundle.mjs (+ bundle-imports.mjs, desktop-loader.mjs, check-bundle.test.mjs), load-harness.mjs, externals.mjs
tests/test_plugin_api.py     pytest + FastAPI TestClient
```

Reference versions: Hermes `v0.22.2` (Windows host; the read-only checkout used for the loader is commit `d923a5eb487a17c0a68801bba9ef0f56c81040a8`), Node 24.13.0 (Linux) and 26.7.0 (Windows), Python 3.10.12 and 3.14.8 on Linux, Python 3.14.7 on Windows, uv 0.13.1 (Linux) and 0.12.17 (Windows), esbuild 0.28.2, acorn 8.19.0 / acorn-walk 8.3.5, CodeMirror `@codemirror/view` 6.43.15 / `state` 6.7.6 / `lang-markdown` 6.5.2 / `language` 6.13.1.

## How Hermes mounts and reaches the backend (read from the v0.22.2 sources)

These are code reads, not runs.

- Discovery: `hermes_cli/web_server_dashboard.py::_discover_dashboard_plugins` scans `<plugins root>/*/dashboard/manifest.json`. `api` must be a relative path inside `dashboard/` (`_safe_plugin_api_relpath`).
- Gate: a user plugin's `plugin_api.py` is imported only if the plugin is in `plugins.enabled` and not in `plugins.disabled` (`_plugin_api_mount_skip_reason`). Enabling the pane in Desktop Capabilities does not import Python.
- Mount: `_mount_one_plugin_api` loads the file with `importlib.util.spec_from_file_location` under the module name `hermes_dashboard_plugin_<name>`, then `app.include_router(router, prefix="/api/plugins/<name>")`. `tests/test_plugin_api.py` loads the backend the same way and mounts it under the same prefix.
- Startup versus hot mount: the SDK doc says to restart the gateway after enabling. The code also has a first-request hot mount (`_PluginApiHotMount`, `_try_hot_mount_plugin_api`) for plugins enabled after start. Not tested here. The operator steps follow the documented restart.
- Auth: `ctx.rest(path)` becomes `hermesApi({ path: '/api/plugins/<id>' + path, ...profileScoped() })` (`apps/desktop/src/api/plugins.ts`) over the Desktop's `window.hermesDesktop.api` bridge. On the backend the request passes the host-header check, the OAuth gate, then `auth_middleware`, which requires the session token on every `/api/` path (`hermes_cli/web_server.py`). A request without it gets 401 before the plugin-enabled gate runs. `ctx.rest` rejects `..` in the path. How the bridge attaches the token was not traced.
- Loader: `apps/desktop/src/contrib/runtime-loader.ts` scans the whole source for import specifiers with a regular expression, honours a match only where its own lexer says the text is code (not a string, template text, comment or regex literal), refuses anything except the keys of the SDK import map, rewrites the allowed ones to shim blob URLs, and `import()`s the result with a 10 s evaluation deadline. The shims re-export the app's live React and SDK, so React is a singleton. Exact rules: see "Import gate".

## Risk A: backend

### What was proven

`dashboard/plugin_api.py` is pure `pathlib`/`os`/stdlib plus FastAPI. There is no `subprocess` and no shell (a test parses the source and checks this). One helper uses `ctypes` on Windows to read the final path of an open handle.

| Route | Behaviour |
|---|---|
| `GET /health` | `{ok, plugin, python, platform}` |
| `GET /validate?root=` | `{root, exists, valid}`; `valid` is true only if `<root>/.obsidian` is a directory; relative root gives 400 |
| `GET /list?root=` | sorted relative POSIX paths of `*.md` (case-insensitive suffix); prunes `.obsidian`, `.trash`, `node_modules`; does not enter a symlinked or junctioned folder that exists when it looks; lists a symlinked file only if it resolves inside the root and outside ignored folders |
| `GET /read?root=&path=` | UTF-8 text read as bytes, so CRLF is preserved; 415 on invalid UTF-8; `.md` only; refuses ignored folders |

Path safety on `/read` has three layers. First a lexical check rejects absolute paths, `\` or `/` prefixes (UNC), drive letters and `:` (also blocks NTFS alternate data streams), NUL, `*?"<>|`, empty, `.` and `..` segments, segments ending in space or dot, and (on Windows) reserved device names such as `CON.md`. `\` is normalised to `/`. Ignored folder names are matched case-insensitively on Windows and macOS and case-sensitively on Linux.

Second, the note is opened, and the real path of the file that was actually opened is read back from the open handle: `/proc/self/fd/<n>` on Linux, `fcntl(F_GETPATH)` on macOS, `GetFinalPathNameByHandleW` on Windows (via `ctypes`, called only in one helper). That real path must be inside the resolved root and must not pass through an ignored folder, otherwise the response is 403 and no byte is read. The check is bound to the opened file, not to a pathname that is looked up again, so a file or a parent folder swapped for a link at any moment during the request cannot change what is returned. If the real path cannot be obtained (for example `/proc` is not mounted) the route fails closed with 501. Status codes: 400 lexical, 403 ignored folder or escape, 404 missing, 415 not UTF-8, 501 cannot verify.

What this guarantee covers: the bytes returned by `/read`, for any link, junction or replacement that is a path link. What it does not cover is stated next.

### Out of scope under the approved threat model: R1 and R2

Both were found by the round 2 reviewer, who reproduced them on Linux and native Windows against the unmodified backend. The owner decided on 2026-10-19 (owner approval, 2026-10-19) that they are out of scope. No code was changed for them, the protections that exist stay, and this report does not claim that `/read` or `/list` is safe against them. I re-ran the reviewer's `extended_probes.py` against this branch's backend on Linux while writing this report; the outputs below are unchanged by this round.

R1, hard link. The probe creates `outside/secret.md`, then `os.link(secret, root/'hard.md')`, then calls `/read` with `path=hard.md`.

```
hard-link-identity:                 samefile=true, nlink=2
hard-link-to-sibling-outside-file:  status=200, content='OUTSIDE ROOT SENTINEL'
hard-link-to-ignored-folder:        status=200, content='IGNORED SENTINEL'
```

A hard link is a second name for the same file. Its in-vault pathname resolves inside the root, so no path-based check can see that the file is also reachable from outside. Detecting it would need a link-count policy, which the owner chose not to require. Whoever can create the link needs write access to the vault, and the same user could read the target directly.

R2, folder replaced during a listing. The probe pauses just before the real `scandir` of an inspected sub-folder, renames that folder away and puts a symlink (Linux) or junction (Windows) to an outside folder in its place, then lets the walk continue.

```
coordinated-parent-swap-list:  status=200, files=['safe.md', 'sub/outside-name.md', 'sub/secret.md']
list-swap-coordination:        fired=1, errors=[]
```

`outside-name.md` exists only in the outside folder, so `/list` reported a name from outside the vault. `/list` returns names, not contents, and `/read` of those names is still refused (the probe's `coordinated-parent-swap-read` returns 403 and `nested-directory-link-read` returns 403). The same user needs write access inside the vault to do this.

Other results from the same probe, run on both systems by the reviewer and on Linux here: a nested outside directory link is omitted by `/list` and refused by `/read`; a coordinated parent swap before `/read` opens is refused with 403.

### Tests

`tests/test_plugin_api.py`, 67 tests against a temp fixture vault. They cover:
- validate true, false, `.obsidian` as a file, missing root, relative root, missing parameter;
- list with nested folders, an upper-case `.MD`, and `.obsidian`/`.trash`/`node_modules` skipped;
- read of accents and emoji, CRLF byte-exactness, a name with spaces, `'` and `&`, backslash separators, invalid UTF-8;
- 23 traversal and malformed-path variants: `../`, `..\`, `..%2f` and `%2e%2e/` (sent raw as well), UNC, `C:\` and `C:/`, `c:x`, `::$DATA`, NUL, trailing space and dot, an absolute path that points at a real secret file;
- symlinked file and directory escaping root (403), an inside-root symlink (allowed), Windows reserved device names, root with forward slashes or a trailing separator;
- race regressions: a file swapped for an escaping symlink while requests run, a parent folder swapped for an escaping link while requests run, and three deterministic cases that swap exactly before or after the server opens the file;
- listing regressions for links that exist at request time: a folder link cycle, a folder link pointing outside, a folder link staying inside, and on Windows a real NTFS junction pointing outside and a junction cycle;
- case regressions: mixed-case `Node_Modules`, `.Obsidian`, `.TRASH` (listed on Linux, excluded on Windows) and mixed-case spellings of existing ignored folders (403 on Windows), plus an 8.3 short-name alias of `node_modules` on Windows.

There is no test for R1 or R2, because they are out of scope. The static symlink tests are not a containment proof; the race tests plus the design argument above are what back the `/read` claim, within the limits below.

Three tests are platform-specific and are reported as skipped on Linux (8.3 short names, junction outside, junction cycle). They run on Windows.

Commands and real results at this branch's head (the last line of each run):

```
# Linux (WSL), Python 3.10.12, venv from uv.lock
uv run --locked --python 3.10 pytest
  -> 64 passed, 3 skipped, 1 warning in 9.78s      (the 3 skips are the NTFS-only tests)

# Linux (WSL), Python 3.14.8
uv run --locked --python 3.14 pytest
  -> 64 passed, 3 skipped, 1 warning in 9.70s

# Native Windows, Hermes' bundled Python 3.14.7, uv on Windows, tree copied to a Windows scratch dir
uv run --locked --python <hermes-tools>/python-3.14.7*/python.exe pytest
  -> 67 passed, 1 warning in 9.78s                 (nothing skipped)

uvx ruff check dashboard tests   -> All checks passed!
```

The one warning is Starlette saying `httpx` with `TestClient` is deprecated in favour of `httpx2`. It affects tests only. The race tests each spend about 4 s hammering requests, so a full run is about 10 s.

macOS was not run. The macOS branch of the opened-file check (`F_GETPATH`) is written but has never executed. The rest of the backend is not platform-specific, but "identical on macOS" is inferred, not shown.

The reference backend (hermes-obsidian-memory `dashboard/api.py`, commit `11239984`) was read for comparison. It scans a fixed vault from an env var with `rglob` and `read_text`, with no root parameter and no containment check. It also reports `str(rel)`, which is backslashed on Windows, and `read_text` would turn CRLF into LF. This spike does not copy it. It uses a `scandir` walk that does not enter links, `as_posix()` and byte-exact reads.

### Not proven (Risk A)

- A real gateway importing `plugin_api.py` through `_mount_one_plugin_api`, with the real middleware stack. The test app only mirrors that mount.
- `ctx.rest` from a real Desktop reaching `/api/plugins/pairdown/*` on the Windows backend, and the session-token / profile handling on that call.
- The effect of `plugins.enabled` and the gateway restart on the live Windows install, and whether the first-request hot mount makes the restart unnecessary.
- Behaviour with `plugins.isolation: host` (requests are forwarded to a plugin host, responses buffered). Not read in depth.
- macOS execution, including the `F_GETPATH` branch of the opened-file check.
- Containment is tested, not proven. The race tests drive a high-rate swapper against the real route for a few seconds; they show that the unfixed code leaks and the fixed code never did in the recorded runs, they do not prove the absence of every interleaving. Other filesystems were not tried: network shares, FUSE, OneDrive placeholders, case-sensitive NTFS folders, ReFS, and Windows volumes with 8.3 names switched off (the alias test skips there).
- R1 and R2 are not defended (out of scope, above).
- A vault that contains a symlinked folder is listed without that folder's notes although `/read` serves them if they resolve inside the root.
- Security design is not final. `root` is caller-supplied, so any authenticated client can list or read `.md` files under any absolute directory the gateway user can read. The product must restrict roots to a configured allow-list.
- Large vaults: `/list` walks synchronously with no cache or pagination. Not measured.

## Risk B: editor bundle

### What was proven

`desktop/src` registers one pane through `@hermes/plugin-sdk` (`ctx.register({ area: PANES_AREA, ... })`). The pane has a root `<input>` and a "Check" button. The button calls `ctx.rest('/health')` and `ctx.rest('/validate?root=...')` and shows the JSON. The pane also mounts a CodeMirror 6 editor (`@codemirror/state`, `view`, `lang-markdown`, plus `language` for `syntaxTree`) with sample Markdown. The live-preview decoration walks the Markdown syntax tree and hides the `**` marks of every `StrongEmphasis` node that does not touch a selected line.

- Bundle size: 474,937 bytes minified. The build is deterministic: rebuilding on Linux (Node 24.13.0) and on native Windows (Node 26.7.0) at this head gave the identical sha256 `844a6a9b15f158dd4c9c28f6d37d8bce99b83667f4137827832e5b73e8c194c4`, equal to the committed file. Supplemental sizes, measured with these exact commands on the committed file: `gzip -c desktop/plugin.js | wc -c` gives 164,401 bytes (gzip level 6); Node `zlib.gzipSync(readFileSync('desktop/plugin.js')).length` with default options gives 166,422 bytes; the same build settings as `scripts/build.mjs` with `minify: false` (banner included) give 877,240 bytes unminified.
- Where the bytes go (minified): `@codemirror/view` 152 KB, `@lezer/javascript` 78 KB, `@codemirror/state` 47 KB, `@lezer/markdown` 36 KB. `@lezer/css`, `@lezer/html` and the `lang-html`/`lang-css`/`lang-javascript` wrappers add roughly 70 KB more. `@codemirror/lang-markdown` pulls those in for fenced-code and HTML support. Our code is 2.7 KB. (Per-package figures date from the first version of this report and were not re-measured.)
- The committed bundle contains no dynamic `import()`, no `import.meta`, no `require()`, no `eval` and no `new Function`: it has exactly three static imports, the allowed ones, each written the way the Desktop loader recognises (`from"react"` style, no escapes, no comments).

### Import gate

`npm run check` (`scripts/check-bundle.mjs`) runs two gates on `desktop/plugin.js` exactly as shipped, with no re-bundling and no optimisation, so dead code is still seen.

1. Gate 1, parser pre-check (`scripts/bundle-imports.mjs`). The file is parsed with acorn as an ES module. It is fast, needs only this repository, and is deliberately stricter than the loader.
2. Gate 2, the Desktop's own loader, decisive (`scripts/desktop-loader.mjs`). `unsupportedImports` and `rewriteSpecifiers` are cut out of `apps/desktop/src/contrib/runtime-loader.ts` in a hermes-agent checkout named by `HERMES_AGENT_DIR`, transpiled in memory and run on the bundle text. The checkout is only read. The bundle passes only if `unsupportedImports()` returns nothing and, after `rewriteSpecifiers()`, every import the parser sees has been rewritten to one of the shim URLs and the import count did not change. The second condition catches what `unsupportedImports()` cannot: an import the loader never recognised is not refused by it and not rewritten, so it would reach the browser still naming `react`.

`HERMES_AGENT_DIR` is mandatory. Without it `check-bundle.mjs` and the harness exit 1. `--no-loader` (and `npm run check:fast`) runs gate 1 alone and prints `skip - --no-loader`. A bundle that passes gate 1 alone has not been shown to load.

Exact forms. "Loader-recognised" means the text just before the string literal matches `from\s*`, `import\s*\(\s*` or `import\s+`, and the literal is `'…'` or `"…"` with at least one character, no quote inside and no escape sequence, so the raw text equals the decoded value. Those are the rules of the loader's regular expression `(from\s*|import\s*\(\s*|import\s+)(['"])([^'"]+)\2`.

| Form | Result |
|---|---|
| `import a from "react"`, `import {a} from "react"`, `import * as a from "react"`, `import "react"`, `export * from "react"`, `export {a} from "react"`, `import("react")` with an allowed specifier (`@hermes/plugin-sdk`, `react`, `react/jsx-runtime`), single or double quotes, loader-recognised spelling | accepted |
| minifier spelling `from"react"`, `import ( "react" )`, newlines inside `import(\n"react")` | accepted |
| a name not on the list: bare (`left-pad`), relative (`./a.js`), URL (`https://…`), `react/jsx-dev-runtime`, in `import`, `export … from` or `import()`, including inside `if (false)`, `0 && …` or a function nobody calls | rejected |
| `import(expr)` where the argument is not a plain string literal: variable, concatenation, template literal (even without `${}`) | rejected, "computed dynamic import" |
| an allowed name written with an escape: `"re\u0061ct"`, `"\x72eact"`, `"re\u{61}ct"`, also in `import()` | rejected (gate 1: "spelled in a way the Desktop loader does not recognise"; gate 2: the loader refuses the raw text `re\u0061ct`) |
| an allowed name with a comment before it: `import/*c*/("react")`, `import(/*c*/"react")`, `from /*c*/ "react"`, `from // c<newline> "react"` | rejected (gate 1 as above; gate 2: the loader does not rewrite it) |
| `import"react"` (no whitespace after `import`) | rejected: the loader's `import\s+` needs whitespace, so it is never rewritten. `from"react"` is accepted because `from\s*` allows none |
| `import.meta`, `require(...)`, `eval(...)`, `new Function(...)` / `Function(...)` | rejected |
| no default export, a file that does not parse as an ES module, a missing file | rejected |
| import-looking text inside strings and comments | accepted (it is not an import; the Desktop loader also ignores it) |

The allowlist is the loader's import map without `react/jsx-dev-runtime`; the harness asserts that the checker allowlist is a subset of the Desktop's `sdkImportMap`.

Limits. This is a lint of the shipped file, not a sandbox. Code that builds a module reference at run time through paths the parser cannot see (for example `globalThis["ev" + "al"](...)`, or `Reflect.get`-style indirection) is not detected; a bundle with such code would be caught by review, not by this check. `require`, `eval` and `Function` are matched by name as call targets. The decisive gate is only as current as the checkout in `HERMES_AGENT_DIR`: a different Desktop build could have a different loader. If the loader source layout changes, the extraction fails loudly instead of passing. The gate does not execute the bundle; evaluation happens in the harness, with stubs.

Controls. `npm test` runs `scripts/check-bundle.test.mjs`, 49 `node:test` cases, each through the real command line, with `HERMES_AGENT_DIR` set:
- accepted: the three allowed static imports, a literal `import("react")`, `from"react"`, single quotes, whitespace in `import()`, a side-effect import of an allowed name, import-looking text in strings and comments, and the committed `plugin.js`;
- rejected, forms from earlier rounds: bare, relative and URL imports, export-from, `react/jsx-dev-runtime`, literal URL and bare dynamic imports, computed, concatenated and template-literal dynamic imports, dead-branch, short-circuit and uncalled-function forms, `import.meta`, `require`, `eval`, `new Function`, missing default export, unparsable text, a missing file;
- rejected, new in round 2 (R3): the escaped allowed literal (`\u`, `\x`, `\u{}`, static and dynamic), the comment-separated dynamic `import()` (before the parenthesis and after it), a comment between `from` and the specifier (block and line), and `import"react"`;
- the Desktop loader gate (7 cases, skipped when `HERMES_AGENT_DIR` is unset): the real loader's raw behaviour for the escaped literal (`unsupportedImports()` returns `['re\\u0061ct']`) and for the comment-separated import (`unsupportedImports()` returns `[]` and `rewriteSpecifiers()` returns the text unchanged), the gate alone rejecting a case, the gate accepting what the loader rewrites, the command rejecting both R3 controls with the loader messages, and the command accepting the committed bundle;
- the loader is mandatory by default: without `HERMES_AGENT_DIR` the command exits 1, and `--no-loader` is reported as a skip.

Every rejected case also asserts that the success message is never printed.

Headless load (`scripts/load-harness.mjs`). This is not the real Desktop. It uses stub modules for the three allowed imports, jsdom instead of Electron, and a recording `ctx`. The Desktop's own `unsupportedImports` and `rewriteSpecifiers` (read-only) are applied to the minified bundle. That is stronger evidence than a re-implementation, because the real lexer copes with minified CodeMirror (regex-literal and string heuristics).

```
HERMES_AGENT_DIR=<hermes-agent checkout> npm run harness
ok - checker allowlist is a subset of the Desktop SDK import map (@hermes/plugin-sdk, react/jsx-dev-runtime, react/jsx-runtime, react)
ok - checker and loader gate both reject all 12 samples (bare, relative, URL, export-from, dynamic, dead branch, escaped literal, comment-separated, no whitespace)
ok - computed import(x): the Desktop loader does not see it, the checker rejects it
ok - Desktop runtime-loader import allowlist accepts the bundle (unsupportedImports() returned [])
ok - Desktop runtime-loader rewriteSpecifiers() rewrote every import of the bundle to a shim module
ok - bundle evaluated without throwing (40 ms)
ok - register(ctx) called ctx.register({ id: "pane", area: "panes", title: "PairDown" })
ok - render() returned a React element
ok - CodeMirror mounted; ** markers hidden on the inactive line and kept on the active line
ok - moving the cursor swaps which line shows its ** markers (decorations update on selection)
ok - pane called ctx.rest("/health", "/validate?root=%2Ftmp%2Fexample%20vault") and rendered the JSON result
ok - no errors reported by jsdom while the pane ran (including animation-frame measure passes)
harness passed (stubbed modules + jsdom; not the real Hermes Desktop)
```

Negative controls for the editor: swapping the hiding decoration for a plain mark made the harness fail on the "inactive line must hide its ** markers" assertion.

### New finding: catalog admission lint

`hermes_cli/plugin_validate_desktop.py` (used by `hermes plugins validate` and catalog admission, not by the local disk loader) scans `desktop/plugin.js` for "prototype patching" with the regex `X.prototype.y = ...`. It was run read-only against this bundle: 19 findings, all from CodeMirror and Lezer's own class setup (for example `ye.prototype.startSide=ye.prototype.endSide=0`), nothing from our code. The round 2 review reproduced the 19 findings. Local loading is not affected. Getting into the catalog would need a build step that removes these assignments (for example `Object.assign(X.prototype, {...})`), an exemption for bundled dependencies, or a different editor. Treat it as a follow-up before any catalog submission. The finding came from a copy of that single function run in isolation; the full `hermes plugins validate` was not run.

### Not proven (Risk B)

- Loading in the real Desktop (Electron renderer, real blob-URL shims, real React 19.2.7 singleton, real SDK `PANES_AREA`, a real pane tile). The harness uses the real loader functions but stub modules and jsdom.
- CodeMirror layout in a real browser engine: scrolling, IME, selection by mouse, themes (`var(--ui-*)` variables are not used yet), focus handling inside a pane, the 10 s evaluation deadline on a cold machine. jsdom has no layout.
- Hot reload and `onDispose` behaviour (the editor is destroyed on unmount only; no `ctx.onDispose` hookup).
- That the SDK version and `PANES_AREA` export on the target Desktop match the checkout read here (`v0.22.2`), and that the target Desktop's loader matches the checkout's loader.
- Bundle size acceptability in the real app (475 KB, uncached on each hot reload).
- Windows execution of the real Desktop. The JS tooling (`npm ci`, build, check, tests, harness) did run on native Windows with Node 26.7.0 and produced the same bundle bytes; the Electron renderer was not run.

## Review history

### Review round 1: F1 to F5

An independent review of the first version returned FAIL. Each finding was reproduced by the reviewer on the unmodified code, then fixed. Every regression test below was run against the pre-fix `dashboard/plugin_api.py` (restored from commit `ff897d1`) and against the fixed one, with the same test file.

| # | Finding | Fix | Regression tests |
|---|---|---|---|
| F1 | `/read` resolved the path, checked it was under the root, then reopened it with `read_bytes()`. A file swapped for an escaping symlink between the two steps returned outside bytes on Linux and Windows. | Open first, then verify the real path of the opened handle (see "Path safety"). No second pathname lookup decides containment. | `test_read_never_returns_outside_bytes_while_the_file_is_swapped_for_a_symlink`, `..._while_a_parent_folder_is_swapped_for_a_link`, `test_file_replaced_by_an_escaping_symlink_just_before_open_is_refused`, `..._just_after_open_returns_only_the_opened_file`, `test_parent_folder_replaced_by_an_escaping_link_just_before_open_is_refused` |
| F2 | On Windows `os.walk(followlinks=False)` entered NTFS junctions, so `/list` enumerated files outside the root and could recurse through junction cycles. | `/list` uses its own `scandir` walk and does not enter a symlink or junction that exists when it looks (`st_reparse_tag` is the mount-point tag). Linked files are listed only if their real path is inside the root and outside ignored folders. | `test_list_does_not_enter_a_junction_to_an_outside_folder`, `test_list_terminates_on_a_junction_cycle` (Windows only), plus the link-cycle, outside-link and inside-link tests that run on every platform |
| F3 | Ignored names were matched case-sensitively: on Windows `Node_Modules/` was listed and `.OBSIDIAN/x.md` was readable. | One helper folds case on Windows and macOS and not on Linux, used by the walk pruning, the lexical read check and the real-path check. The real-path check also closes 8.3 aliases and links into ignored folders. | `test_ignored_folder_names_follow_the_filesystem_case_rules`, `test_read_refuses_mixed_case_spelling_of_existing_ignored_folders`, `test_read_refuses_short_name_alias_of_ignored_folder` (Windows only), `test_read_refuses_a_link_inside_root_that_points_into_an_ignored_folder` |
| F4 | `check-bundle.mjs` inspected the metadata of an optimised re-bundle, so `import(specifier)` and an import inside `if (false)` passed with exit 0. | Parse the shipped file with acorn; reject non-literal and non-allowlisted specifiers, dead or not. Loader alignment was incomplete: see R3. | `scripts/check-bundle.test.mjs`, 26 cases at the time |
| F5 | The report claimed the checker "fails on any other import and any dynamic import", which was false, and it did not record the platform defects. | Corrected in round 1, but the round 2 review found the closure claims still unsupported: see R4. | n/a |

Before and after (round 1; the "before" runs use the new tests with the old code):

```
# F1-F3, pre-fix backend, same test file
Linux  (WSL, Py 3.14):  5 failed, 59 passed, 3 skipped
  failed: file swapped for symlink (race); file swapped just before open; file swapped just after open
          (the hook never fires, the old code does not go through os.open); parent swapped just before open;
          link into an ignored folder served (200 instead of 403)
Windows (Py 3.14.7):   10 failed, 57 passed
  failed: the 5 above, plus mixed-case ignored folders (list and read), 8.3 alias of node_modules,
          junction to an outside folder listed, junction cycle (list took more than 10 s)
# F1-F3, fixed backend
Linux  Py 3.10 / 3.14:  64 passed, 3 skipped (the 3 NTFS-only tests)
Windows Py 3.14.7:      67 passed
```

Race evidence in more detail. The swapper thread replaces `flip.md` over and over, atomically, with either a hard link to an in-root note or a symlink to an outside file, while the test sends `/read` requests as fast as it can for up to 4 s:
- pre-fix: Linux returned `OUTSIDE ROOT SENTINEL` with status 200 after 101 requests; Windows after 2 and after 15 requests (two runs). The reviewer's own probe script (`backend_probes.py`) also reproduced it on both systems.
- fixed: the two stress tests (file swap, parent-folder swap) passed in 5 of 5 consecutive runs on Linux and 5 of 5 on Windows, and the deterministic seam tests passed in every full-suite run. Each stress run asserts that the swapper completed swaps, that some requests returned 200 with the in-root bytes and that some were refused with 403 (so the escaping link really was hit). The reviewer's probe script, run unmodified, gave: Linux 2,000 requests, 24,736 swaps, statuses 200: 1,150 and 403: 850, no leak; Windows 2,000 requests, 14,643 swaps, 200: 759, 403: 1,086, 404: 155, no leak. The round 2 reviewer repeated it: Linux 2,000 requests / 15,370 swaps and Windows 2,000 requests / 11,837 swaps, no leak.
- the parent-folder stress test (a real folder repeatedly renamed away and replaced by a link to an outside folder) is probabilistic against the old code: it failed on Linux in 3 of 7 pre-fix runs (after 14, 251 and 371 requests) and did not fail on Windows in any of 7 pre-fix runs. For parent folders the dependable before/after evidence is the deterministic test that swaps the folder immediately before the server opens the file: it fails pre-fix and passes after on both systems.

F4 before and after (round 1). Pre-fix, 8 controls exit 0 with "bundle check passed": computed `import(s)`, `import("rea" + "ct")`, ``import(`react`)``, `if (false) import("https://...")`, `0 && import("left-pad")`, an uncalled `s => import(s)`, `eval(...)` and `new Function(...)`. After the fix all 26 controls behaved as specified.

### Review round 2: R1 to R4

The round 2 reviewer confirmed F1 and F3, and found R1 to R4.

| # | Action in this round | Where | Evidence |
|---|---|---|---|
| R1 | No code change. Out of scope by owner decision (owner approval, 2026-10-19). Documented, and the claim that reads are safe against concurrent links was narrowed to "path links and replacement during `/read`". | this document; `dashboard/plugin_api.py` module docstring | The reviewer's probe still reproduces: `hard-link-to-sibling-outside-file` status 200, `OUTSIDE ROOT SENTINEL` (re-run on Linux here) |
| R2 | No code change. Out of scope by owner decision (owner approval, 2026-10-19). Documented; the `list_notes` docstring no longer implies protection. | this document; `list_notes` docstring | The reviewer's probe still reproduces: `coordinated-parent-swap-list` status 200, files include `sub/outside-name.md` (re-run on Linux here) |
| R3 | The Desktop's loader is now the decisive gate on the shipped bundle; the parser pre-check rejects every spelling the loader would not recognise; new controls. | `scripts/desktop-loader.mjs` (new), `scripts/bundle-imports.mjs` (`loaderRecognises`), `scripts/check-bundle.mjs`, `scripts/load-harness.mjs`, `scripts/check-bundle.test.mjs`, `package.json` (`check:fast`) | see below |
| R4 | This document rewritten: every finding with its real status, before/after outputs, exact import forms, the threat model, unproven items. | `docs/spike-feasibility.md` | n/a |

R3 reproduction in the review: `import React from "re\u0061ct"; export default React;` passed the checker (exit 0) while the Desktop's `unsupportedImports` returned the raw text `re\u0061ct` and `rewriteSpecifiers` left the source unchanged. `import/*comment*/("react"); export default {};` also passed the checker but the loader neither flagged nor rewrote it. The parser sees the decoded value, the loader's regular expression sees the raw spelling and a narrower token layout. Fixed by making the loader's own result decisive and by teaching the pre-check the same spelling rule.

R3 before and after, the same 49-case test file against the old and the new checker (Linux, `HERMES_AGENT_DIR` set):

```
# old scripts/bundle-imports.mjs and scripts/check-bundle.mjs (f99bd46), new tests
ℹ tests 49   ℹ pass 35   ℹ fail 14
  failed: rejects an allowed specifier written with a \u escape / \x escape / \u{...} escape
          rejects an escaped allowed specifier in a dynamic import
          rejects a comment between import and the opening parenthesis of a dynamic import
          rejects a comment between the parenthesis and the specifier of a dynamic import
          rejects a comment between from and the specifier of a static import
          rejects a line comment between from and the specifier
          rejects a side-effect import with no whitespace after import
          the command rejects the escaped allowed literal
          the command rejects the comment-separated dynamic import
          the command accepts the committed desktop/plugin.js          (old CLI has no loader gate)
          fails without HERMES_AGENT_DIR instead of silently skipping   (old CLI has no loader gate)
          names the skipped loader gate when run with --no-loader       (old CLI has no loader gate)

# new checker, same tests, Linux (Node 24.13.0) and native Windows (Node 26.7.0)
ℹ tests 49   ℹ pass 49   ℹ fail 0   ℹ skipped 0
```

Eleven of the 14 failures are rejection controls the old checker passed with exit 0; the other three fail because the old command line has no loader mode. The same sample through the real CLI:

```
$ cat esc.mjs
import React from "re\u0061ct"; export default React;
# old checker (f99bd46)
bundle check passed: only @hermes/plugin-sdk, react, react/jsx-runtime are imported, all by string literal     (exit 0)
# new checker
bundle check FAILED:
  - static import of "react" at 1:1 is spelled in a way the Desktop loader does not recognise or rewrite (...)
  - Desktop loader gate: the Desktop loader refuses the import of "re\u0061ct"
  - Desktop loader gate: the Desktop loader does not rewrite the static import of "react" at 1:1: it would reach the browser unmapped     (exit 1)
```

The reviewer's own `checker_comparison.mjs`, run against this head, shows the escaped and the comment-separated controls exiting 1 from the CLI (before the fix: 0), computed, dead-branch, concatenated and template controls exiting 1, and the committed bundle passing. The comparison script's embedded harness run on those two rejected bundles also exits 1, as intended: the harness refuses a bundle the loader would not rewrite.

Final runs at this head:
- Linux: `uv run --locked --python 3.10 pytest` and `--python 3.14`: 64 passed, 3 skipped each. `uvx ruff check dashboard tests`: All checks passed. `npm run verify` (build 474,937 bytes, check passed, `npm test` 49 pass 0 fail, harness passed), bundle sha256 `844a6a9b…c194c4`.
- Native Windows (Hermes Python 3.14.7, Node 26.7.0, tree copied to a scratch dir): `pytest` 67 passed; `npm ci` then `npm run verify`: 49 pass 0 fail, harness passed, same sha256. `ruff` was run on Linux only.

Follow-ups, not done here:
- Restrict `root` to configured vault roots (the product's own follow-up).
- Run the operator steps on the real Windows Desktop to prove the gateway mount, `ctx.rest` auth and the real loader on the real bundle. Run the `F_GETPATH` branch on a Mac.
- Decide whether `/list` should list notes behind in-root symlinked folders (today `/read` serves them but `/list` does not).
- Catalog admission lint (19 prototype-patching hits) and the `lang-markdown` bundle size.
- Move the Python and JS checks into CI; the repository has none yet. CI needs a hermes-agent checkout for the loader gate (`HERMES_AGENT_DIR`).

## Operator steps (live install and verification)

Nothing in this spike installs into Hermes. Run these yourself after review, on the Windows host. `%HERMES_HOME%` is the Hermes home directory (the default is the `hermes` folder under `%LOCALAPPDATA%`). A profile-scoped run uses that profile's `config.yaml` for `plugins.enabled`; check which profile the Desktop backend was launched with.

1. Get the package into the plugins root. The folder name must be `pairdown`. The built `desktop/plugin.js` is committed, so Node is not needed:
   ```
   git clone --branch spike/feasibility git@github.com:fneuch/pairdown.git "%HERMES_HOME%\plugins\pairdown"
   ```
   Alternative, pinned by commit: `hermes plugins install fneuch/pairdown --ref <40-char SHA of the branch head>`. Untested here.
2. Optional, read-only: `hermes plugins validate "%HERMES_HOME%\plugins\pairdown"`. Expect the `desktop surface` check to fail with prototype-patching hits (see above). That does not block a local install.
3. Enable the agent half (this is the `plugins.enabled` gate that allows importing `plugin_api.py`): `hermes plugins enable pairdown`. Confirm with `hermes plugins list`.
4. Restart the Hermes backend / gateway that the Desktop connects to, as the SDK doc requires ("backend routes mount at startup"). Quit and reopen the Desktop if it manages the backend itself.
5. In the Desktop: Capabilities, Plugins, press Rescan. `pairdown` should list with the desktop half off. Switch it on. A "PairDown" pane appears in the right zone.
6. Verify the backend through the pane. Type the absolute path of a vault folder (a folder containing `.obsidian`) and press Check.
   - Expect `health`: `{"ok": true, "plugin": "pairdown", "platform": "win32", ...}` and `validate`: `{"exists": true, "valid": true}`.
   - Type a folder without `.obsidian`: expect `"valid": false`.
   - Type a relative path: expect an error text (HTTP 400) in the result box.
   - A 404 means the route is not mounted: check step 3 and 4, then `hermes logs gui -f` and `%HERMES_HOME%\logs\errors.log` for `Failed to load plugin pairdown API routes`.
   - A 401 means the session token was not sent by the bridge. That is a real finding for Risk A.
7. Verify the editor. The pane shows a heading and two bold lines. The line with the cursor shows `**bold text**`; the other line shows plain bold text. Click into the other line and the markers swap. A load error toast naming an unsupported import would mean the Desktop loader disagrees with the gate in "Import gate"; report its text.
8. Report back: Desktop version, whether step 4 was needed (try step 5 and 6 once without a restart to see if the hot mount works), and any toast on load.
9. To undo: `hermes plugins remove pairdown`, or switch the pane off in Capabilities, Plugins.

## Reproduce the spike checks

```
# Python (Linux or Windows; the race tests take about 10 s in total)
uv run --locked pytest
uvx ruff check dashboard tests

# JS (Linux or Windows; Node 20+). HERMES_AGENT_DIR is a read-only hermes-agent checkout.
npm ci
export HERMES_AGENT_DIR=<hermes-agent checkout>   # Windows cmd: set HERMES_AGENT_DIR=...
npm run verify                                    # build + check (both gates) + 49 checker tests + harness
npm test                                          # only the checker controls (loader cases skipped without HERMES_AGENT_DIR)
npm run check:fast                                # parser pre-check only, no checkout needed; not proof of loadability
node scripts/check-bundle.mjs <other bundle>      # check any other file with both gates
```
