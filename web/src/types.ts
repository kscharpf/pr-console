// Response shapes of server.py (and parsers.py, which builds parts of them).

/** parsers.parse_verdict: one of the config's [verdicts] strings, or "UNKNOWN" */
export type VerdictKind = string;
export interface Verdict {
  verdict: VerdictKind;
  reasons: string[];
  line: string;
}

/** parsers.review_state */
export interface Review {
  state: string; // "NONE" or the GitHub review state, e.g. "APPROVED", "CHANGES_REQUESTED", "COMMENTED"
  author: string;
  at: string;
  answered: boolean | null;
}

/** `gh run list --json databaseId,status,conclusion,url,headSha`, or the error of that call */
export interface VerifyRun {
  databaseId: number;
  status: string;
  conclusion: string;
  url: string;
  headSha: string;
}
export interface VerifyRunError {
  error: string;
}

/** Cache._row after Cache._enrich (which drops `body`) */
export interface PrRow {
  number: number;
  title: string;
  branch: string;
  scope: string;
  author: string;
  head: string;
  base: string;
  created_at: string;
  draft: boolean;
  labels: string[];
  review: Review;
  is_train: boolean;
  constituents: number[];
  verify_run: VerifyRun | VerifyRunError | null;
  status?: Verdict;
  /** The `## Summary` section of the PR body (parsers.pr_summary) */
  summary: string;
  url: string;
}

/** GET /api/prs: Cache.snapshot() */
export interface PrsResponse {
  prs: PrRow[];
  fetched_at: number | null;
  error: string | null;
  refreshing: boolean;
}

/** parsers.parse_lock_reason plus `alive` */
export interface ClaudeLock {
  agent: string;
  pid: number;
  start: string;
  alive: boolean;
}

/** parsers.parse_porcelain plus the fields sessions() adds */
export interface Worktree {
  path: string;
  head: string;
  branch: string;
  detached: boolean;
  bare: boolean;
  locked: boolean;
  lock_reason: string;
  prunable: boolean;
  dirty: boolean | null;
  open_pr: number | null;
  merged_pr: number | null;
  claude_lock: ClaudeLock | null;
  is_main: boolean;
  ready_to_clean: boolean;
}

export interface ReverifyLock {
  pr: number;
  holder: { pid: number; start: string } | null;
  alive: boolean;
  status: string;
}

export interface TrainReverifyStatus {
  pr: number;
  exit: number;
  status: string;
}

/** GET /api/sessions */
export interface SessionsResponse {
  groups: Record<string, Worktree[]>;
  reverify_locks: ReverifyLock[];
  reverify_state_dir: string;
  train_reverify_status: TrainReverifyStatus[];
}

/** A job key: the PR number, "train", or "branch:<name>" */
export type JobKey = number | string;

/** GET /api/jobs/<id> */
export interface Job {
  id: string;
  key: JobKey;
  argv: string[];
  started: number;
  output: string[];
  exit: number | null;
  ended: number | null;
}

/** GET /api/jobs: jobs without their output */
export type JobSummary = Omit<Job, "output">;

/** GET /api/history: the last 100 lines of actions.jsonl */
export interface HistoryEntry {
  id: string;
  key: JobKey;
  argv: string[];
  started: number;
  ended: number;
  exit: number;
  output_tail: string[];
}

/** POST /api/action: 202 */
export interface ActionStarted {
  id: string;
  argv: string[];
}

/** POST /api/refresh: 202 */
export interface RefreshStarted {
  ok: true;
}

/** Any error response */
export interface ApiError {
  error: string;
}

/** POST /api/action bodies, one per kind that build_action accepts */
export type ActionBody =
  | { kind: "reverify"; pr: number }
  | { kind: "reverify_cancel"; pr: number }
  | { kind: "merge"; pr: number }
  | { kind: "close"; pr: number; comment: string }
  | { kind: "label"; pr: number; add: boolean }
  | { kind: "train"; prs: number[]; dry_run: boolean }
  | { kind: "rebuild"; train: number; without: number[]; dry_run: boolean }
  | { kind: "cleanup"; branch: string; apply: boolean };

export type ActionKind = ActionBody["kind"];

/** The command names of config.COMMANDS */
export type CommandName =
  | "verdict"
  | "train_verdict"
  | "reverify"
  | "reverify_cancel"
  | "reverify_status"
  | "train"
  | "rebuild"
  | "cleanup";

/** One argument of a command template; a nested array repeats once per item of its list placeholder. */
export type TemplateArg = string | string[];

/** GET /api/config: config.public */
export interface RepoConfig {
  name: string;
  repo: string;
  gh_repo: string;
  conventions: {
    train_branch_prefix?: string;
    train_title_prefix?: string;
    verify_workflow?: string;
    maintainer_label?: string;
    worktrees_dir?: string;
    worktree_groups: Record<string, string>;
    reverify_lock_dir?: string;
  };
  verdicts: { ready_for_train?: string; ready?: string; not_ready?: string };
  commands: Partial<Record<CommandName, TemplateArg[]>>;
  flags: { dry_run_flag?: string; apply_flag?: string };
  actions: ActionKind[];
  coordinator: { description: string };
}

interface Values {
  pr?: number;
  train?: number;
  branch?: string;
  prs?: number[];
  without?: number[];
  dry_run?: boolean;
  apply?: boolean;
}

