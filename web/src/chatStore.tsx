import { Button, Code, Group, Modal, ScrollArea, Stack, Text } from "@mantine/core";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, eventsUrl } from "./api";
import type { ChatEvent, ChatRow, PendingPermission } from "./types";

/** What the page knows of one chat. Kept per chat, so switching chats loses nothing. */
export interface ChatState {
  events: ChatEvent[];
  lastId: number;
  /** Streamed text of the message being written; cleared when the full message arrives. */
  draft: string;
  connected: boolean;
}

interface Store {
  rows: ChatRow[];
  error: string | null;
  selected: string | null;
  select: (id: string | null) => void;
  state: (id: string) => ChatState;
  reload: () => Promise<void>;
  /** Changes on every event, so the views re-render. */
  version: number;
}

const Ctx = createContext<Store | null>(null);

export function useChats(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("useChats outside ChatsProvider");
  return s;
}

const POLL_MS = 2000;
const empty = (): ChatState => ({ events: [], lastId: 0, draft: "", connected: false });

export function ChatsProvider({ children }: { children: ReactNode }) {
  const [rows, setRows] = useState<ChatRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const states = useRef(new Map<string, ChatState>());
  const lastRows = useRef("");

  const reload = useCallback(async () => {
    try {
      const r = await api.chats();
      const json = JSON.stringify(r);
      if (json !== lastRows.current) {
        lastRows.current = json;
        setRows(r);
      }
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on mount: reload sets state only after its await
    void reload();
    const t = setInterval(() => void reload(), POLL_MS);
    return () => clearInterval(t);
  }, [reload]);

  const state = useCallback((id: string): ChatState => {
    let s = states.current.get(id);
    if (!s) {
      s = empty();
      states.current.set(id, s);
    }
    return s;
  }, []);

  // One stream at a time, for the chat on screen (browsers allow few connections per host).
  // It resumes after the last event this page has; EventSource's own reconnects send Last-Event-ID.
  useEffect(() => {
    if (!selected) return;
    const id = selected;
    const s = state(id);
    const es = new EventSource(eventsUrl(id, s.lastId));
    const bump = () => setVersion((v) => v + 1);
    es.onopen = () => {
      s.connected = true;
      bump();
    };
    es.onerror = () => {
      s.connected = false;
      bump();
    };
    es.onmessage = (m: MessageEvent<string>) => {
      let ev: ChatEvent;
      try {
        ev = JSON.parse(m.data) as ChatEvent;
      } catch {
        return;
      }
      if (ev.id <= s.lastId) return; // already have it
      s.lastId = ev.id;
      if (ev.kind === "delta") s.draft += ev.text;
      else {
        s.events = [...s.events, ev];
        if (ev.kind === "text" || ev.kind === "result" || ev.kind === "user" || ev.kind === "error") s.draft = "";
      }
      if (ev.kind === "status" || ev.kind === "permission" || ev.kind === "permission_decided") void reload();
      bump();
    };
    return () => {
      es.close();
      s.connected = false;
    };
  }, [selected, state, reload]);

  const store = useMemo<Store>(
    () => ({ rows, error, selected, select: setSelected, state, reload, version }),
    [rows, error, selected, state, reload, version],
  );

  return (
    <Ctx.Provider value={store}>
      {children}
      <PermissionModal rows={rows} reload={reload} />
    </Ctx.Provider>
  );
}

/** One modal for every open permission request of every chat, oldest chat first, one at a time. */
function PermissionModal({ rows, reload }: { rows: ChatRow[]; reload: () => Promise<void> }) {
  const [answered, setAnswered] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const queue: PendingPermission[] = [...rows]
    .sort((a, b) => a.created - b.created)
    .flatMap((r) => r.pending)
    .filter((p) => !answered.has(p.req_id));
  const cur = queue[0];
  const chat = cur ? rows.find((r) => r.id === cur.chat) : undefined;

  const decide = async (allow: boolean) => {
    if (!cur) return;
    setBusy(true);
    try {
      await api.decide(cur.chat, cur.req_id, allow);
      setFailure(null);
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setAnswered((s) => new Set(s).add(cur.req_id)); // a request the server no longer has is gone too
      setBusy(false);
      void reload();
    }
  };

  return (
    <Modal
      key={cur?.req_id ?? "none"} // remount per request, so Deny takes the focus each time
      opened={cur !== undefined}
      onClose={() => undefined}
      withCloseButton={false}
      closeOnClickOutside={false}
      closeOnEscape={false}
      title={<Text fw={700}>{cur?.title ?? `Allow ${cur?.tool ?? ""}?`}</Text>}
      size="xl"
      centered
      lockScroll={false} // react-remove-scroll injects an inline <style>, which the CSP refuses
      data-testid="permission-modal"
    >
      {cur && (
        <Stack gap="sm">
          <Text size="sm">
            Chat <b>{chat?.title ?? cur.chat}</b>
            <Text span c="dimmed" size="xs">
              {" "}
              in {chat?.cwd ?? "?"}
            </Text>
          </Text>
          <Text size="sm">
            Tool: <Code data-testid="permission-tool">{cur.tool}</Code>
          </Text>
          {cur.description && <Text size="sm">{cur.description}</Text>}
          {cur.decision_reason && (
            <Text size="sm" c="dimmed">
              Why it asks: {cur.decision_reason}
            </Text>
          )}
          {cur.blocked_path && (
            <Text size="sm" c="dimmed">
              Path: <Code>{cur.blocked_path}</Code>
            </Text>
          )}
          <ScrollArea.Autosize mah="45vh" type="auto">
            <Code block data-testid="permission-input">
              {JSON.stringify(cur.input, null, 2)}
            </Code>
          </ScrollArea.Autosize>
          {failure && (
            <Text c="red" size="sm">
              {failure}
            </Text>
          )}
          <Group justify="space-between">
            <Text size="xs" c="dimmed" data-testid="permission-queue">
              {queue.length > 1 ? `${queue.length - 1} more waiting` : ""}
            </Text>
            <Group>
              <Button color="red" variant="filled" onClick={() => void decide(false)} disabled={busy} data-autofocus data-testid="permission-deny">
                Deny
              </Button>
              <Button variant="default" onClick={() => void decide(true)} disabled={busy} data-testid="permission-allow">
                Allow once
              </Button>
            </Group>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
