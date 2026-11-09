import type { ActionBody, RepoConfig, ChatDir, ChatRow, LaunchBody, ActionStarted, HistoryEntry, Job, PrsResponse, RefreshStarted, SessionsResponse } from "./types";

const KEY = "pr-console-token";

/** Take the token from `?token=` (first load) into sessionStorage, strip it from the address
 * bar, and otherwise read it back from sessionStorage (a reload of this tab). */
function initToken(): string {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("token");
  if (fromUrl) {
    try {
      sessionStorage.setItem(KEY, fromUrl);
    } catch {
      // storage blocked: the token lives only in memory for this page
    }
    url.searchParams.delete("token");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    return fromUrl;
  }
  try {
    return sessionStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

const TOKEN = initToken();

export class ApiFailure extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function call<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { headers: { "X-Token": TOKEN }, cache: "no-store" };
  if (body !== undefined) {
    init.method = "POST";
    init.headers = { "X-Token": TOKEN, "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  let j: unknown = null;
  try {
    j = await r.json();
  } catch {
    j = null;
  }
  if (!r.ok) {
    const msg =
      j !== null && typeof j === "object" && "error" in j && typeof j.error === "string" ? j.error : `HTTP ${r.status}`;
    throw new ApiFailure(msg, r.status);
  }
  return j as T;
}

export const api = {
  config: () => call<RepoConfig>("/api/config"),
  prs: () => call<PrsResponse>("/api/prs"),
  sessions: () => call<SessionsResponse>("/api/sessions"),
  job: (id: string) => call<Job>(`/api/jobs/${encodeURIComponent(id)}`),
  history: () => call<HistoryEntry[]>("/api/history"),
  refresh: () => call<RefreshStarted>("/api/refresh", {}),
  action: (b: ActionBody) => call<ActionStarted>("/api/action", b),
  chats: () => call<ChatRow[]>("/api/chats"),
  chatDirs: () => call<ChatDir[]>("/api/chats/dirs"),
  launch: (b: LaunchBody) => call<ChatRow>("/api/chats", b),
  resume: (id: string, cwd?: string) => call<ChatRow>("/api/chats/resume", cwd ? { id, cwd } : { id }),
  send: (id: string, text: string) => call<ChatRow>(`/api/chats/${id}/send`, { text }),
  interrupt: (id: string) => call<{ ok: true }>(`/api/chats/${id}/interrupt`, {}),
  end: (id: string) => call<{ ok: true }>(`/api/chats/${id}/end`, {}),
  deleteChat: (id: string) => call<{ ok: true }>(`/api/chats/${id}/delete`, {}),
  decide: (id: string, req_id: string, allow: boolean) => call<{ ok: true }>(`/api/chats/${id}/permission`, { req_id, allow }),
};

/** The event stream URL. EventSource cannot send headers, so this one endpoint takes the token in the query. */
export function eventsUrl(id: string, after: number): string {
  return `/api/chats/${encodeURIComponent(id)}/events?after=${after}&token=${encodeURIComponent(TOKEN)}`;
}
