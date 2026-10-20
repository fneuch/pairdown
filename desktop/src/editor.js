import { EditorState } from '@codemirror/state'
import { Decoration, EditorView, ViewPlugin } from '@codemirror/view'
import { Language, defineLanguageFacet, syntaxTree } from '@codemirror/language'
import { parser as markdownParser } from '@lezer/markdown'

export const SAMPLE_MARKDOWN = [
  '# PairDown spike',
  '',
  'This line has **bold text** and keeps its markers while the cursor is on it.',
  '',
  'This other line has **more bold** and hides its markers when the cursor is elsewhere.',
  ''
].join('\n')

// The CommonMark parser alone. @codemirror/lang-markdown would also bundle the HTML, CSS and
// JavaScript grammars for fenced code and inline HTML, which this editor does not use.
const markdownLanguage = new Language(defineLanguageFacet(), markdownParser, [], 'markdown')

const hiddenMarker = Decoration.replace({})
const strongText = Decoration.mark({ class: 'pd-strong' })

function activeLineNumbers(state) {
  const lines = new Set()
  for (const range of state.selection.ranges) {
    const first = state.doc.lineAt(range.from).number
    const last = state.doc.lineAt(range.to).number
    for (let n = first; n <= last; n++) lines.add(n)
  }
  return lines
}

function touchesActiveLine(state, active, from, to) {
  const first = state.doc.lineAt(from).number
  const last = state.doc.lineAt(to).number
  for (let n = first; n <= last; n++) if (active.has(n)) return true
  return false
}

function buildDecorations(view) {
  const { state } = view
  const active = activeLineNumbers(state)
  const ranges = []
  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from,
      to,
      enter(node) {
        if (node.name !== 'StrongEmphasis') return
        ranges.push(strongText.range(node.from, node.to))
        if (touchesActiveLine(state, active, node.from, node.to)) return
        for (const mark of node.node.getChildren('EmphasisMark')) {
          ranges.push(hiddenMarker.range(mark.from, mark.to))
        }
      }
    })
  }
  return Decoration.set(ranges, true)
}

// Hides **bold** markers on every line that does not hold a selection.
export const livePreview = ViewPlugin.fromClass(
  class {
    constructor(view) {
      this.decorations = buildDecorations(view)
    }

    update(update) {
      const treeChanged = syntaxTree(update.startState) !== syntaxTree(update.state)
      if (update.docChanged || update.selectionSet || update.viewportChanged || treeChanged) {
        this.decorations = buildDecorations(update.view)
      }
    }
  },
  { decorations: plugin => plugin.decorations }
)

const theme = EditorView.baseTheme({
  '.pd-strong': { fontWeight: 'bold' },
  '&': { height: '100%' },
  '.cm-scroller': { overflow: 'auto' }
})

export function createEditor(parent, doc = SAMPLE_MARKDOWN) {
  const cursor = doc.indexOf('bold text')
  return new EditorView({
    parent,
    state: EditorState.create({
      doc,
      selection: { anchor: Math.max(cursor, 0) },
      extensions: [markdownLanguage.extension, livePreview, theme, EditorView.lineWrapping]
    })
  })
}
