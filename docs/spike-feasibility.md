# PairDown feasibility spike

Throwaway spike. It checks two technical risks before any product code is written:

- Risk A: a native Hermes Desktop pane can call a Python `plugin_api.py` backend through `ctx.rest`, and that backend can read a local vault with `pathlib` only, the same way on Windows, Linux and macOS.
- Risk B: a CodeMirror 6 editor can be bundled into a single `desktop/plugin.js` that the Hermes Desktop disk-plugin loader accepts. Disk plugins may only import `@hermes/plugin-sdk`, `react` and `react/jsx-runtime`.

Verdict: both risks hold up in every check that can be run without the real Desktop. A first review of this spike (round 1) failed it on five points: a read race that could return bytes from outside the vault, NTFS junctions followed by `/list`, case-sensitive matching of ignored folders on Windows, a bundle import checker that accepted forbidden imports, and report claims that did not reproduce. All five are fixed, and every fix has a regression test that was run to fail before the fix and pass after (see "Review round 1"). The decisive checks (real Desktop load, real gateway mount, real `ctx.rest` auth, macOS) are still not run. See "Not proven" and "Operator steps". One new risk was found earlier (catalog admission lint, below).

Reference versions: Hermes `v0.22.2` (Windows host), Desktop SDK docs and sources read from that checkout, Node 24.13.0 (Linux) and 26.7.0 (Windows), Python 3.10.12 and 3.14.8 on Linux, Python 3.14.7 on Windows, uv 0.13.1, esbuild 0.28.2, acorn 8.19.0 / acorn-walk 8.3.5, CodeMirror `@codemirror/view` 6.43.15 / `state` 6.7.6 / `lang-markdown` 6.5.2 / `language` 6.13.1.

## Layout

```
plugin.yaml                  name: pairdown
dashboard/manifest.json      { "name": "pairdown", "api": "plugin_api.py", tab hidden }
dashboard/plugin_api.py      router = APIRouter(); /health /validate /list /read
desktop/plugin.js            built artifact (committed, reproducible)
desktop/src/                 index.jsx (registration), pane.jsx (pane), editor.js (CodeMirror + decoration)
scripts/                     build.mjs, check-bundle.mjs (+ bundle-imports.mjs, check-bundle.test.mjs), load-harness.mjs, externals.mjs
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
| `GET /list?root=` | sorted relative POSIX paths of `*.md` (case-insensitive suffix); prunes `.obsidian`, `.trash`, `node_modules`; never enters a symlinked or junctioned folder; lists a symlinked file only if it resolves inside the root and outside ignored folders |
| `GET /read?root=&path=` | UTF-8 text read as bytes, so CRLF is preserved; 415 on invalid UTF-8; `.md` only; refuses ignored folders |

Path safety on `/read` has three layers. First a lexical check rejects absolute paths, `\` or `/` prefixes (UNC), drive letters and `:` (also blocks NTFS alternate data streams), NUL, `*?"<>|`, empty, `.` and `..` segments, segments ending in space or dot, and (on Windows) reserved device names such as `CON.md`. `\` is normalised to `/`. Ignored folder names are matched case-insensitively on Windows and macOS and case-sensitively on Linux.

Second, the note is opened, and the real path of the file that was actually opened is read back from the open handle: `/proc/self/fd/<n>` on Linux, `fcntl(F_GETPATH)` on macOS, `GetFinalPathNameByHandleW` on Windows (via `ctypes`, stdlib, called only in one helper). That real path must be inside the resolved root and must not pass through an ignored folder, otherwise the response is 403 and no byte is read. The check is bound to the opened file, not to a pathname that is looked up again, so a file or a parent folder swapped for a link at any moment cannot change what is returned. If the real path cannot be obtained (for example `/proc` is not mounted) the route fails closed with 501. A second `resolve()` before `read_bytes()` was the first version's design and is exactly what the review showed to be racy. Status codes: 400 lexical, 403 ignored folder or escape, 404 missing, 415 not UTF-8, 501 cannot verify.

Scope of the guarantee. It covers the bytes returned by `/read`. Not covered: `/list` only enumerates names and is not protected against a folder being swapped for a link while the walk runs (it never enters a link that exists when it looks, and it returns names only); a hard link inside the vault to a file outside it is indistinguishable from an ordinary file and is allowed; `root` itself is caller-chosen (see "Not proven").

