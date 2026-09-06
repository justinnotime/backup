import { spawn } from 'node:child_process'


const REPORTER = process.env.ORC_TURN_REPORT_COMMAND || "orc-turn-report"

function report(kind) {
  try {
    const child = spawn(REPORTER, ['--kind', kind, '--harness', 'dsh'], {
      detached: true,
      stdio: 'ignore',
    })
    child.on("error", () => {})
    child.unref()
  } catch {
    // fail-soft is the contract
  }
}

export const name = 'dsh-turn-report'

export function apply(ctx) {
  let midTurn = false
  ctx.on('agent/pre-step', async (payload, next) => {
    if (!midTurn) {
      midTurn = true
      report('start')
    }
    return next ? next() : undefined
  })
  ctx.on('agent/turn-stopping', async (payload, next) => {
    midTurn = false
    report('end')
    return next ? next() : undefined
  })
}
