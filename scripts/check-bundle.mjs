import { build } from 'esbuild'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

import { ALLOWED_EXTERNALS } from './externals.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const bundle = process.argv[2] ? resolve(process.argv[2]) : `${root}desktop/plugin.js`
const allowed = new Set(ALLOWED_EXTERNALS)
const problems = []

if (!existsSync(bundle)) {
  console.error(`bundle check FAILED: ${bundle} does not exist; run "npm run build" first`)
  process.exit(1)
}

// Re-bundle the built file with the allowed specifiers external: any other
// import either fails to resolve or is inlined, and both show up below.
let result
try {
  result = await build({
    absWorkingDir: root,
    entryPoints: [bundle],
    bundle: true,
    write: false,
    metafile: true,
    format: 'esm',
    platform: 'browser',
    external: ALLOWED_EXTERNALS,
    logLevel: 'silent'
  })
} catch (error) {
  problems.push(`built file does not resolve cleanly: ${error.errors?.map(e => e.text).join('; ') ?? error.message}`)
}

if (result) {
  const inputs = Object.keys(result.metafile.inputs)
  if (inputs.length !== 1) problems.push(`built file pulls in other files: ${inputs.join(', ')}`)

  const [output] = Object.values(result.metafile.outputs)
  for (const imp of output.imports) {
    if (!allowed.has(imp.path)) problems.push(`disallowed ${imp.kind}: ${imp.path}`)
  }
  const used = output.imports.map(imp => `${imp.kind} ${imp.path}`)
  console.log(`imports found in desktop/plugin.js: ${used.join(', ') || '(none)'}`)
}

const source = readFileSync(bundle, 'utf8')
if (!/\bexport\s*\{[^}]*\bas\s+default\b|\bexport\s+default\b/.test(source)) {
  problems.push('built file has no default export')
}
if (/\bimport\s*\.\s*meta\b|\brequire\s*\(/.test(source)) {
  problems.push('built file uses import.meta or require()')
}

console.log(`bundle size: ${statSync(bundle).size} bytes`)

if (problems.length > 0) {
  console.error('bundle check FAILED:')
  for (const problem of problems) console.error(`  - ${problem}`)
  process.exit(1)
}
console.log('bundle check passed: only @hermes/plugin-sdk, react and react/jsx-runtime are imported')
