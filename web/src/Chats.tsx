import { Alert, Anchor, Badge, Button, Card, Code, Group, NativeSelect, Stack, Text, Textarea, TextInput, Title } from "@mantine/core";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import { useChats, type ChatState } from "./chatStore";
import { useConfig } from "./config";
import { Markdown } from "./Markdown";
import { shortPath } from "./status";
import { MODELS, PERMISSION_MODES, type ChatEvent, type ChatRow, type ChatStatus, type Model, type PermissionMode } from "./types";

const MAX_PROMPT = 16_000; // chats.MAX_PROMPT

const STATUS: Record<ChatStatus, [string, string]> = {
  starting: ["starting", "gray"],
  running: ["running", "blue"],
  waiting: ["waiting for you", "green"],
  permission: ["waiting for permission", "orange"],
  idle: ["idle", "teal"],
  ended: ["ended", "gray"],
  error: ["error", "red"],
};

const live = (s: ChatStatus) => s !== "ended" && s !== "error";
const when = (t: number) => (t ? new Date(t * 1000).toLocaleString() : "");
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function Chats({ onOpenPr }: { onOpenPr: (n: number) => void }) {
  const { rows, error, selected, select } = useChats();
  const [launching, setLaunching] = useState(false);
  const cur = rows.find((r) => r.id === selected) ?? null;

  return (
    <div className="chats" data-testid="chats">
      <div className="chats-list">
        <Stack gap="sm">
          <Group>
            <Button size="xs" onClick={() => setLaunching((l) => !l)} data-testid="chat-new">
              {launching ? "Close" : "New chat"}
            </Button>
          </Group>
          {launching && (
            <Launch
              onDone={(id) => {
                setLaunching(false);
                select(id);
              }}
            />
          )}
          {error && <Alert color="red">{error}</Alert>}
          {rows.length === 0 && !error && (
            <Text c="dimmed" size="sm">
              No chats yet.
            </Text>
          )}
          {rows.map((r) => (
            <ChatCard key={r.id} r={r} active={r.id === selected} onClick={() => select(r.id)} onOpenPr={onOpenPr} />
          ))}
        </Stack>
      </div>
      <div className="chats-pane">
        {cur ? (
          <ChatPane key={cur.id} r={cur} onOpenPr={onOpenPr} />
        ) : (
          <Text c="dimmed">Choose a chat, or start a new one.</Text>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ s }: { s: ChatStatus }) {
  const [label, color] = STATUS[s];
  return (
    <Badge color={color} variant="light" className="nowrap" data-testid="chat-status">
      {label}
    </Badge>
  );
}

function PrLink({ r, onOpenPr }: { r: ChatRow; onOpenPr: (n: number) => void }) {
  if (r.open_pr === null) return null;
  const n = r.open_pr;
  return (
    <Anchor
      component="button"
      size="xs"
      onClick={(e: React.MouseEvent) => {
        e.stopPropagation();
        onOpenPr(n);
      }}
      data-testid={`chat-pr-${n}`}
    >
      #{n}
    </Anchor>
  );
}

function ChatCard({ r, active, onClick, onOpenPr }: { r: ChatRow; active: boolean; onClick: () => void; onOpenPr: (n: number) => void }) {
  const cfg = useConfig();
  return (
    <Card withBorder padding="xs" className={active ? "chat-card chat-card-active" : "chat-card"} onClick={onClick} data-testid={`chat-${r.id}`}>
      <Group justify="space-between" wrap="nowrap" gap="xs">
        <Text size="sm" fw={600} lineClamp={1} data-testid="chat-title">
          {r.title}
        </Text>
        <StatusBadge s={r.status} />
      </Group>
      <Group gap="xs" wrap="wrap">
        <Code>{shortPath(cfg, r.cwd)}</Code>
        {r.branch && <Code>{r.branch}</Code>}
        <PrLink r={r} onOpenPr={onOpenPr} />
      </Group>
      <Text size="xs" c="dimmed">
        last activity {when(r.last_activity)}
      </Text>
    </Card>
  );
}

function Launch({ onDone }: { onDone: (id: string) => void }) {
  const cfg = useConfig();
  const [cwd, setCwd] = useState("");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState<Model>("");
  const [mode, setMode] = useState<PermissionMode>("auto");
  const [resumeId, setResumeId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { reload } = useChats();

  useEffect(() => {
    api
      .chatDirs()
      .then((d) => {
        setCwd((c) => c || (d.find((x) => x.is_main) ?? d[0])?.path || "");
      })
      .catch((e: unknown) => setError(errText(e)));
  }, []);

  const go = async (f: () => Promise<ChatRow>) => {
    setBusy(true);
    try {
      const r = await f();
      setError(null);
      await reload();
      onDone(r.id);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card withBorder data-testid="chat-launch">
      <Stack gap="xs">
        <Title order={6}>New coordinator chat</Title>
        <Text size="sm" c="dimmed" data-testid="chat-role">
          Coordinator session in the main checkout <Code>{cwd || "…"}</Code>
          {cfg.coordinator.description ? `: ${cfg.coordinator.description}` : "."}
        </Text>
        <Textarea
          label="First prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.currentTarget.value)}
          autosize
          minRows={3}
          maxLength={MAX_PROMPT}
          data-testid="chat-prompt"
        />
        <Group grow>
          <NativeSelect
            label="Model"
            value={model}
            onChange={(e) => setModel(e.currentTarget.value as Model)}
            data={MODELS.map((m) => ({ value: m, label: m || "CLI default" }))}
            data-testid="chat-model"
          />
          <NativeSelect
            label="Permission mode"
            description="auto matches your terminal setting"
            value={mode}
            onChange={(e) => setMode(e.currentTarget.value as PermissionMode)}
            data={PERMISSION_MODES.map((m) => ({ value: m, label: { auto: "auto (classifier approves routine actions, asks or blocks risky ones)", default: "default (asks for each tool)", plan: "plan (read-only, then asks)" }[m] }))}
            data-testid="chat-mode"
          />
        </Group>
        <Button
          onClick={() => void go(() => api.launch({ cwd, prompt, model, permission_mode: mode }))}
          disabled={!cwd || !prompt.trim() || busy}
          loading={busy}
          data-testid="chat-launch-go"
        >
          Launch
        </Button>
        <Group align="flex-end" wrap="nowrap">
          <TextInput
            label="Or resume a session by ID (in the directory above)"
            value={resumeId}
            onChange={(e) => setResumeId(e.currentTarget.value.trim())}
            placeholder="00000000-0000-0000-0000-000000000000"
            className="grow"
            data-testid="chat-resume-id"
          />
          <Button variant="default" onClick={() => void go(() => api.resume(resumeId, cwd))} disabled={!resumeId || busy} data-testid="chat-resume-go">
            Resume
          </Button>
        </Group>
        {error && (
          <Alert color="red" data-testid="chat-launch-error">
            {error}
          </Alert>
        )}
      </Stack>
    </Card>
  );
}

function ChatPane({ r, onOpenPr }: { r: ChatRow; onOpenPr: (n: number) => void }) {
  const { state } = useChats();
  const s = state(r.id);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const card = useRef<HTMLDivElement>(null);
  // Fit the pane between where it starts and the top of the fixed log panel, measured rather
  // than assumed: fonts and the rows above it differ between platforms.
  const [height, setHeight] = useState<number | null>(null);
  useLayoutEffect(() => {
    const fit = () => {
      const el = card.current;
      if (!el) return;
      const log = document.querySelector(".log");
      const bottom = log ? log.getBoundingClientRect().top : window.innerHeight;
      setHeight(Math.max(320, Math.floor(bottom - el.getBoundingClientRect().top - 12)));
    };
    fit();
    const ro = new ResizeObserver(fit);
    const log = document.querySelector(".log");
    if (log) ro.observe(log);
    window.addEventListener("resize", fit);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", fit);
    };
  }, []);
  const stick = useRef(true); // follow new output unless the reader has scrolled up
  const lastTop = useRef(0); // scrollTop after our own scroll, to tell the reader's scrolls from ours

  // Leaving the tab unmounts the pane; restore each chat's place when it comes back.
  useEffect(() => {
    const saved = SCROLL.get(r.id);
    stick.current = saved?.stick ?? true;
    lastTop.current = saved?.top ?? 0;
  }, [r.id]);
  useEffect(() => {
    const el = box.current;
    if (!el || height === null) return; // not scrollable until the pane has its measured height
    if (stick.current) el.scrollTop = el.scrollHeight; // scroll the transcript only, never the page
    else el.scrollTop = SCROLL.get(r.id)?.top ?? el.scrollTop;
    lastTop.current = el.scrollTop;
  }, [r.id, s.events.length, s.draft, height]);

  const act = async (f: () => Promise<unknown>, clear = false) => {
    setBusy(true);
    try {
      await f();
      setError(null);
      if (clear) setText("");
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const running = r.status === "running" || r.status === "permission" || r.status === "starting";
  return (
    <Card withBorder data-testid="chat-pane" className="chat-pane-card" ref={card} h={height ?? undefined}>
      <Stack gap="xs" className="chat-pane-stack">
        <Group justify="space-between" wrap="nowrap">
          <Title order={5} lineClamp={1}>
            {r.title}
          </Title>
          <StatusBadge s={r.status} />
        </Group>
        <Group gap="xs">
          <Code>{r.cwd}</Code>
          {r.branch && <Code>{r.branch}</Code>}
          <PrLink r={r} onOpenPr={onOpenPr} />
          <Text size="xs" c="dimmed">
            {r.model || "default model"} · {r.permission_mode} · id <span data-testid="chat-id">{r.id}</span>
          </Text>
        </Group>
        <div
          className="transcript"
          data-testid="transcript"
          ref={box}
          onScroll={(e) => {
            const el = e.currentTarget;
            // Content growing under us is not the reader scrolling: only an upward scroll unsticks.
            if (el.scrollHeight - el.scrollTop - el.clientHeight < 40) stick.current = true;
            else if (el.scrollTop < lastTop.current - 2) stick.current = false;
            lastTop.current = el.scrollTop;
            SCROLL.set(r.id, { top: el.scrollTop, stick: stick.current });
          }}
        >
          <Transcript s={s} />
        </div>
        {error && (
          <Alert color="red" data-testid="chat-error">
            {error}
          </Alert>
        )}
        <Activity s={s} status={r.status} lastActivity={r.last_activity} />
        <Textarea
          placeholder={live(r.status) ? "Message" : "Message (resumes the session)"}
          value={text}
          onChange={(e) => setText(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) void act(() => api.send(r.id, text), true);
          }}
          autosize
          minRows={2}
          maxLength={MAX_PROMPT}
          data-testid="chat-input"
        />
        <Group justify="space-between">
          <Text size="xs" c="dimmed">
            ⌘/Ctrl+Enter sends
          </Text>
          <Group gap="xs">
            {!live(r.status) && (
              <Button variant="default" size="xs" onClick={() => void act(() => api.resume(r.id))} disabled={busy} data-testid="chat-resume">
                Resume
              </Button>
            )}
            {live(r.status) && (
              <Button variant="default" size="xs" onClick={() => void act(() => api.end(r.id))} disabled={busy} data-testid="chat-end">
                End session
              </Button>
            )}
            <Button color="orange" variant="light" size="xs" onClick={() => void act(() => api.interrupt(r.id))} disabled={!running || busy} data-testid="chat-interrupt">
              Interrupt
            </Button>
            <Button size="xs" onClick={() => void act(() => api.send(r.id, text), true)} disabled={!text.trim() || busy} data-testid="chat-send">
              Send
            </Button>
          </Group>
        </Group>
      </Stack>
    </Card>
  );
}

/** Each chat's transcript position, kept across tab switches (the pane unmounts). */
const SCROLL = new Map<string, { top: number; stick: boolean }>();

function Stamp({ t }: { t: number }) {
  if (!t) return <span className="stamp" />;
  const d = new Date(t * 1000);
  return (
    <span className="stamp" title={d.toLocaleString()} data-testid="msg-time">
      {d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
    </span>
  );
}

const ago = (secs: number) => {
  const v = Math.max(0, Math.floor(secs));
  return v < 60 ? `${v}s` : v < 3600 ? `${Math.floor(v / 60)}m ${v % 60}s` : `${Math.floor(v / 3600)}h ${Math.floor((v % 3600) / 60)}m`;
};

/** While a turn runs: how long it has been working and how long since the last output. */
function Activity({ s, status, lastActivity }: { s: ChatState; status: ChatStatus; lastActivity: number }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const h = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(h);
  }, []);
  if (status !== "running" && status !== "starting" && status !== "permission") return null;
  let turnStart = 0;
  for (const e of s.events) if (e.kind === "user") turnStart = e.t;
  const quiet = now - lastActivity;
  return (
    <Text size="xs" c={quiet > 120 ? "orange" : "dimmed"} data-testid="chat-activity">
      {status === "permission" ? "waiting for your permission" : "working"}
      {turnStart ? ` for ${ago(now - turnStart)}` : ""} · last output {ago(quiet)} ago
    </Text>
  );
}

function Transcript({ s }: { s: ChatState }) {
  const results = new Map<string, Extract<ChatEvent, { kind: "tool_result" }>>();
  const decided = new Map<string, boolean>();
  for (const e of s.events) {
    if (e.kind === "tool_result") results.set(e.tool_use_id, e);
    if (e.kind === "permission_decided") decided.set(e.req_id, e.allow);
  }
  const out: ReactNode[] = [];
  // Every message carries the time it arrived, so a quiet session reads as quiet, not hung.
  const push = (e: ChatEvent, node: ReactNode) =>
    out.push(
      <div key={`row-${e.id}`} className="msg-row">
        <Stamp t={e.t} />
        <div className="msg-cell">{node}</div>
      </div>,
    );
  for (const e of s.events) {
    const k = String(e.id);
    switch (e.kind) {
      case "user":
        push(e, 
          <div key={k} className="msg msg-user" data-testid="msg-user">
            <Markdown text={e.text} />
          </div>,
        );
        break;
      case "text":
        push(e, 
          <div key={k} className={e.sub ? "msg msg-assistant msg-sub" : "msg msg-assistant"} data-testid="msg-assistant">
            <Markdown text={e.text} />
          </div>,
        );
        break;
      case "tool_use": {
        const res = results.get(e.tool_use_id);
        push(e, 
          <div key={k} className="msg msg-tool" data-testid="msg-tool">
            <Text size="sm">
              <Code>{e.name}</Code>{" "}
              {res === undefined ? (
                <Badge size="xs" color="gray" variant="light">
                  pending
                </Badge>
              ) : res.is_error ? (
                <Badge size="xs" color="red" variant="light">
                  error
                </Badge>
              ) : (
                <Badge size="xs" color="green" variant="light">
                  done
                </Badge>
              )}
            </Text>
            <Code block className="tool-input">
              {JSON.stringify(e.input, null, 2)}
            </Code>
            {res !== undefined && (
              <details data-testid="tool-result">
                <summary>result</summary>
                <Code block>{res.content || "(empty)"}</Code>
              </details>
            )}
          </div>,
        );
        break;
      }
      case "permission": {
        const d = decided.get(e.req_id);
        push(e, 
          <Text key={k} size="xs" c={d === undefined ? "orange" : d ? "green" : "red"} data-testid="msg-permission">
            permission for {e.tool}: {d === undefined ? "waiting" : d ? "allowed" : "denied"}
          </Text>,
        );
        break;
      }
      case "hook":
        push(e, 
          <Alert key={k} color="orange" title={`hook ${e.name || e.event}: ${e.outcome ?? ""}${e.exit_code !== null ? ` (exit ${e.exit_code})` : ""}`} data-testid="msg-hook">
            <pre className="pre-wrap">{e.output}</pre>
          </Alert>,
        );
        break;
      case "error":
        push(e, 
          <Alert key={k} color="red" title="error" data-testid="msg-error">
            <pre className="pre-wrap">{e.message}</pre>
          </Alert>,
        );
        break;
      case "result":
        push(e, 
          <Text key={k} size="xs" c={e.is_error ? "red" : "dimmed"} data-testid="msg-result">
            {e.terminal_reason?.startsWith("aborted") ? "interrupted" : e.is_error ? `turn failed: ${e.text ?? e.subtype}` : "turn done"}
            {e.cost !== null ? ` · $${e.cost.toFixed(4)}` : ""} · {(e.duration_ms / 1000).toFixed(1)} s
            {e.errors.length > 0 ? ` · ${e.errors.join("; ")}` : ""}
          </Text>,
        );
        break;
      case "info":
      case "system":
        push(e, 
          <Text key={k} size="xs" c="dimmed">
            {e.text}
          </Text>,
        );
        break;
      case "status":
      case "delta":
      case "permission_decided":
      case "tool_result":
        break;
    }
  }
  if (s.draft)
    out.push(
      <div key="draft" className="msg msg-assistant msg-draft" data-testid="msg-draft">
        <Markdown text={s.draft} />
      </div>,
    );
  return <>{out}</>;
}
