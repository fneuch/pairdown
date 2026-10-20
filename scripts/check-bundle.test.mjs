// Negative and positive controls for scripts/check-bundle.mjs. Each case is a
// small ES module checked through the real command line, so the exit code and
// the success message are what an operator or CI would see.
import { strict as assert } from 'node:assert'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const checker = join(root, 'scripts/check-bundle.mjs')
const work = mkdtempSync(join(tmpdir(), 'pairdown-check-'))
after(() => rmSync(work, { recursive: true, force: true }))

let counter = 0
function run(source) {
  const file = join(work, `case-${counter++}.mjs`)
  writeFileSync(file, source)
  return runFile(file)
}

function runFile(file) {
  const result = spawnSync(process.execPath, [checker, file], { cwd: root, encoding: 'utf8' })
  return { status: result.status, out: `${result.stdout}${result.stderr}` }
}

function assertRejected(source, expected) {
  const { status, out } = run(source)
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