Tests: `tests/test_plugin_api.py`, 67 tests against a temp fixture vault. They cover:
- validate true, false, `.obsidian` as a file, missing root, relative root, missing parameter;
- list with nested folders, an upper-case `.MD`, and `.obsidian`/`.trash`/`node_modules` skipped;
- read of accents and emoji, CRLF byte-exactness, a name with spaces, `'` and `&`, backslash separators, invalid UTF-8;
- 23 traversal and malformed-path variants: `../`, `..\`, `..%2f` and `%2e%2e/` (sent raw as well), UNC, `C:\` and `C:/`, `c:x`, `::$DATA`, NUL, trailing space and dot, an absolute path that points at a real secret file;
- symlinked file and directory escaping root (403), an inside-root symlink (allowed), Windows reserved device names, root with forward slashes or a trailing separator;
- race regressions (below): a file swapped for an escaping symlink while requests run, a parent folder swapped for an escaping link while requests run, and three deterministic cases that swap exactly before or after the server opens the file;
- listing regressions: a folder link cycle, a folder link pointing outside, a folder link staying inside, and on Windows a real NTFS junction pointing outside and a junction cycle;
- case regressions: mixed-case `Node_Modules`, `.Obsidian`, `.TRASH` (listed on Linux, excluded on Windows) and mixed-case spellings of existing ignored folders (403 on Windows), plus an 8.3 short-name alias of `node_modules` on Windows.

Three tests are platform-specific and are reported as skipped on Linux (8.3 short names, junction outside, junction cycle). They run on Windows.

Commands and real results after the fixes (full-suite output is the last line of each run):

```
# Linux (WSL), Python 3.10.12, venv from uv.lock
uv run --locked --python 3.10 pytest
  -> 64 passed, 3 skipped, 1 warning in 9.62s      (the 3 skips are the NTFS-only tests)

# Linux (WSL), Python 3.14.8
uv run --locked --python 3.14 pytest
  -> 64 passed, 3 skipped, 1 warning in 9.56s

# Native Windows, Hermes' bundled Python 3.14.7, uv on Windows, tree copied to a Windows scratch dir
uv run --locked --python <hermes-tools>/python-3.14.7*/python.exe pytest
  -> 67 passed, 1 warning in 10.12s                (nothing skipped)

