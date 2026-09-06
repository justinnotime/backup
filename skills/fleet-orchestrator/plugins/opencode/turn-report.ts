import type { Plugin } from "@opencode-ai/plugin"
import { spawn } from "node:child_process"


const REPORTER = process.env.ORC_TURN_REPORT_COMMAND || "orc-turn-report"

function report(kind: "start" | "end") {
  try {
    const child = spawn(REPORTER, ["--kind", kind, "--harness", "opencode"], {
      detached: true,
      stdio: "ignore",
    })
    child.on("error", () => {})
    child.unref()
  } catch {
    // fail-soft is the contract
  }
}

const turnReportPlugin: Plugin = async () => ({
  "chat.message": async () => {
    report("start")
  },
  event: async ({ event }) => {
    if (event?.type === "session.idle") report("end")
  },
})

export default turnReportPlugin
