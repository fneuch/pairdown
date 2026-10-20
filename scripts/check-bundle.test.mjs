// Negative and positive controls for scripts/check-bundle.mjs. Each case is a
// small ES module checked through the real command line, so the exit code and
// the success message are what an operator or CI would see.
import { strict as assert } from 'node:assert'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, before, describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'

import { desktopLoaderProblems, loadDesktopLoader } from './desktop-loader.mjs'
import { ALLOWED_EXTERNALS } from './externals.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const checker = join(root, 'scripts/check-bundle.mjs')
const agentDir = process.env.HERMES_AGENT_DIR
const work = mkdtempSync(join(tmpdir(), 'pairdown-check-'))
after(() => rmSync(work, { recursive: true, force: true }))

let counter = 0
function run(source, extraArgs) {
  const file = join(work, `case-${counter++}.mjs`)
  writeFileSync(file, source)
  return runFile(file, extraArgs)
}

// Unless told otherwise the command runs the parser pre-check alone, so these
// cases need no Hermes checkout. The loader gate has its own describe block.
function runFile(file, extraArgs = ['--no-loader'], env = process.env) {
  const result = spawnSync(process.execPath, [checker, file, ...extraArgs], { cwd: root, encoding: 'utf8', env })
  return { status: result.status, out: `${result.stdout}${result.stderr}` }
}

function assertRejected(source, expected, extraArgs) {
  const { status, out } = run(source, extraArgs)
  assert.equal(status, 1, `expected exit 1, got ${status}:\n${out}`)
  assert.doesNotMatch(out, /bundle check passed/, 'a rejected bundle must never print the success message')
  assert.match(out, expected)
}

const ALLOWED_STATIC =
  'import { PANES_AREA } from "@hermes/plugin-sdk"; import React from "react"; import { jsx } from "react/jsx-runtime"; export default { PANES_AREA, React, jsx };'

describe('accepted bundles', () => {
  it('accepts static imports of the three allowed specifiers', () => {
    const { status, out } = run(ALLOWED_STATIC)
    assert.equal(status, 0, out)
    assert.match(out, /bundle check passed/)
  })

  it('accepts a dynamic import whose literal specifier is allowed', () => {
    const { status, out } = run('export default { load() { return import("react") } };')
    assert.equal(status, 0, out)
    assert.match(out, /dynamic import react/)
  })

  it('ignores import-looking text inside strings and comments, like the Desktop loader does', () => {
    const source = '// import "left-pad"\nconst note = \'import "left-pad" from "nowhere"\'; export default note;'
    const { status, out } = run(source)
    assert.equal(status, 0, out)
  })

  it('accepts the committed desktop/plugin.js', { skip: !existsSync(join(root, 'desktop/plugin.js')) }, () => {
    const { status, out } = runFile(join(root, 'desktop/plugin.js'))
    assert.equal(status, 0, out)
    assert.match(out, /bundle check passed/)
  })
})

describe('rejected bundles: static forms', () => {
  it('rejects a bare side-effect import', () => assertRejected('import "left-pad"; export default {};', /disallowed static import of "left-pad"/))
  it('rejects a bare named import that is never used', () =>
    assertRejected('import { unused } from "left-pad"; export default {};', /disallowed static import of "left-pad"/))
  it('rejects a relative import', () => assertRejected('import "./other.js"; export default {};', /disallowed static import of ".\/other.js"/))
  it('rejects a URL import', () =>
    assertRejected('import "https://example.invalid/x.js"; export default {};', /disallowed static import of "https:\/\/example.invalid\/x.js"/))
  it('rejects export-from of a bare package', () =>
    assertRejected('export * from "left-pad"; export default {};', /disallowed export-from of "left-pad"/))
  it('rejects export-from of a relative file', () =>
    assertRejected('export { a } from "./a.js"; export default {};', /disallowed export-from of ".\/a.js"/))
  it('rejects react/jsx-dev-runtime, which is outside the three allowed specifiers', () =>
    assertRejected('import { jsxDEV } from "react/jsx-dev-runtime"; export default { jsxDEV };', /disallowed static import of "react\/jsx-dev-runtime"/))
})

