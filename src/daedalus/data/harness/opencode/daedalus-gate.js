// daedalus-gate: installed by `daedalus init`
// Daedalus acceptance gate for OpenCode. Re-installing overwrites it.
//
// A thin shim: it turns OpenCode plugin events into the JSON payloads of `daedalus hook <name>`
// and turns the answers back into OpenCode actions. All decisions live in Daedalus.
//   chat.message                          -> daedalus hook prompt
//   tool.execute.before (edit tools)      -> daedalus hook pre-edit   (never blocks the edit)
//   event session.idle                    -> daedalus hook stop       ({"decision":"block"} sends the
//                                            reason back to the agent as a new message, so it continues)
//   experimental.chat.system.transform    -> daedalus hook session-start (context for the agent)
//
// Portable: where `daedalus` is not on PATH every hook is a no-op. Fail-open for the harness (an
// error never locks the user out) but never into acceptance (an error only lets the agent stop).
import { spawn } from "node:child_process"

const STOP_TIMEOUT_MS = 3600 * 1000
const HOOK_TIMEOUT_MS = 120 * 1000
const EDIT_TOOLS = new Set(["edit", "write", "patch", "multiedit", "apply_patch"])
// Matches the harness-prompt prefix Daedalus ignores when remembering the user's request.
const FEEDBACK_PREFIX = "Stop hook feedback:\n"
const INSTALL_HINT = 'This repository is gated by Daedalus, which is not installed here: pip install "git+https://github.com/KEV-INZHU/Daedalus"'

// Run `daedalus hook <name>` with a payload on stdin. Resolves the parsed JSON answer, or null for
// "nothing to do" (empty output, bad output, timeout), or {missing: true} when daedalus is not on PATH.
function hook(name, payload, cwd, timeoutMs) {
  return new Promise((resolve) => {
    let out = ""
    let done = false
    const finish = (value) => {
      if (done) return
      done = true
      clearTimeout(timer)
      resolve(value)
    }
    let child
    try {
      child = spawn("daedalus", ["hook", name], { cwd, stdio: ["pipe", "pipe", "ignore"] })
    } catch {
      return resolve(null)
    }
    const timer = setTimeout(() => {
      try { child.kill() } catch {}
      finish(null)
    }, timeoutMs)
    child.on("error", (err) => finish(err && err.code === "ENOENT" ? { missing: true } : null))
    child.stdout.on("data", (d) => { out += d })
    child.on("close", () => {
      try { finish(out.trim() ? JSON.parse(out) : null) } catch { finish(null) }
    })
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(payload))
  })
}

export const DaedalusGate = async ({ client, directory }) => {
  // Sessions Daedalus launched itself are governed by its lifecycle already.
  if (process.env.DAEDALUS_AGENT) return {}
  const cwd = directory || process.cwd()
  const stopping = new Set() // sessions with a stop hook in flight
  const aborted = new Set() // sessions the user interrupted: their next idle is not a "finished" turn
  const subagent = new Map() // session id -> is a child (subagent) session
  const context = new Map() // session id -> Promise<string | null>

  const tell = async (message) => {
    if (!message) return
    try { await client.tui.showToast({ body: { message, variant: "info" } }) } catch {}
  }

  const isChild = async (sessionID) => {
    if (subagent.has(sessionID)) return subagent.get(sessionID)
    let child = false
    try {
      const res = await client.session.get({ path: { id: sessionID } })
      child = !!((res && res.data) || res || {}).parentID
    } catch {}
    subagent.set(sessionID, child)
    return child
  }

  return {
    "chat.message": async (input, output) => {
      try {
        const text = (output.parts || [])
          .filter((p) => p && p.type === "text" && !p.synthetic)
          .map((p) => p.text)
          .join("\n")
        await hook("prompt", { cwd, session_id: input.sessionID, prompt: text }, cwd, HOOK_TIMEOUT_MS)
      } catch {}
    },

    "tool.execute.before": async (input, output) => {
      try {
        if (!EDIT_TOOLS.has(String(input.tool).toLowerCase())) return
        const a = output.args || {}
        const target = a.filePath || a.file_path || a.path
        const res = await hook(
          "pre-edit",
          { cwd, session_id: input.sessionID, tool_name: input.tool, tool_input: target ? { file_path: target } : {} },
          cwd,
          HOOK_TIMEOUT_MS,
        )
        await tell(res && res.systemMessage) // never throws: the gate does not block edits
      } catch {}
    },

    "experimental.chat.system.transform": async (input, output) => {
      try {
        const id = input && input.sessionID
        if (!id) return
        if (!context.has(id)) {
          context.set(
            id,
            hook("session-start", { cwd, session_id: id }, cwd, HOOK_TIMEOUT_MS).then((res) => {
              if (res && res.missing) return INSTALL_HINT
              const c = res && res.hookSpecificOutput && res.hookSpecificOutput.additionalContext
              return typeof c === "string" ? c : null
            }),
          )
        }
        const c = await context.get(id)
        if (c) output.system.push(c)
      } catch {}
    },

    event: async ({ event }) => {
      try {
        const type = event && event.type
        const props = (event && event.properties) || {}
        if (type === "session.error") {
          const name = props.error && props.error.name
          if (props.sessionID && name === "MessageAbortedError") aborted.add(props.sessionID)
          return
        }
        if (type !== "session.idle") return
        const id = props.sessionID
        if (!id || stopping.has(id)) return
        if (aborted.delete(id)) return // the user stopped the agent: do not force it on
        if (await isChild(id)) return
        stopping.add(id)
        let res
        try {
          res = await hook("stop", { cwd, session_id: id }, cwd, STOP_TIMEOUT_MS)
        } finally {
          stopping.delete(id)
        }
        if (!res) return
        if (res.decision === "block" && res.reason) {
          // The agent must not stop: hand it the gate's reason as its next instruction. Not awaited:
          // the reply is a whole agent turn, and its own idle event runs the gate again. The gate
          // caps its blocks, so this ends when the gate stops answering "block".
          Promise.resolve(
            client.session.prompt({
              path: { id },
              body: { parts: [{ type: "text", text: FEEDBACK_PREFIX + res.reason }] },
            }),
          ).catch(() => {})
          return
        }
        await tell(res.systemMessage)
      } catch {}
    },
  }
}
