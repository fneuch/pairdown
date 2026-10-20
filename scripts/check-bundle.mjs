import { existsSync, readFileSync, statSync } from 'node:fs'
import { relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { inspectBundle } from './bundle-imports.mjs'
import { ALLOWED_EXTERNALS } from './externals.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const bundle = process.argv[2] ? resolve(process.argv[2]) : resolve(root, 'desktop/plugin.js')

if (!existsSync(bundle)) {
  console.error(`bundle check FAILED: ${bundle} does not exist; run "npm run build" first`)
  process.exit(1)
}

// The shipped file is parsed as it is. Re-bundling it first would let the
// optimiser drop dead branches and unused imports before they are looked at.
const { imports, problems } = inspectBundle(readFileSync(bundle, 'utf8'), ALLOWED_EXTERNALS)

const found = [...new Set(imports.map(imp => `${imp.kind} ${imp.specifier}`))]
console.log(`imports found in ${relative(root, bundle) || bundle}: ${found.join(', ') || '(none)'}`)
console.log(`bundle size: ${statSync(bundle).size} bytes`)

if (problems.length > 0) {
  console.error('bundle check FAILED:')
  for (const problem of problems) console.error(`  - ${problem}`)
  process.exit(1)
}
console.log(`bundle check passed: only ${ALLOWED_EXTERNALS.join(', ')} are imported, all by string literal`)
