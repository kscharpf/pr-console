import type { PrRow, RepoConfig, VerifyRun, VerifyRunError } from "./types";

/** The colour of a verdict: the config's [verdicts] strings, else UNKNOWN. */
export function verdictColor(cfg: RepoConfig, v: string | undefined): string {
  if (v === undefined) return "gray";
  if (v === cfg.verdicts.ready_for_train) return "green";
  if (v === cfg.verdicts.ready) return "teal";
  if (v === cfg.verdicts.not_ready) return "red";
  if (v === "UNKNOWN") return "orange";
  return "gray";
}

export function reviewColor(state: string): string {
  switch (state) {
    case "APPROVED":
      return "green";
    case "CHANGES_REQUESTED":
      return "red";
    case "COMMENTED":
      return "blue";
    case "DISMISSED":
      return "gray";
    default:
      return "gray";
  }
}

export function isRunError(r: VerifyRun | VerifyRunError): r is VerifyRunError {
  return "error" in r;
}

export function runColor(r: VerifyRun): string {
  if (r.status !== "completed") return "yellow";
  if (r.conclusion === "success") return "green";
  if (r.conclusion === "cancelled" || r.conclusion === "skipped") return "gray";
  return "red";
}

export function age(iso: string): string {
  const h = (Date.now() - Date.parse(iso)) / 36e5;
  if (!Number.isFinite(h)) return "";
  return h < 48 ? `${Math.max(0, Math.round(h))}h` : `${Math.round(h / 24)}d`;
}

/** A PR can join a train when its verdict is the config's ready_for_train and trains can be built. */
export const canTrain = (cfg: RepoConfig, p: PrRow): boolean =>
  cfg.actions.includes("train") && cfg.verdicts.ready_for_train !== undefined && p.status?.verdict === cfg.verdicts.ready_for_train;

/** The worktree path, shortened below the config's worktrees directory. */
export function shortPath(cfg: RepoConfig, p: string): string {
  const dir = cfg.conventions.worktrees_dir;
  if (!dir) return p;
  const i = p.indexOf(`/${dir}/`);
  return i < 0 ? p : "…/" + p.slice(i + dir.length + 2);
}