describe('rejected bundles: dynamic forms', () => {
  it('rejects a dynamic import of a URL', () =>
    assertRejected('import("https://example.invalid/no.js"); export default {};', /disallowed dynamic import of "https:\/\/example.invalid\/no.js"/))
  it('rejects a dynamic import of a bare package', () =>
    assertRejected('export default { load() { return import("left-pad") } };', /disallowed dynamic import of "left-pad"/))
  it('rejects a computed dynamic import', () =>
    assertRejected('export default { load(specifier) { return import(specifier) } };', /computed dynamic import/))
  it('rejects a concatenated dynamic import', () =>
    assertRejected('export default { load() { return import("rea" + "ct") } };', /computed dynamic import/))
  it('rejects a template-literal dynamic import, which the Desktop loader does not see', () =>
    assertRejected('export default { load() { return import(`react`) } };', /computed dynamic import/))
  it('rejects a forbidden import inside a dead branch (if (false))', () =>
    assertRejected('if (false) import("https://example.invalid/no.js"); export default {};', /disallowed dynamic import of "https:\/\/example.invalid\/no.js"/))
  it('rejects a forbidden import inside a short-circuit that never runs', () =>
    assertRejected('0 && import("left-pad"); export default {};', /disallowed dynamic import of "left-pad"/))
  it('rejects a computed import inside a function that is never called', () =>
    assertRejected('const never = specifier => import(specifier); export default {};', /computed dynamic import/))
})

describe('rejected bundles: spellings the Desktop loader does not recognise', () => {
  const unrecognised = /spelled in a way the Desktop loader does not recognise or rewrite/
  it('rejects an allowed specifier written with a \\u escape', () =>
    assertRejected('import React from "re\\u0061ct"; export default React;', unrecognised))
  it('rejects an allowed specifier written with a \\x escape', () =>
    assertRejected('import React from "\\x72eact"; export default React;', unrecognised))
  it('rejects an allowed specifier written with a \\u{...} escape', () =>
    assertRejected('import React from "re\\u{61}ct"; export default React;', unrecognised))
  it('rejects an escaped allowed specifier in a dynamic import', () =>
    assertRejected('export default { load() { return import("re\\u0061ct") } };', unrecognised))
  it('rejects a comment between import and the opening parenthesis of a dynamic import', () =>
    assertRejected('import/*c*/("react"); export default {};', unrecognised))
  it('rejects a comment between the parenthesis and the specifier of a dynamic import', () =>
    assertRejected('export default { load() { return import(/*c*/"react") } };', unrecognised))
  it('rejects a comment between from and the specifier of a static import', () =>
    assertRejected('import React from /*c*/ "react"; export default React;', unrecognised))
  it('rejects a line comment between from and the specifier', () =>
    assertRejected('import React from // c\n "react"; export default React;', unrecognised))
  it('rejects a side-effect import with no whitespace after import', () =>
    assertRejected('import"react"; export default {};', unrecognised))
  it('still reports a disallowed name before the spelling problem', () =>
    assertRejected('import "left-pad"; export default {};', /disallowed static import of "left-pad"/))
})

describe('accepted spellings the Desktop loader recognises', () => {
  it('accepts from directly followed by the quote, as the minifier writes it', () => {
    const { status, out } = run('import React from"react"; export default React;')
    assert.equal(status, 0, out)
  })
  it('accepts single quotes', () => {
    const { status, out } = run("import React from 'react'; export default React;")
    assert.equal(status, 0, out)
  })
  it('accepts whitespace and newlines inside a dynamic import', () => {
    const { status, out } = run('export default { load() { return import (\n  "react"\n) } };')
    assert.equal(status, 0, out)
  })
  it('accepts a side-effect import of an allowed specifier', () => {
    const { status, out } = run('import "react"; export default {};')
    assert.equal(status, 0, out)
  })
})

