// Static inspection of a built plugin bundle: every way a module can pull in
// another module is read from the syntax tree of the file as shipped. Nothing
// is bundled, optimised or tree-shaken first, so an import that dead-code
// elimination would remove is still seen.
//
// A bundle passes only if every import and export-from specifier is a string
// literal on the allowlist, and none of the escape hatches below is present.
import { parse } from 'acorn'
import { full } from 'acorn-walk'

const place = node => `${node.loc.start.line}:${node.loc.start.column + 1}`

function literalSpecifier(node) {
  return node && node.type === 'Literal' && typeof node.value === 'string' ? node.value : null
}

// The Desktop loader finds specifiers with a regular expression over the raw
// text (apps/desktop/src/contrib/runtime-loader.ts, importSpecifierRe):
//   (from\s* | import\s*\(\s* | import\s+) quote [^'"]+ quote
// An import is only mapped to the live SDK modules when it matches that shape,
// so anything else, even if it is a legal allowed import to a parser, would
// reach the browser unmapped. This is a pre-check that must be at least as strict
// as the loader; the loader itself is the decisive gate (desktop-loader.mjs).
const LOADER_PREFIX = /(?:from\s*|import\s*\(\s*|import\s+)$/
const LOADER_LITERAL = /^(['"])[^'"]+\1$/

/** True when the Desktop loader would see the string literal as an import specifier. */
function loaderRecognises(source, literal) {
  const raw = source.slice(literal.start, literal.end)
  // The loader compares the raw spelling: "re\u0061ct" is not "react" to it.
  if (!LOADER_LITERAL.test(raw) || raw.slice(1, -1) !== literal.value) return false
  return LOADER_PREFIX.test(source.slice(0, literal.start))
}

/**
 * @param {string} source built ES module text
 * @param {Iterable<string>} allowedSpecifiers
 * @returns {{ imports: { kind: string, specifier: string, at: string }[], problems: string[] }}
 */
export function inspectBundle(source, allowedSpecifiers) {
  const allowed = new Set(allowedSpecifiers)
  const imports = []
  const problems = []
  let hasDefaultExport = false

  let ast
  try {
    ast = parse(source, { ecmaVersion: 'latest', sourceType: 'module', allowHashBang: true, locations: true })
  } catch (error) {
    return { imports, problems: [`cannot parse the bundle as an ES module: ${error.message}`] }
  }

  const record = (kind, literal, node) => {
    const specifier = literal.value
    imports.push({ kind, specifier, at: place(node) })
    if (!allowed.has(specifier)) {
      problems.push(`disallowed ${kind} of "${specifier}" at ${place(node)}`)
    } else if (!loaderRecognises(source, literal)) {
      problems.push(
        `${kind} of "${specifier}" at ${place(node)} is spelled in a way the Desktop loader does not recognise or rewrite ` +
          '(escaped characters in the specifier, or a comment or missing whitespace before it)'
      )
    }
  }

  full(ast, node => {
    switch (node.type) {
      case 'ImportDeclaration':
        record('static import', node.source, node)
        break
      case 'ExportAllDeclaration':
        record('export-from', node.source, node)
        break
      case 'ExportNamedDeclaration':
        if (node.source) record('export-from', node.source, node)
        if (node.specifiers.some(s => (s.exported.name ?? s.exported.value) === 'default')) hasDefaultExport = true
        break
      case 'ExportDefaultDeclaration':
        hasDefaultExport = true
        break
      case 'ImportExpression': {
        const specifier = literalSpecifier(node.source)
        if (specifier === null) {
          problems.push(`computed dynamic import at ${place(node)}: the specifier is not a string literal`)
        } else {
          record('dynamic import', node.source, node)
        }
        break
      }
      case 'MetaProperty':
        if (node.meta.name === 'import') problems.push(`import.meta at ${place(node)}`)
        break
      case 'CallExpression':
      case 'NewExpression': {
        // Code generated from a string can contain any import; the checker cannot see it.
        const { callee } = node
        const isCall = node.type === 'CallExpression'
        if (callee.type === 'Identifier') {
          if (isCall && ['require', 'eval', 'Function'].includes(callee.name)) {
            problems.push(`${callee.name}() call at ${place(node)}`)
          } else if (!isCall && callee.name === 'Function') {
            problems.push(`new Function() at ${place(node)}`)
          }
        }
        break
      }
    }
  })

  if (!hasDefaultExport) problems.push('the bundle has no default export')
  return { imports, problems }
}
