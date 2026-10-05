/**
 * Pi port of the VibeWise Claude Code plugin. Reuses the plugin's own files:
 *
 * - /vibe-wise:learn and /vibe-wise:reset expand skills/<name>/SKILL.md the way
 *   Pi expands /skill:name, substituting ${CLAUDE_PLUGIN_ROOT}. The skills are
 *   not registered with Pi, matching their disable-model-invocation frontmatter.
 * - hooks/hooks.json runs hooks/session_start.py on startup|resume|clear|compact|fork.
 *   The same script runs here on those events (Pi's "new" is Claude's "clear"),
 *   so state lookup and pause rules stay identical to the plugin.
 */

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const HOOK = join(ROOT, "hooks", "session_start.py");
const PI_TOOLS =
	"Pi tool names: where these instructions say Glob, use the find or ls tool when enabled, " +
	"or the conditional shell check described above. AskUserQuestion is available as a tool.";

function skillPrompt(name: string, args: string): string {
	const file = join(ROOT, "skills", name, "SKILL.md");
	const body = readFileSync(file, "utf8")
		.replace(/^---\n[\s\S]*?\n---\n/, "")
		.trim()
		.replaceAll("${CLAUDE_PLUGIN_ROOT}", ROOT);
	const block =
		`<skill name="vibe-wise:${name}" location="${file}">\n` +
		`References are relative to ${dirname(file)}.\n\n${body}\n\n${PI_TOOLS}\n</skill>`;
	return args ? `${block}\n\n${args}` : block;
}

function restorationContext(cwd: string): string | undefined {
	const run = spawnSync("python3", [HOOK], {
		input: JSON.stringify({ hook_event_name: "SessionStart", cwd }),
		encoding: "utf8",
		timeout: 5000,
	});
	if (run.status !== 0 || !run.stdout.trim()) return undefined;
	try {
		return JSON.parse(run.stdout).hookSpecificOutput?.additionalContext;
	} catch {
		return undefined;
	}
}

export default function vibeWise(pi: ExtensionAPI) {
	let runActive = false;
	pi.on("agent_start", () => {
		runActive = true;
	});
	pi.on("agent_end", () => {
		runActive = false;
	});

	function restore(ctx: ExtensionContext, retrying = false) {
		const content = restorationContext(ctx.cwd);
		if (!content) return;
		// Compaction inside a tool loop, or overflow recovery that retries the turn:
		// steer so the next model call in this run sees it. Otherwise ride along with
		// the next prompt. A plain append at session start lands before Pi's initial
		// system record and never reaches the model, and a steer after a finished run
		// makes Pi start an extra, unrequested turn.
		pi.sendMessage(
			{ customType: "vibe-wise-restore", content, display: false },
			{ deliverAs: runActive || retrying ? "steer" : "nextTurn" },
		);
	}

	// Pi loads a fresh extension instance per session. RPC mode emits that
	// session's start event twice (runtime rebind plus the command handler), so
	// restore once per instance. Reload keeps the transcript, which already holds
	// the restoration message.
	let started = false;
	pi.on("session_start", (event, ctx) => {
		if (started || event.reason === "reload") return;
		started = true;
		restore(ctx);
	});
	pi.on("session_compact", (event, ctx) => restore(ctx, event.willRetry));

	for (const [name, description] of [
		["learn", "Activate or resume VibeWise learning-first development"],
		["reset", "Back up this project's VibeWise notes and restart onboarding"],
	]) {
		pi.registerCommand(`vibe-wise:${name}`, {
			description,
			handler: async (args, ctx) => {
				const prompt = skillPrompt(name, args.trim());
				pi.sendUserMessage(prompt, ctx.isIdle() ? undefined : { deliverAs: "followUp" });
			},
		});
	}
}