// The Desktop's own loader is the decisive gate. These cases need a
// hermes-agent checkout and are skipped, loudly, without HERMES_AGENT_DIR.
describe('Desktop loader gate', { skip: agentDir ? false : 'HERMES_AGENT_DIR is not set' }, () => {
  const shimUrls = Object.fromEntries(ALLOWED_EXTERNALS.map((spec, i) => [spec, `blob:test/${i}`]))
  let loader
  before(async () => {
    loader = await loadDesktopLoader(agentDir, shimUrls)
  })

  const ESCAPED = 'import React from "re\\u0061ct"; export default React;'
  const COMMENTED = 'import/*c*/("react"); export default {};'

  it('the real loader refuses the escaped spelling and reports its raw text', () => {
    assert.deepEqual(loader.unsupportedImports(ESCAPED), ['re\\u0061ct'])
    assert.match(desktopLoaderProblems(ESCAPED, loader, shimUrls).join('\n'), /refuses the import of "re\\u0061ct"/)
  })

  it('the real loader neither flags nor rewrites the comment-separated dynamic import', () => {
    assert.deepEqual(loader.unsupportedImports(COMMENTED), [])
    assert.equal(loader.rewriteSpecifiers(COMMENTED), COMMENTED)
    assert.match(
      desktopLoaderProblems(COMMENTED, loader, shimUrls).join('\n'),
      /does not rewrite the dynamic import of "react"/
    )
  })

  it('the loader gate alone rejects a comment between from and the specifier', () => {
    const problems = desktopLoaderProblems('import React from /*c*/ "react"; export default React;', loader, shimUrls)
    assert.match(problems.join('\n'), /does not rewrite the static import of "react"/)
  })

  it('the loader gate accepts what the loader rewrites', () => {
    const accepted = [
      ALLOWED_STATIC,
      'import React from"react"; export default React;',
      'export default { load() { return import (\n "react" ) } };'
    ]
    for (const source of accepted) assert.deepEqual(desktopLoaderProblems(source, loader, shimUrls), [], source)
  })

  it('the command rejects the escaped allowed literal', () =>
    assertRejected(ESCAPED, /Desktop loader gate: the Desktop loader refuses the import of "re\\u0061ct"/, []))
  it('the command rejects the comment-separated dynamic import', () =>
    assertRejected(COMMENTED, /Desktop loader gate: the Desktop loader does not rewrite the dynamic import/, []))
  it('the command accepts the committed desktop/plugin.js', { skip: !existsSync(join(root, 'desktop/plugin.js')) }, () => {
    const { status, out } = runFile(join(root, 'desktop/plugin.js'), [])
    assert.equal(status, 0, out)
    assert.match(out, /Desktop loader gate: unsupportedImports\(\) is empty and every import is rewritten/)
  })
})

describe('Desktop loader gate is mandatory by default', () => {
  it('fails without HERMES_AGENT_DIR instead of silently skipping the loader', () => {
    const env = { ...process.env }
    delete env.HERMES_AGENT_DIR
    const { status, out } = runFile(join(root, 'desktop/plugin.js'), [], env)
    assert.equal(status, 1, out)
    assert.match(out, /HERMES_AGENT_DIR is not set/)
    assert.doesNotMatch(out, /bundle check passed/)
  })
  it('names the skipped loader gate when run with --no-loader', () => {
    const { status, out } = run(ALLOWED_STATIC)
    assert.equal(status, 0, out)
    assert.match(out, /skip - --no-loader/)
  })
})

describe('rejected bundles: other escape hatches', () => {
  it('rejects import.meta', () => assertRejected('export default { url: import.meta.url };', /import\.meta/))
  it('rejects require()', () => assertRejected('const fs = require("fs"); export default { fs };', /require\(\) call/))
  it('rejects eval()', () => assertRejected('eval("import(\'left-pad\')"); export default {};', /eval\(\) call/))
  it('rejects new Function()', () =>
    assertRejected('export default new Function("return import(\'left-pad\')");', /new Function\(\)/))
  it('rejects a bundle without a default export', () => assertRejected('export const value = 1;', /no default export/))
  it('rejects text that is not a module', () => assertRejected('export default {', /cannot parse/))
  it('rejects a missing file', () => {
    const { status, out } = runFile(join(work, 'does-not-exist.js'))
    assert.equal(status, 1)
    assert.match(out, /does not exist/)
  })
})
