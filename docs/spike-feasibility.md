# PairDown feasibility spike

Throwaway spike. It checks two technical risks before any product code is written:

- Risk A: a native Hermes Desktop pane can call a Python `plugin_api.py` backend through `ctx.rest`, and that backend can read a local vault with `pathlib` only, the same way on Windows, Linux and macOS.
- Risk B: a CodeMirror 6 editor can be bundled into a single `desktop/plugin.js` that the Hermes Desktop disk-plugin loader accepts. Disk plugins may only import `@hermes/plugin-sdk`, `react` and `react/jsx-runtime`.

Verdict: both risks hold up in every check that can be run without the real Desktop. The decisive checks (real Desktop load, real gateway mount, real `ctx.rest` auth) are not run yet. See "Not proven" and "Operator steps". One new risk was found (catalog admission lint, below).

Reference versions: Hermes `v0.22.2` (Windows host), Desktop SDK docs and sources read from that checkout, Node 24.13.0, Python 3.10.12 and 3.14.8 on Linux, Python 3.14.7 on Windows, uv 0.13.1, esbuild 0.28.2, CodeMirror `@codemirror/view` 6.43.15 / `state` 6.7.6 / `lang-markdown` 6.5.2 / `language` 6.13.1.

## Layout

```
plugin.yaml                  name: pairdown
dashboard/manifest.json      { "name": "pairdown", "api": "plugin_api.py", tab hidden }
dashboard/plugin_api.py      router = APIRouter(); /health /validate /list /read
desktop/plugin.js            built artifact (committed, reproducible)
desktop/src/                 index.jsx (registration), pane.jsx (pane), editor.js (CodeMirror + decoration)
scripts/                     build.mjs, check-bundle.mjs, load-harness.mjs, externals.mjs
tests/test_plugin_api.py     pytest + FastAPI TestClient
```

## How Hermes mounts and reaches the backend (read from the v0.22.2 sources)

These are code reads, not runs.

- Discovery: `hermes_cli/web_server_dashboard.py::_discover_dashboard_plugins` scans `<plugins root>/*/dashboard/manifest.json`. `api` must be a relative path inside `dashboard/` (`_safe_plugin_api_relpath`).
- Gate: a user plugin's `plugin_api.py` is imported only if the plugin is in `plugins.enabled` and not in `plugins.disabled` (`_plugin_api_mount_skip_reason`). Enabling the pane in Desktop Capabilities does not import Python.
- Mount: `_mount_one_plugin_api` loads the file with `importlib.util.spec_from_file_location` under the module name `hermes_dashboard_plugin_<name>`, then `app.include_router(router, prefix="/api/plugins/<name>")`. `tests/test_plugin_api.py` loads the backend the same way and mounts it under the same prefix.
- Startup versus hot mount: the SDK doc says to restart the gateway after enabling. The code also has a first-request hot mount (`_PluginApiHotMount`, `_try_hot_mount_plugin_api`) for plugins enabled after start. Not tested here. The operator steps follow the documented restart.
- Auth: `ctx.rest(path)` becomes `hermesApi({ path: '/api/plugins/<id>' + path, ...profileScoped() })` (`apps/desktop/src/api/plugins.ts`) over the Desktop's `window.hermesDesktop.api` bridge. On the backend the request passes the host-header check, the OAuth gate, then `auth_middleware`, which requires the session token on every `/api/` path (`hermes_cli/web_server.py`). A request without it gets 401 before the plugin-enabled gate runs. `ctx.rest` rejects `..` in the path. How the bridge attaches the token was not traced.
- Loader: `apps/desktop/src/contrib/runtime-loader.ts` scans the whole source for import specifiers (comment- and string-aware lexer), refuses anything except `@hermes/plugin-sdk` / `react*`, rewrites the allowed ones to shim blob URLs, and `import()`s the result with a 10 s evaluation deadline. The shims re-export the app's live React and SDK, so React is a singleton.

## Risk A: backend

### What was proven

`dashboard/plugin_api.py` is pure `pathlib`/`os`/stdlib plus FastAPI. There is no `subprocess` and no shell (a test parses the source and checks this).

| Route | Behaviour |
|---|---|
| `GET /health` | `{ok, plugin, python, platform}` |
| `GET /validate?root=` | `{root, exists, valid}`; `valid` is true only if `<root>/.obsidian` is a directory; relative root gives 400 |
| `GET /list?root=` | sorted relative POSIX paths of `*.md` (case-insensitive suffix); prunes `.obsidian`, `.trash`, `node_modules`; does not follow directory symlinks; omits symlinked files that resolve outside root |
| `GET /read?root=&path=` | UTF-8 text read as bytes, so CRLF is preserved; 415 on invalid UTF-8; `.md` only; refuses ignored folders |

