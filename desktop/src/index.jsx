import { PANES_AREA } from '@hermes/plugin-sdk'

import { PairDownPane } from './pane.jsx'

export default {
  id: 'pairdown',
  name: 'PairDown',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'PairDown',
      data: { placement: 'right', width: '420px' },
      render: () => <PairDownPane rest={ctx.rest} />
    })
  }
}