const PLACEHOLDER = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;
const quote = (s: string): string => (/^[A-Za-z0-9._/:=@+-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`);

/** The argv config.expand builds, as the confirmation shows it (the program as the config writes it). */
export function expandCommand(cfg: RepoConfig, name: CommandName, v: Values): string {
  const t = cfg.commands[name];
  if (!t) return "";
  const one = (a: string, item?: number): string[] => {
    const whole = /^\{([A-Za-z_][A-Za-z0-9_]*)\}$/.exec(a)?.[1];
    if (whole === "dry_run") return v.dry_run && cfg.flags.dry_run_flag ? [cfg.flags.dry_run_flag] : [];
    if (whole === "apply") return v.apply && cfg.flags.apply_flag ? [cfg.flags.apply_flag] : [];
    if (whole === "prs" || whole === "without") return item !== undefined ? [String(item)] : (v[whole] ?? []).map(String);
    return [
      a.replace(PLACEHOLDER, (_m, k: string) => {
        const x = k === "pr" ? v.pr : k === "train" ? v.train : k === "branch" ? v.branch : undefined;
        return x === undefined ? "" : String(x);
      }),
    ];
  };
  const argv: string[] = [];
  for (const a of t) {
    if (typeof a === "string") {
      argv.push(...one(a));
      continue;
    }
    const list = a.map((x) => /^\{(prs|without)\}$/.exec(x)?.[1]).find((x) => x !== undefined) as "prs" | "without" | undefined;
    for (const item of list ? (v[list] ?? []) : []) for (const x of a) argv.push(...one(x, /^\{(prs|without)\}$/.test(x) ? item : undefined));
  }
  return argv.map(quote).join(" ");
}

/** The command build_action runs for a body, for the confirmation text. */
export function commandFor(cfg: RepoConfig, b: ActionBody): string {
  switch (b.kind) {
    case "reverify":
    case "reverify_cancel":
      return expandCommand(cfg, b.kind, { pr: b.pr });
    case "merge":
      return `gh pr merge ${b.pr} --merge`;
    case "close":
      return `gh pr close ${b.pr}` + (b.comment ? ` --comment ${quote(b.comment)}` : "");
    case "label":
      return `gh pr edit ${b.pr} ${b.add ? "--add-label" : "--remove-label"} ${quote(cfg.conventions.maintainer_label ?? "")}`;
    case "train":
      return expandCommand(cfg, "train", { prs: b.prs, dry_run: b.dry_run });
    case "rebuild":
      return expandCommand(cfg, "rebuild", { train: b.train, without: b.without, dry_run: b.dry_run });
    case "cleanup":
      return expandCommand(cfg, "cleanup", { branch: b.branch, apply: b.apply });
  }
}

// ---------------------------------------------------------------- chats (chats.py)

/** The permission modes the server accepts: both still ask before acting. */
export const PERMISSION_MODES = ["auto", "default", "plan"] as const;
export type PermissionMode = (typeof PERMISSION_MODES)[number];
/** "" is the CLI's default model. */
export const MODELS = ["", "opus", "sonnet", "haiku"] as const;
export type Model = (typeof MODELS)[number];

export type ChatStatus = "starting" | "running" | "waiting" | "permission" | "idle" | "ended" | "error";

/** A can_use_tool request waiting for the maintainer. */
export interface PendingPermission {
  req_id: string;
  chat: string;
  tool: string;
  input: Record<string, unknown>;
  tool_use_id: string | null;
  title: string | null;
  description: string | null;
  decision_reason: string | null;
  blocked_path: string | null;
}

/** GET /api/chats: Chat.public() plus branch and open_pr */
export interface ChatRow {
  id: string;
  cwd: string;
  created: number;
  title: string;
  model: Model;
  permission_mode: PermissionMode;
  resume_id: string | null;
  status: ChatStatus;
  /** A session is starting or running; only an unconnected chat can be deleted. */
  connected: boolean;
  last_activity: number;
  pending: PendingPermission[];
  last_event_id: number;
  branch: string;
  open_pr: number | null;
}

/** GET /api/chats/dirs */
export interface ChatDir {
  path: string;
  branch: string;
  is_main: boolean;
}

interface EventBase {
  id: number;
  t: number;
}
/** One event of GET /api/chats/<id>/events (EventBuffer.append) */
export type ChatEvent = EventBase &
  (
    | { kind: "status"; status: ChatStatus }
    | { kind: "user"; text: string }
    | { kind: "delta"; text: string }
    | { kind: "text"; text: string; sub?: boolean }
    | { kind: "tool_use"; tool_use_id: string; name: string; input: Record<string, unknown>; sub?: boolean }
    | { kind: "tool_result"; tool_use_id: string; content: string; is_error: boolean }
    | ({ kind: "permission" } & Omit<PendingPermission, "chat">)
    | { kind: "permission_decided"; req_id: string; allow: boolean }
    | { kind: "hook"; event: string; name: string; outcome: string | null; exit_code: number | null; output: string }
    | {
        kind: "result";
        subtype: string;
        is_error: boolean;
        cost: number | null;
        duration_ms: number;
        num_turns: number;
        terminal_reason: string | null;
        errors: string[];
        text: string | null;
      }
    | { kind: "error"; message: string }
    | { kind: "info"; text: string }
    | { kind: "system"; text: string }
  );

export interface LaunchBody {
  cwd: string;
  prompt: string;
  model: Model;
  permission_mode: PermissionMode;
}