Path safety on `/read` has two layers. First a lexical check rejects absolute paths, `\` or `/` prefixes (UNC), drive letters and `:` (also blocks NTFS alternate data streams), NUL, `*?"<>|`, empty, `.` and `..` segments, and segments ending in space or dot. `\` is normalised to `/`. Second, after joining to the resolved root, `resolve(strict=True)` must stay under the root (symlink escape gives 403). Status codes: 400 lexical, 403 ignored folder or symlink escape, 404 missing, 415 not UTF-8.

Tests: `tests/test_plugin_api.py`, 53 tests against a temp fixture vault. They cover:
- validate true, false, `.obsidian` as a file, missing root, relative root, missing parameter;
- list with nested folders, an upper-case `.MD`, and `.obsidian`/`.trash`/`node_modules` skipped;
- read of accents and emoji, CRLF byte-exactness, a name with spaces, `'` and `&`, backslash separators, invalid UTF-8;
- 23 traversal and malformed-path variants: `../`, `..\`, `..%2f` and `%2e%2e/` (sent raw as well), UNC, `C:\` and `C:/`, `c:x`, `::$DATA`, NUL, trailing space and dot, an absolute path that points at a real secret file;
- symlinked file and directory escaping root (403), an inside-root symlink (allowed), Windows reserved device names, root with forward slashes or a trailing separator.

Commands and real results (full-suite output is the last line of each run):

```
# Linux (WSL), Python 3.10.12, venv from uv.lock
uv run pytest
  -> 53 passed, 1 warning in 1.16s

# Linux (WSL), Python 3.14.8
uv run --python 3.14 pytest
  -> 53 passed, 1 warning in 2.14s

# Native Windows, Hermes' bundled Python 3.14.7, uv on Windows, tree copied to a Windows temp dir
uv run --python <hermes-tools>/python-3.14.7*/python.exe pytest
  -> platform win32 -- Python 3.14.7, pytest-9.1.1 ... 53 passed, 1 warning in 2.15s

