// daedalus-gate: installed by `daedalus init` (do not edit; re-install overwrites it).
// Translates Pi extension events into `daedalus hook <name>` calls. A no-op when
// `daedalus` is not on PATH, inside sessions Daedalus launched (DAEDALUS_AGENT), and
// on any error: it never blocks a tool call and never reports anything as accepted.
// Docs: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md
import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const BOOTSTRAP: string = ""; // @bootstrap (install sets this from harness.bootstrap)
const INSTALL_HINT = 'pip install "git+https://github.com/KEV-INZHU/Daedalus"';
const STOP_TIMEOUT_MS = 3600 * 1000;
const EDIT_TOOLS = new Set(["edit", "write"]);

type Out = Record<string, any>;

function run(cmd: string, args: string[], input: string, timeoutMs: number, cwd: string): Promise<{ out: string; missing: boolean }> {
  return new Promise((resolve) => {
    let out = "";
    let done = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (missing: boolean) => {
      if (!done) { done = true; if (timer) clearTimeout(timer); resolve({ out, missing }); }
    };
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(cmd, args, { cwd, stdio: ["pipe", "pipe", "ignore"], shell: process.platform === "win32" });
    } catch { return resolve({ out: "", missing: true }); }
    timer = setTimeout(() => { try { child.kill(); } catch {} finish(false); }, timeoutMs);
    child.stdout?.on("data", (d: Buffer) => { out += d.toString("utf8"); });
    child.on("error", (e: NodeJS.ErrnoException) => finish(e.code === "ENOENT"));
    // On Windows the command runs through cmd.exe, which reports a missing command as exit 9009, not ENOENT.
    child.on("close", (code: number | null) => finish(process.platform === "win32" && code === 9009 && !out));
    child.stdin?.on("error", () => {});
    child.stdin?.end(input);
  });
}

async function hook(name: string, payload: Out, cwd: string, timeoutMs = 120_000): Promise<{ out: Out; missing: boolean }> {
  if (process.env.DAEDALUS_AGENT) return { out: {}, missing: false };
  const r = await run("daedalus", ["hook", name], JSON.stringify(payload), timeoutMs, cwd);
  try { return { out: r.out.trim() ? JSON.parse(r.out) : {}, missing: r.missing }; }
  catch { return { out: {}, missing: r.missing }; }
}

export default function (pi: ExtensionAPI) {
  if (process.env.DAEDALUS_AGENT) return;
  let context = "";

  const say = (ctx: any, msg: unknown) => {
    if (typeof msg !== "string" || !msg) return;
    try { if (ctx.hasUI) { ctx.ui.notify(msg, "info"); return; } } catch {}
    console.error(msg); // print/json mode: stderr, so stdout stays the event stream
  };
  const base = (ctx: any) => ({ cwd: ctx.cwd, session_id: ctx.sessionManager.getSessionId() });

  pi.on("session_start", async (_e, ctx) => {
    try {
      let r = await hook("session-start", base(ctx), ctx.cwd);
      if (r.missing && BOOTSTRAP) {
        await run("sh", ["-c", BOOTSTRAP], "", 600_000, ctx.cwd);
        r = await hook("session-start", base(ctx), ctx.cwd);
      }
      if (r.missing) return say(ctx, `This repository is gated by Daedalus, which is not installed here: ${INSTALL_HINT}`);
      context = r.out?.hookSpecificOutput?.additionalContext ?? "";
      say(ctx, r.out?.systemMessage);
    } catch {}
  });

  // Inject the gate's context into the system prompt of every turn (not stored in the session).
  pi.on("before_agent_start", async (e: any) => {
    if (!context) return;
    return { systemPrompt: `${e.systemPrompt}\n\n${context}` };
  });

  pi.on("input", async (e: any, ctx) => {
    try {
      // Messages the gate itself sent (source "extension") must not become the user's request.
      if (e.source !== "extension") await hook("prompt", { ...base(ctx), prompt: e.text }, ctx.cwd);
    } catch {}
    return { action: "continue" };
  });

  pi.on("tool_call", async (e: any, ctx) => {
    try {
      if (!EDIT_TOOLS.has(e.toolName)) return; // never blocks the edit
      const r = await hook("pre-edit", { ...base(ctx), tool_name: e.toolName, tool_input: { file_path: e.input?.path } }, ctx.cwd);
      say(ctx, r.out?.systemMessage);
    } catch {}
  });

  pi.on("agent_end", async (e: any, ctx) => {
    try {
      const last = [...(e.messages ?? [])].reverse().find((m: any) => m.role === "assistant");
      if (last && (last.stopReason === "aborted" || last.stopReason === "error")) return; // user stopped it / pi retries
      const r = (await hook("stop", base(ctx), ctx.cwd, STOP_TIMEOUT_MS)).out;
      say(ctx, r.systemMessage);
      if (r.decision === "block" && r.reason) {
        const reason = String(r.reason);
        if (!ctx.hasUI) return say(ctx, reason); // print/json mode exits after this turn: surface it
        // agent_end fires while the run is still finishing; send once idle (that starts a new turn).
        let tries = 0;
        const t = setInterval(() => {
          let idle = false;
          try { idle = ctx.isIdle(); } catch { clearInterval(t); return; }
          if (idle || ++tries > 400) {
            clearInterval(t);
            try { pi.sendUserMessage(reason, { deliverAs: "followUp" }); } catch {}
          }
        }, 25);
      }
    } catch {}
  });
}
