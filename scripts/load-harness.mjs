// Headless load test for desktop/plugin.js. This is NOT the real Hermes Desktop:
// the three allowed imports are replaced by stubs, and the app shell, the
// Electron bridge and the gateway are absent.
//
// 1. Evaluate the bundle with stubbed @hermes/plugin-sdk, react and
//    react/jsx-runtime, and call register() with a recording context.
// 2. Mount the registered pane in jsdom with real React and check the
//    CodeMirror live-preview decoration and the ctx.rest calls.
//
// Optional: set HERMES_AGENT_DIR to a hermes-agent checkout and the Desktop's
// own import allowlist and specifier rewriter (runtime-loader.ts) are extracted
// and applied to the bundle, instead of the simple rewriter used otherwise.
import { transform } from 'esbuild'
import { strict as assert } from 'node:assert'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { JSDOM, VirtualConsole } from 'jsdom'

import { ALLOWED_EXTERNALS } from './externals.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const bundlePath = process.argv[2] ? resolve(process.argv[2]) : `${root}desktop/plugin.js`
const require = createRequire(import.meta.url)
const work = mkdtempSync(join(tmpdir(), 'pairdown-harness-'))
const step = message => console.log(`ok - ${message}`)

function writeModule(name, source) {
  const file = join(work, name)
  writeFileSync(file, source)
  return pathToFileURL(file).href
}

// Same shape as the Desktop's shim modules: a default export plus one named
// export per member of the live namespace.
function shimSource(namespace) {
  const names = Object.keys(namespace).filter(n => n !== 'default' && /^[A-Za-z_$][\w$]*$/.test(n))
  return `const m = globalThis.__pairdownShims[${JSON.stringify(namespace.__name)}];\n` +
    `export default m.default ?? m;\nexport const { ${names.join(', ')} } = m;\n`
}

const React = require('react')
const jsxRuntime = require('react/jsx-runtime')
const sdkStub = { PANES_AREA: 'panes' }
const namespaces = {
  '@hermes/plugin-sdk': { ...sdkStub, __name: '@hermes/plugin-sdk' },
  react: { ...React, __name: 'react' },
  'react/jsx-runtime': { ...jsxRuntime, __name: 'react/jsx-runtime' }
}
globalThis.__pairdownShims = namespaces

const shimUrls = Object.fromEntries(
  Object.entries(namespaces).map(([spec, ns], i) => [spec, writeModule(`shim-${i}.mjs`, shimSource(ns))])
)

async function loadDesktopLoader() {
  const dir = process.env.HERMES_AGENT_DIR
  if (!dir) return null
  const source = readFileSync(join(dir, 'apps/desktop/src/contrib/runtime-loader.ts'), 'utf8')
  const start = source.indexOf('const importSpecifierRe')
  const end = source.indexOf('export function unloadRuntimePlugin')
  assert(start > 0 && end > start, 'runtime-loader.ts layout changed: cannot extract the specifier functions')
  const ts = `const sdkImportMap = () => (${JSON.stringify(shimUrls)})\n${source.slice(start, end)}\n` +
    'export { rewriteSpecifiers, unsupportedImports }\n'
  const { code } = await transform(ts, { loader: 'ts', format: 'esm' })
  return import(writeModule('desktop-loader.mjs', code))
}

function simpleRewrite(source) {
  let out = source
  for (const spec of ALLOWED_EXTERNALS) {
    out = out.replaceAll(`"${spec}"`, JSON.stringify(shimUrls[spec]))
  }
  return out
}

const source = readFileSync(bundlePath, 'utf8')
const desktopLoader = await loadDesktopLoader()
let rewritten
if (desktopLoader) {
  assert.deepEqual(desktopLoader.unsupportedImports(source), [])
  rewritten = desktopLoader.rewriteSpecifiers(source)
  assert.notEqual(rewritten, source)
  step('Desktop runtime-loader import allowlist accepts the bundle (unsupportedImports() returned [])')
  step('Desktop runtime-loader rewriteSpecifiers() rewrote the bundle imports')
} else {
  rewritten = simpleRewrite(source)
  console.log('skip - HERMES_AGENT_DIR not set: using the simple specifier rewriter, Desktop loader code not exercised')
}
for (const spec of ALLOWED_EXTERNALS) assert(!rewritten.includes(`"${spec}"`), `unrewritten import of ${spec}`)