uvx ruff check dashboard tests   -> All checks passed!
```

The one warning is Starlette saying `httpx` with `TestClient` is deprecated in favour of `httpx2`. It affects tests only. The race tests each spend about 4 s hammering requests, so a full run is about 10 s.

macOS was not run. The macOS branch of the opened-file check (`F_GETPATH`) is written but has never executed. The rest of the backend is not platform-specific, but "identical on macOS" is inferred, not shown.

The reference backend (hermes-obsidian-memory `dashboard/api.py`, commit `11239984`) was read for comparison. It scans a fixed vault from an env var with `rglob` and `read_text`, with no root parameter and no containment check. It also reports `str(rel)`, which is backslashed on Windows, and `read_text` would turn CRLF into LF. This spike does not copy it. It uses a `scandir` walk that never enters links, `as_posix()` and byte-exact reads.

### Not proven (Risk A)

- A real gateway importing `plugin_api.py` through `_mount_one_plugin_api`, with the real middleware stack. The test app only mirrors that mount.
- `ctx.rest` from a real Desktop reaching `/api/plugins/pairdown/*` on the Windows backend, and the session-token / profile handling on that call.
- The effect of `plugins.enabled` and the gateway restart on the live Windows install, and whether the first-request hot mount makes the restart unnecessary.
- Behaviour with `plugins.isolation: host` (requests are forwarded to a plugin host, responses buffered). Not read in depth.
- macOS execution, including the `F_GETPATH` branch of the opened-file check.
- Containment is tested, not proven. The race tests drive a high-rate swapper against the real route for a few seconds; they show that the unfixed code leaks and the fixed code never did in the runs recorded below, they do not prove the absence of every interleaving. The design argument (the check reads the real path of the file handle that is then read) is what carries the guarantee. Other filesystems were not tried: network shares, FUSE, OneDrive placeholders, case-sensitive NTFS folders, ReFS, and Windows volumes with 8.3 names switched off (the alias test skips there).
- Not covered by the read guarantee: a hard link inside the vault to an outside file; `/list` under a concurrent folder swap (names only); a vault that contains a symlinked folder is listed without that folder's notes although `/read` serves them if they resolve inside the root.
- Security design is not final. `root` is caller-supplied, so any authenticated client can list or read `.md` files under any absolute directory the gateway user can read. A product version should restrict roots to a configured allow-list.
- Large vaults: `/list` walks synchronously with no cache or pagination. Not measured.

## Risk B: editor bundle

### What was proven

`desktop/src` registers one pane through `@hermes/plugin-sdk` (`ctx.register({ area: PANES_AREA, ... })`). The pane has a root `<input>` and a "Check" button. The button calls `ctx.rest('/health')` and `ctx.rest('/validate?root=...')` and shows the JSON. The pane also mounts a CodeMirror 6 editor (`@codemirror/state`, `view`, `lang-markdown`, plus `language` for `syntaxTree`) with sample Markdown. The live-preview decoration walks the Markdown syntax tree and hides the `**` marks of every `StrongEmphasis` node that does not touch a selected line.

```
npm run build   -> built desktop/plugin.js: 474937 bytes
npm run check   -> imports found in desktop/plugin.js: static import @hermes/plugin-sdk, static import react, static import react/jsx-runtime
                   bundle size: 474937 bytes
                   bundle check passed: only @hermes/plugin-sdk, react, react/jsx-runtime are imported, all by string literal
```

- Bundle size: 474,937 bytes minified. The build is deterministic: rebuilding in a fresh clone on Linux, and on native Windows with Node 26.7.0, gave the identical sha256 (`844a6a9b...c194c4`). Supplemental sizes, measured with these exact commands on the committed file: `gzip -c desktop/plugin.js | wc -c` gives 164,401 bytes (gzip level 6); Node `zlib.gzipSync(readFileSync('desktop/plugin.js')).length` with default options gives 166,422 bytes; the same build settings as `scripts/build.mjs` with `minify: false` (banner included) give 877,240 bytes unminified. The 166,365 and 877,163 figures in the first version of this report did not reproduce and are withdrawn.
- Where the bytes go (minified): `@codemirror/view` 152 KB, `@lezer/javascript` 78 KB, `@codemirror/state` 47 KB, `@lezer/markdown` 36 KB. `@lezer/css`, `@lezer/html` and the `lang-html`/`lang-css`/`lang-javascript` wrappers add roughly 70 KB more. `@codemirror/lang-markdown` pulls those in for fenced-code and HTML support. Our code is 2.7 KB. (Per-package figures come from the first version of this report and were not re-measured.)
- The committed bundle contains no dynamic `import()`, no `import.meta`, no `require()`, no `eval` and no `new Function`: it has exactly three static imports, the allowed ones.

What `npm run check` (`scripts/check-bundle.mjs`) does and does not reject. It parses `desktop/plugin.js` exactly as shipped with acorn (no re-bundling, no optimisation, so dead code is still seen) and exits 1 unless every module reference is a string literal on the allowlist `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`.

| Form | Result |
|---|---|
| `import "x"`, `import a from "x"`, `import {a} from "x"`, `import * as a from "x"` with `x` not on the list (bare, relative, URL, `react/jsx-dev-runtime`) | rejected |
| `export * from "x"`, `export {a} from "x"` with `x` not on the list | rejected |
| `import("x")` with a string-literal `x` not on the list, including inside `if (false)`, `0 && ...` or a function nobody calls | rejected |
| `import(expr)` where the argument is not a plain string literal: variable, concatenation, template literal (even without `${}`) | rejected, "computed dynamic import" |
| `import.meta`, `require(...)`, `eval(...)`, `new Function(...)` / `Function(...)` | rejected |
| no default export, a file that does not parse as an ES module, a missing file | rejected |
| any of the three allowed specifiers, as a static import, an `export ... from`, or a string-literal `import("react")` | accepted |
| import-looking text inside strings and comments | accepted (it is not an import; the Desktop loader also ignores it) |

Limits: this is a lint of the shipped file, not a sandbox. Code that builds a module reference at run time through paths the parser cannot see (for example `globalThis["ev" + "al"](...)`, or `Reflect.get`-style indirection) is not detected; a bundle with such code would be caught by review, not by this check. `require`, `eval` and `Function` are matched by name as call targets. The first version of this report claimed that the check "fails on any other import and any dynamic import". That was wrong in both directions: it missed computed and dead-branch imports, and it never rejected a dynamic `import("react")`. The accurate statement is the table above.

Controls: `npm test` runs `scripts/check-bundle.test.mjs` (26 `node:test` cases, each through the real command line): 4 accepted bundles (including the committed `plugin.js`), and rejections for bare, relative and URL imports, export-from, `react/jsx-dev-runtime`, literal URL and bare dynamic imports, computed, concatenated and template-literal dynamic imports, dead-branch and unused forms, `import.meta`, `require`, `eval`, `new Function`, missing default export, unparsable text and a missing file. Each rejected case also asserts that the success message is never printed.

Alignment with the Desktop loader (`apps/desktop/src/contrib/runtime-loader.ts`, `sdk/runtime.ts`, read-only). The loader finds specifiers with a regex that needs a quoted string, in code only (strings and comments are skipped), and allows the keys of `sdkImportMap`: `@hermes/plugin-sdk`, `react`, `react/jsx-runtime` and `react/jsx-dev-runtime`. The checker is deliberately stricter: its allowlist is the first three only, and it also rejects forms the loader cannot see (computed `import(x)`, template-literal specifiers, `eval`). With `HERMES_AGENT_DIR` set the harness asserts three things on every run: the checker's allowlist is a subset of the Desktop's import map; the checker rejects all six sample sources the loader's own `unsupportedImports()` rejects (bare, relative, URL, export-from, dynamic, dead branch); and the loader does not flag `import(s)` while the checker does.

Headless load (`scripts/load-harness.mjs`). This is not the real Desktop. It uses stub modules for the three allowed imports, jsdom instead of Electron, and a recording `ctx`.

```
HERMES_AGENT_DIR=<hermes-agent checkout> npm run harness
ok - checker allowlist is a subset of the Desktop SDK import map (@hermes/plugin-sdk, react/jsx-dev-runtime, react/jsx-runtime, react)
ok - checker rejects all 6 samples the Desktop loader rejects (bare, relative, URL, export-from, dynamic, dead branch)
ok - computed import(x): the Desktop loader does not see it, the checker rejects it
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

Full pipeline results (`npm ci && npm run verify`): see "Review round 1" for the final runs on Linux and native Windows.

### New finding: catalog admission lint

`hermes_cli/plugin_validate_desktop.py` (used by `hermes plugins validate` and catalog admission, not by the local disk loader) scans `desktop/plugin.js` for "prototype patching" with the regex `X.prototype.y = ...`. It was run read-only against this bundle: 19 findings, all from CodeMirror and Lezer's own class setup (for example `ye.prototype.startSide=ye.prototype.endSide=0`), nothing from our code. Local loading is not affected. Getting into the catalog would need a build step that removes these assignments (for example `Object.assign(X.prototype, {...})`), an exemption for bundled dependencies, or a different editor. Treat it as a follow-up before any catalog submission. The finding came from a copy of that single function run in isolation; the full `hermes plugins validate` was not run.

### Not proven (Risk B)

- Loading in the real Desktop (Electron renderer, real blob-URL shims, real React 19.2.7 singleton, real SDK `PANES_AREA`, a real pane tile).
- CodeMirror layout in a real browser engine: scrolling, IME, selection by mouse, themes (`var(--ui-*)` variables are not used yet), focus handling inside a pane, the 10 s evaluation deadline on a cold machine. jsdom has no layout.
- Hot reload and `onDispose` behaviour (the editor is destroyed on unmount only; no `ctx.onDispose` hookup).
- That the SDK version and `PANES_AREA` export on the target Desktop match the checkout read here (`v0.22.2`).
- Bundle size acceptability in the real app (475 KB, uncached on each hot reload).
- Windows execution of the real Desktop. The JS tooling (`npm ci`, build, check, tests, harness) did run on native Windows with Node 26.7.0 and produced the same bundle bytes (see "Review round 1"); the Electron renderer was not run.

## Review round 1: defects found, fixes and regression evidence

An independent review of the first version of this spike returned FAIL. Each finding was reproduced by the reviewer on the unmodified code, then fixed here. Every regression test below was run against the pre-fix `dashboard/plugin_api.py` (restored from commit `ff897d1`) and against the fixed one, with the same test file.

| # | Finding in the first version | Fix | Regression tests |
|---|---|---|---|
| F1 | `/read` resolved the path, checked it was under the root, then reopened it with `read_bytes()`. A file swapped for an escaping symlink between the two steps returned outside bytes on Linux and Windows. | Open first, then verify the real path of the opened handle (see "Path safety"). No second pathname lookup decides containment. | `test_read_never_returns_outside_bytes_while_the_file_is_swapped_for_a_symlink`, `..._while_a_parent_folder_is_swapped_for_a_link`, `test_file_replaced_by_an_escaping_symlink_just_before_open_is_refused`, `..._just_after_open_returns_only_the_opened_file`, `test_parent_folder_replaced_by_an_escaping_link_just_before_open_is_refused` |
| F2 | On Windows `os.walk(followlinks=False)` entered NTFS junctions, so `/list` enumerated files outside the root and could recurse through junction cycles. | `/list` uses its own `scandir` walk and never enters a symlink or junction (`st_reparse_tag` is the mount-point tag). Linked files are listed only if their real path is inside the root and outside ignored folders. | `test_list_does_not_enter_a_junction_to_an_outside_folder`, `test_list_terminates_on_a_junction_cycle` (Windows only), plus the link-cycle, outside-link and inside-link tests that run on every platform |
| F3 | Ignored names were matched case-sensitively: on Windows `Node_Modules/` was listed and `.OBSIDIAN/x.md` was readable. | One helper folds case on Windows and macOS and not on Linux, used by the walk pruning, the lexical read check and the real-path check. The real-path check also closes 8.3 aliases and links into ignored folders. | `test_ignored_folder_names_follow_the_filesystem_case_rules`, `test_read_refuses_mixed_case_spelling_of_existing_ignored_folders`, `test_read_refuses_short_name_alias_of_ignored_folder` (Windows only), `test_read_refuses_a_link_inside_root_that_points_into_an_ignored_folder` |
| F4 | `check-bundle.mjs` inspected the metadata of an optimised re-bundle, so `import(specifier)` and an import inside `if (false)` passed with exit 0. | Parse the shipped file with acorn; reject non-literal and non-allowlisted specifiers, dead or not (see Risk B for the exact forms). | `scripts/check-bundle.test.mjs`, 26 cases; harness alignment steps |
| F5 | The report claimed the checker "fails on any other import and any dynamic import", which was false, and it did not record the platform defects. | This report: corrected checker statement, this section, scope limits of the containment guarantee, size commands. | n/a |

Before and after, real results (the "before" runs use the new tests with the old code):

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
- fixed: the two stress tests (file swap, parent-folder swap) passed in 5 of 5 consecutive runs on Linux and 5 of 5 on Windows, and the deterministic seam tests passed in every full-suite run. Each stress run asserts that the swapper completed swaps, that some requests returned 200 with the in-root bytes and that some were refused with 403 (so the escaping link really was hit). The reviewer's probe script, run unmodified against the fixed file, gave: Linux 2,000 requests, 24,736 swaps, statuses 200: 1,150 and 403: 850, no leak; Windows 2,000 requests, 14,643 swaps, 200: 759, 403: 1,086, 404: 155, no leak.
- the parent-folder stress test (a real folder repeatedly renamed away and replaced by a link to an outside folder) is probabilistic against the old code: it failed on Linux in 3 of 7 pre-fix runs (after 14, 251 and 371 requests) and did not fail on Windows in any of 7 pre-fix runs. For parent folders the dependable before/after evidence is the deterministic test that swaps the folder immediately before the server opens the file: it fails pre-fix and passes after on both systems.

Reviewer probes after the fixes (same script, unmodified, Windows): mixed-case list gives `['safe.md']` and `/read .OBSIDIAN/hidden.md` gives 403; a junction to an outside folder gives list `['safe.md']` and read 403.

F4 before and after. Pre-fix, 8 of the new controls exit 0 with "bundle check passed": computed `import(s)`, `import("rea" + "ct")`, ``import(`react`)``, `if (false) import("https://...")`, `0 && import("left-pad")`, an uncalled `s => import(s)`, `eval(...)` and `new Function(...)`. With the fix all 26 controls behave as specified. The reviewer's probe script against the fixed checker: bare, literal URL, relative, computed and dead-branch URL all exit 1; allowed `import("react")` exits 0; the Desktop loader exits 1 on the dead-branch URL, so checker and loader now agree.

Final JS run (`HERMES_AGENT_DIR=<hermes-agent checkout> npm ci && npm run verify`):
- Linux, Node 24.13.0: build 474,937 bytes, check passed, `npm test` 26 pass 0 fail, harness passed.
- Native Windows, Node 26.7.0 from the Hermes tools folder, tree copied to a scratch dir: same results; the rebuilt `desktop/plugin.js` has sha256 `844a6a9b...c194c4`, identical to the Linux build and to the committed file.

What this round does not change. Real Desktop load, real gateway mount, `ctx.rest` auth on the Windows backend and macOS are still not proven. The static symlink tests from the first version are still there, and they are not a complete containment proof; the race tests and the design argument above are what back the claim, with the limits listed under "Not proven (Risk A)".

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
# Python (Linux or Windows; the race tests take about 10 s in total)
uv run --locked pytest
uvx ruff check dashboard tests

# JS (Linux or Windows; Node 20+)
npm ci
npm run verify                                  # build + check + checker tests + harness
npm test                                        # only the 26 checker controls
HERMES_AGENT_DIR=<hermes-agent checkout> npm run harness   # also use the Desktop's own loader code
node scripts/check-bundle.mjs <other bundle>    # check any other file
```
