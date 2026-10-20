import { useEffect, useRef, useState } from 'react'

import { createEditor } from './editor.js'

export function PairDownPane({ rest }) {
  const [root, setRoot] = useState('')
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const editorHost = useRef(null)

  useEffect(() => {
    const view = createEditor(editorHost.current)
    return () => view.destroy()
  }, [])

  const check = async () => {
    setBusy(true)
    try {
      const health = await rest('/health')
      const validate = await rest(`/validate?root=${encodeURIComponent(root)}`)
      setResult({ health, validate })
    } catch (error) {
      setResult({ error: String(error?.message ?? error) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full flex-col gap-2 p-3 text-sm" data-pairdown="pane">
      <div className="flex gap-2">
        <input
          className="min-w-0 flex-1 rounded border px-2 py-1"
          data-pairdown="root"
          placeholder="Absolute path of a vault folder"
          value={root}
          onChange={event => setRoot(event.target.value)}
        />
        <button
          className="rounded border px-2 py-1"
          data-pairdown="check"
          disabled={busy || !root}
          onClick={check}
          type="button"
        >
          Check
        </button>
      </div>
      <pre className="max-h-40 overflow-auto text-xs" data-pairdown="result">
        {result ? JSON.stringify(result, null, 2) : 'No result yet.'}
      </pre>
      <div className="min-h-0 flex-1 overflow-hidden rounded border" data-pairdown="editor" ref={editorHost} />
    </div>
  )
}
