// Hermes Desktop's own import handling, taken from a read-only hermes-agent
// checkout and applied to a built bundle. The checkout is never modified: the
// two functions are cut out of apps/desktop/src/contrib/runtime-loader.ts,
// transpiled in memory and imported from a data: URL.
//
// This is the decisive import gate. The parser based pre-check in
// bundle-imports.mjs is only a fast first filter that must be at least as strict.
import { transform } from 'esbuild'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { inspectBundle } from './bundle-imports.mjs'

/**
 * @param {string} agentDir root of a hermes-agent checkout
 * @param {Record<string, string>} shimUrls specifier -> URL the Desktop would map it to
 * @returns {Promise<{ rewriteSpecifiers: (source: string) => string, unsupportedImports: (source: string) => string[] }>}
 */
export async function loadDesktopLoader(agentDir, shimUrls) {
  const source = readFileSync(join(agentDir, 'apps/desktop/src/contrib/runtime-loader.ts'), 'utf8')
  const start = source.indexOf('const importSpecifierRe')
  const end = source.indexOf('export function unloadRuntimePlugin')
  if (!(start > 0 && end > start)) {
    throw new Error('runtime-loader.ts layout changed: cannot extract the specifier functions')
  }
  const ts =
    `const sdkImportMap = () => (${JSON.stringify(shimUrls)})\n${source.slice(start, end)}\n` +
    'export { rewriteSpecifiers, unsupportedImports }\n'
  const { code } = await transform(ts, { loader: 'ts', format: 'esm' })
  return import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
}

/**
 * Runs the Desktop loader on the shipped bundle text and reports every way in
 * which the loader and the bundle's real syntax disagree.
 *
 * 1. unsupportedImports() must find nothing.
 * 2. After rewriteSpecifiers(), every import the parser sees must point at one
 *    of the shim URLs. An import the loader did not recognise (escaped
 *    spelling, a comment before the specifier, ...) keeps its original name
 *    and fails here, even when unsupportedImports() never noticed it.
 *
 * @returns {string[]} problems; empty when the Desktop would load the bundle
 */
export function desktopLoaderProblems(source, loader, shimUrls) {
  const problems = []
  for (const specifier of loader.unsupportedImports(source)) {
    problems.push(`the Desktop loader refuses the import of "${specifier}"`)
  }

  const rewritten = loader.rewriteSpecifiers(source)
  const mapped = new Set(Object.values(shimUrls))
  const before = inspectBundle(source, mapped)
  const after = inspectBundle(rewritten, mapped)
  if (after.problems.some(p => p.startsWith('cannot parse'))) {
    problems.push('the bundle no longer parses after the Desktop loader rewrote it')
    return problems
  }
  if (after.imports.length !== before.imports.length) {
    problems.push('the Desktop loader changed the number of imports in the bundle')
  }
  for (const imp of after.imports) {
    if (!mapped.has(imp.specifier)) {
      problems.push(
        `the Desktop loader does not rewrite the ${imp.kind} of "${imp.specifier}" at ${imp.at}: it would reach the browser unmapped`
      )
    }
  }
  return problems
}
