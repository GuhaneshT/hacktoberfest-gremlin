import { isAbsolute, relative, resolve } from "node:path";
import { allow, customPolicies, deny } from "failproofai";

const WRITE_TOOLS = new Set(["Write", "Edit"]);
const DESTRUCTIVE_SHELL = [
  /\b(?:sudo|doas|su)\b/i,
  /\b(?:rm|rmdir|del|erase)\b/i,
  /\bgit\s+(?:push|reset\s+--hard|clean\s+-[a-z]*f)/i,
  /\b(?:shutdown|reboot|halt|poweroff)\b/i,
  /\b(?:mkfs|format|diskpart|bcdedit)\b/i,
  /\bdd\s+if=/i,
  /\b(?:curl|wget)\b[^|]*\|\s*(?:sh|bash|zsh)\b/i,
  /\bremove-item\b.*-recurse/i,
];
const DANGEROUS_TEST_CONTENT = [
  /\b(?:subprocess|socket|requests|urllib|shutil)\b/,
  /\bos\.(?:system|popen|remove|unlink|rmdir|rename|replace)\b/,
  /\b(?:eval|exec|__import__)\s*\(/,
  /\b(?:rm\s+-rf|sudo|git\s+push|chmod\s+777)\b/,
  /open\s*\([^)]*["'](?:w|a|x)/i,
];

const bashCalls = new Map<string, number>();

function normalize(path: string): string {
  return path.replaceAll("\\", "/");
}

function resolveFromCwd(ctx: any, filePath: string): string {
  const cwd = ctx.session?.cwd ?? process.cwd();
  return resolve(cwd, filePath);
}

function isInsideChaos(ctx: any, filePath: string): boolean {
  const cwd = ctx.session?.cwd ?? process.cwd();
  const chaosRoot = resolve(cwd, "tests", "chaos");
  const target = resolveFromCwd(ctx, filePath);
  const rel = relative(chaosRoot, target);
  return rel !== "" && !rel.startsWith("..") && !isAbsolute(rel);
}

customPolicies.add({
  name: "gremlin-tests-only-writes",
  description: "Gremlin may only write pytest files inside tests/chaos",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (!WRITE_TOOLS.has(ctx.toolName ?? "")) return allow();

    const rawPath = String(
      ctx.toolInput?.file_path ?? ctx.toolInput?.filePath ?? ctx.toolInput?.path ?? "",
    );
    if (!rawPath) return deny("Gremlin writes must include a file path.");

    if (!isInsideChaos(ctx, rawPath)) {
      return deny(
        "Gremlin may only write inside tests/chaos. Source files are immutable.",
      );
    }

    const normalized = normalize(resolveFromCwd(ctx, rawPath));
    const basename = normalized.split("/").pop() ?? "";
    if (
      !basename.endsWith(".py") ||
      (!basename.startsWith("test_") && basename !== "conftest.py")
    ) {
      return deny("Gremlin may only write pytest files named test_*.py or conftest.py.");
    }

    const content = String(
      ctx.toolInput?.content ?? ctx.toolInput?.new_string ?? "",
    );
    for (const pattern of DANGEROUS_TEST_CONTENT) {
      if (pattern.test(content)) {
        return deny(
          "Blocked test content containing subprocess, network, eval, or destructive file operations.",
        );
      }
    }

    return allow();
  },
});

customPolicies.add({
  name: "gremlin-protect-target-source",
  description: "Block edits to target source files",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (!WRITE_TOOLS.has(ctx.toolName ?? "")) return allow();

    const rawPath = String(
      ctx.toolInput?.file_path ?? ctx.toolInput?.filePath ?? ctx.toolInput?.path ?? "",
    );
    if (!rawPath) return allow();

    const normalized = normalize(resolveFromCwd(ctx, rawPath));
    const segments = normalized.toLowerCase().split("/");
    if (segments.includes("targets")) {
      return deny("The original target source is immutable.");
    }

    return allow();
  },
});

customPolicies.add({
  name: "gremlin-block-destructive-shell",
  description: "Block destructive shell commands during chaos runs",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (ctx.toolName !== "Bash") return allow();

    const command = String(ctx.toolInput?.command ?? "");
    for (const pattern of DESTRUCTIVE_SHELL) {
      if (pattern.test(command)) {
        return deny(
          "Destructive shell commands are blocked. Use pytest with a bounded timeout instead.",
        );
      }
    }

    return allow();
  },
});

customPolicies.add({
  name: "gremlin-stop-repeated-commands",
  description: "Stop repeated identical shell calls that can indicate a loop",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (ctx.toolName !== "Bash") return allow();

    const command = String(ctx.toolInput?.command ?? "").trim();
    if (!command) return allow();

    const session = ctx.session as { sessionId?: string; id?: string } | undefined;
    const key = `${session?.sessionId ?? session?.id ?? "unknown"}:${command}`;
    const count = (bashCalls.get(key) ?? 0) + 1;
    bashCalls.set(key, count);

    if (bashCalls.size > 5000) {
      bashCalls.clear();
    }

    if (count > 3) {
      return deny(
        "Gremlin appears to be repeating the same shell command. Change approach or stop.",
      );
    }

    return allow();
  },
});