uvx ruff check dashboard tests   -> All checks passed!
```

The one warning is Starlette saying `httpx` with `TestClient` is deprecated in favour of `httpx2`. It affects tests only.

On Windows the symlink tests ran and passed (nothing skipped), so the escape defence was exercised on NTFS too. macOS was not run. Nothing in the backend is platform-specific, but "identical on macOS" is inferred, not shown.

Mutation check: with the `relative_to(root)` containment check removed, the two symlink-escape tests failed. The `..` cases stay rejected anyway, because the lexical dot rules also catch them. That is intended defence in depth, and the symlink tests are real tests.

The reference backend (hermes-obsidian-memory `dashboard/api.py`, commit `11239984`) was read for comparison. It scans a fixed vault from an env var with `rglob` and `read_text`, with no root parameter and no containment check. It also reports `str(rel)`, which is backslashed on Windows, and `read_text` would turn CRLF into LF. This spike does not copy it. It uses `os.walk` with pruning, `as_posix()` and byte-exact reads.

### Not proven (Risk A)

- A real gateway importing `plugin_api.py` through `_mount_one_plugin_api`, with the real middleware stack. The test app only mirrors that mount.
- `ctx.rest` from a real Desktop reaching `/api/plugins/pairdown/*` on the Windows backend, and the session-token / profile handling on that call.
- The effect of `plugins.enabled` and the gateway restart on the live Windows install, and whether the first-request hot mount makes the restart unnecessary.
- Behaviour with `plugins.isolation: host` (requests are forwarded to a plugin host, responses buffered). Not read in depth.
- macOS execution.
- Security design is not final. `root` is caller-supplied, so any authenticated client can list or read `.md` files under any absolute directory the gateway user can read. A product version should restrict roots to a configured allow-list.
- Large vaults: `/list` walks synchronously with no cache or pagination. Not measured.

## Risk B: editor bundle

### What was proven

`desktop/src` registers one pane through `@hermes/plugin-sdk` (`ctx.register({ area: PANES_AREA, ... })`). The pane has a root `<input>` and a "Check" button. The button calls `ctx.rest('/health')` and `ctx.rest('/validate?root=...')` and shows the JSON. The pane also mounts a CodeMirror 6 editor (`@codemirror/state`, `view`, `lang-markdown`, plus `language` for `syntaxTree`) with sample Markdown. The live-preview decoration walks the Markdown syntax tree and hides the `**` marks of every `StrongEmphasis` node that does not touch a selected line.

```
npm run build   -> built desktop/plugin.js: 474937 bytes
npm run check   -> imports found in desktop/plugin.js: import-statement @hermes/plugin-sdk, import-statement react, import-statement react/jsx-runtime, import-statement react/jsx-runtime
                   bundle size: 474937 bytes
                   bundle check passed: only @hermes/plugin-sdk, react and react/jsx-runtime are imported
```

- Bundle size: 474,937 bytes minified (166,365 bytes gzipped; 877,163 bytes unminified). The build is deterministic: rebuilding in a fresh clone gave the identical sha256 (`844a6a9b...c194c4`).
- Where the bytes go (minified): `@codemirror/view` 152 KB, `@lezer/javascript` 78 KB, `@codemirror/state` 47 KB, `@lezer/markdown` 36 KB. `@lezer/css`, `@lezer/html` and the `lang-html`/`lang-css`/`lang-javascript` wrappers add roughly 70 KB more. `@codemirror/lang-markdown` pulls those in for fenced-code and HTML support. Our code is 2.7 KB.
- `check-bundle.mjs` re-bundles the built file with the three specifiers external and fails on any other import, any dynamic import, any inlined file, `import.meta`, `require(` or a missing default export. Negative controls, all exit 1: `import "left-pad"` (fails to resolve), `import("https://...")` (disallowed dynamic-import), a relative `./other.js` import (pulls in other files).

Headless load (`scripts/load-harness.mjs`). This is not the real Desktop. It uses stub modules for the three allowed imports, jsdom instead of Electron, and a recording `ctx`.

```
HERMES_AGENT_DIR=<hermes-agent checkout> npm run harness
ok - Desktop runtime-loader import allowlist accepts the bundle (unsupportedImports() returned [])
ok - Desktop runtime-loader rewriteSpecifiers() rewrote the bundle imports
ok - bundle evaluated without throwing (42 ms)
ok - register(ctx) called ctx.register({ id: "pane", area: "panes", title: "PairDown" })
ok - render() returned a React element
ok - CodeMirror mounted; ** markers hidden on the inactive line and kept on the active line
ok - moving the cursor swaps which line shows its ** markers (decorations update on selection)
ok - pane called ctx.rest("/health", "/validate?root=%2Ftmp%2Fexample%20vault") and rendered the JSON result
ok - no errors reported by jsdom while the pane ran (including animation-frame measure passes)
harness passed (stubbed modules + jsdom; not the real Hermes Desktop)
```

When `HERMES_AGENT_DIR` points at a hermes-agent checkout, the harness extracts the loader's own `unsupportedImports` and `rewriteSpecifiers` from `apps/desktop/src/contrib/runtime-loader.ts` (read-only) and applies them to the minified bundle. That is stronger evidence than a re-implementation, because the real lexer copes with minified CodeMirror (regex-literal and string heuristics). Without the variable, a simple string rewriter is used and the harness prints a `skip` line. If the loader file is later refactored, the extraction fails loudly rather than silently passing.

Negative controls: swapping the hiding decoration for a plain mark made the harness fail on the "inactive line must hide its ** markers" assertion; running the Desktop loader code against `import "left-pad"` fails with `unsupported import: left-pad`.

Full pipeline from a fresh clone of the pushed branch (`npm ci && npm run verify`) passed, and `uv run pytest` there gave `53 passed`.

### New finding: catalog admission lint

`hermes_cli/plugin_validate_desktop.py` (used by `hermes plugins validate` and catalog admission, not by the local disk loader) scans `desktop/plugin.js` for "prototype patching" with the regex `X.prototype.y = ...`. It was run read-only against this bundle: 19 findings, all from CodeMirror and Lezer's own class setup (for example `ye.prototype.startSide=ye.prototype.endSide=0`), nothing from our code. Local loading is not affected. Getting into the catalog would need a build step that removes these assignments (for example `Object.assign(X.prototype, {...})`), an exemption for bundled dependencies, or a different editor. Treat it as a follow-up before any catalog submission. The finding came from a copy of that single function run in isolation; the full `hermes plugins validate` was not run.

### Not proven (Risk B)

- Loading in the real Desktop (Electron renderer, real blob-URL shims, real React 19.2.7 singleton, real SDK `PANES_AREA`, a real pane tile).
- CodeMirror layout in a real browser engine: scrolling, IME, selection by mouse, themes (`var(--ui-*)` variables are not used yet), focus handling inside a pane, the 10 s evaluation deadline on a cold machine. jsdom has no layout.
- Hot reload and `onDispose` behaviour (the editor is destroyed on unmount only; no `ctx.onDispose` hookup).
- That the SDK version and `PANES_AREA` export on the target Desktop match the checkout read here (`v0.22.2`).
- Bundle size acceptability in the real app (475 KB, uncached on each hot reload).
- Windows execution of the JS tooling (Node scripts ran on Linux only).

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
7. Verify the editor. The pane shows a heading and two bold lines. The line with the cursor shows `**bold text**`; the other line shows plain bold text. Click into the other line and the markers swap.
8. Report back: Desktop version, whether step 4 was needed (try step 5 and 6 once without a restart to see if the hot mount works), and any toast on load.
9. To undo: `hermes plugins remove pairdown`, or switch the pane off in Capabilities, Plugins.

## Reproduce the spike checks

```
# Python (Linux or Windows)
uv run pytest

# JS (Linux; Node 20+)
npm ci
npm run verify                                  # build + check + harness
HERMES_AGENT_DIR=<hermes-agent checkout> npm run harness   # also use the Desktop's own loader code
node scripts/check-bundle.mjs <other bundle>    # negative controls
```
