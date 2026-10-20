// Usage: node scripts/check-bundle.mjs [bundle] [--no-loader]
//
// Two gates run on the bundle exactly as shipped:
//
// 1. A parser based pre-check (bundle-imports.mjs): fast, needs nothing but
//    this repository, and strict on purpose.
// 2. The Hermes Desktop's own import handling (unsupportedImports and
//    rewriteSpecifiers from a hermes-agent checkout, see desktop-loader.mjs).
//    This one is decisive: if the loader would refuse or skip an import, the
//    bundle fails, whatever the pre-check said.
//
// The checkout is named by HERMES_AGENT_DIR and is only read. Without it the
// command fails; --no-loader runs the pre-check alone and says so.
import { existsSync, readFileSync, statSync } from 'node:fs'
import { relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { inspectBundle } from './bundle-imports.mjs'
import { desktopLoaderProblems, loadDesktopLoader } from './desktop-loader.mjs'
import { ALLOWED_EXTERNALS } from './externals.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const args = process.argv.slice(2)
const skipLoader = args.includes('--no-loader')
const target = args.find(arg => !arg.startsWith('--'))
const bundle = target ? resolve(target) : resolve(root, 'desktop/plugin.js')

if (!existsSync(bundle)) {
  console.error(`bundle check FAILED: ${bundle} does not exist; run "npm run build" first`)
  process.exit(1)
}

const agentDir = process.env.HERMES_AGENT_DIR
if (!skipLoader && !agentDir) {
  console.error(
    'bundle check FAILED: HERMES_AGENT_DIR is not set. The Desktop loader is the decisive import gate; ' +
      'point it at a hermes-agent checkout, or pass --no-loader for the parser pre-check alone.'
  )
  process.exit(1)
}

// The shipped file is parsed as it is. Re-bundling it first would let the
// optimiser drop dead branches and unused imports before they are looked at.
const source = readFileSync(bundle, 'utf8')
const { imports, problems } = inspectBundle(source, ALLOWED_EXTERNALS)

const found = [...new Set(imports.map(imp => `${imp.kind} ${imp.specifier}`))]
console.log(`imports found in ${relative(root, bundle) || bundle}: ${found.join(', ') || '(none)'}`)
console.log(`bundle size: ${statSync(bundle).size} bytes`)

if (skipLoader) {
  console.log('skip - --no-loader: the Desktop loader gate was not run')
} else {
  // The URLs only have to be distinct; the rewritten text is parsed, never run.
  const shimUrls = Object.fromEntries(ALLOWED_EXTERNALS.map((spec, i) => [spec, `blob:pairdown-gate/${i}`]))
  const loader = await loadDesktopLoader(agentDir, shimUrls)
  const loaderProblems = desktopLoaderProblems(source, loader, shimUrls)
  problems.push(...loaderProblems.map(problem => `Desktop loader gate: ${problem}`))
  if (loaderProblems.length === 0) {
    console.log('Desktop loader gate: unsupportedImports() is empty and every import is rewritten')
  }
}

if (problems.length > 0) {
  console.error('bundle check FAILED:')
  for (const problem of problems) console.error(`  - ${problem}`)
  process.exit(1)
}
console.log(`bundle check passed: only ${ALLOWED_EXTERNALS.join(', ')} are imported, all by string literal`)
