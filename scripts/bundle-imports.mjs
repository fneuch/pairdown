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

  const record = (kind, specifier, node) => {
    imports.push({ kind, specifier, at: place(node) })
    if (!allowed.has(specifier)) problems.push(`disallowed ${kind} of "${specifier}" at ${place(node)}`)
  }

  full(ast, node => {
    switch (node.type) {
      case 'ImportDeclaration':
        record('static import', node.source.value, node)
        break
      case 'ExportAllDeclaration':
        record('export-from', node.source.value, node)
        break
      case 'ExportNamedDeclaration':
        if (node.source) record('export-from', node.source.value, node)
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
          record('dynamic import', specifier, node)
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