// jsdom globals must exist before the bundle is evaluated: CodeMirror inspects
// navigator and document at module scope.
const pageErrors = []
const virtualConsole = new VirtualConsole()
virtualConsole.on('jsdomError', error => pageErrors.push(error))
const dom = new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>', {
  pretendToBeVisual: true,
  url: 'http://localhost/',
  virtualConsole
})
const { window } = dom
window.Range.prototype.getClientRects = () => []
window.Range.prototype.getBoundingClientRect = () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 })
for (const key of ['window', 'document', 'navigator', 'getComputedStyle', 'MutationObserver', 'requestAnimationFrame', 'cancelAnimationFrame', 'Window', 'Node', 'Element', 'HTMLElement', 'Text', 'Range', 'KeyboardEvent', 'MouseEvent', 'Event']) {
  Object.defineProperty(globalThis, key, { value: key === 'window' ? window : window[key], configurable: true, writable: true })
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true

const bundleUrl = writeModule('plugin.mjs', rewritten)
const started = performance.now()
const mod = await import(bundleUrl)
step(`bundle evaluated without throwing (${(performance.now() - started).toFixed(0)} ms)`)

const plugin = mod.default
assert.equal(plugin.id, 'pairdown')
assert.equal(typeof plugin.register, 'function')

const registered = []
const restCalls = []
const ctx = {
  register: contribution => {
    registered.push(contribution)
    return () => {}
  },
  rest: async path => {
    restCalls.push(path)
    if (path === '/health') return { ok: true, plugin: 'pairdown' }
    return { exists: true, valid: true }
  }
}
plugin.register(ctx)
assert.equal(registered.length, 1)
assert.equal(registered[0].area, 'panes')
assert.equal(registered[0].id, 'pane')
assert.equal(typeof registered[0].render, 'function')
step(`register(ctx) called ctx.register({ id: "${registered[0].id}", area: "${registered[0].area}", title: "${registered[0].title}" })`)

const element = registered[0].render()
assert(element && typeof element.type === 'function', 'render() must return a React element')
step('render() returned a React element')

const { createRoot } = require('react-dom/client')
const { EditorView } = await import('@codemirror/view')
const container = window.document.getElementById('app')
const reactRoot = createRoot(container)
await React.act(async () => reactRoot.render(element))

const content = container.querySelector('[data-pairdown="editor"] .cm-content')
assert(content, 'CodeMirror content element not mounted')
const lineTexts = () => [...content.querySelectorAll('.cm-line')].map(line => line.textContent)
const lineWith = (needle) => lineTexts().find(text => text.includes(needle))

assert(lineWith('bold text').includes('**bold text**'), 'active line must keep its ** markers')
assert(!lineWith('more bold').includes('**'), 'inactive line must hide its ** markers')
assert(content.querySelector('.pd-strong'), 'strong mark decoration missing')
step('CodeMirror mounted; ** markers hidden on the inactive line and kept on the active line')

const view = EditorView.findFromDOM(container.querySelector('[data-pairdown="editor"]'))
assert(view, 'could not reach the EditorView through the DOM')
const target = view.state.doc.toString().indexOf('more bold')
await React.act(async () => view.dispatch({ selection: { anchor: target } }))
assert(!lineWith('bold text').includes('**'), 'line must hide markers after the cursor leaves it')
assert(lineWith('more bold').includes('**more bold**'), 'line must show markers once the cursor enters it')
step('moving the cursor swaps which line shows its ** markers (decorations update on selection)')

const input = container.querySelector('[data-pairdown="root"]')
const button = container.querySelector('[data-pairdown="check"]')
const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
await React.act(async () => {
  setValue.call(input, '/tmp/example vault')
  input.dispatchEvent(new window.Event('input', { bubbles: true }))
})
await React.act(async () => button.click())
assert.deepEqual(restCalls, ['/health', '/validate?root=%2Ftmp%2Fexample%20vault'])
assert.match(container.querySelector('[data-pairdown="result"]').textContent, /"valid": true/)
step(`pane called ctx.rest(${restCalls.map(c => JSON.stringify(c)).join(', ')}) and rendered the JSON result`)

await React.act(async () => reactRoot.unmount())
await new Promise(resolve => window.setTimeout(resolve, 100))
rmSync(work, { recursive: true, force: true })
assert.deepEqual(pageErrors.map(String), [], 'jsdom reported errors while the pane ran')
step('no errors reported by jsdom while the pane ran (including animation-frame measure passes)')
console.log('harness passed (stubbed modules + jsdom; not the real Hermes Desktop)')
