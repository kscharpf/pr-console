import { ActionIcon, Alert, Button, Group, Tabs, Text, Title, Tooltip, useComputedColorScheme, useMantineColorScheme } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiFailure } from "./api";
import { ChatsProvider } from "./chatStore";
import { ConfigProvider, useConfig } from "./config";
import { Chats } from "./Chats";
import { DialogProvider } from "./dialogs";
import { History } from "./History";
import { JobProvider } from "./jobs";
import { LogPanel } from "./LogPanel";
import { PrTable } from "./PrTable";
import { Sessions } from "./Sessions";
import { canTrain } from "./status";
import { TrainBuilder } from "./TrainBuilder";
import type { PrsResponse } from "./types";

const AUTO_REFRESH_MS = 15_000;

function ColorToggle() {
  const { setColorScheme } = useMantineColorScheme({ keepTransitions: true }); // else it injects an inline <style>
  const scheme = useComputedColorScheme("light");
  return (
    <Tooltip label={scheme === "dark" ? "Light mode" : "Dark mode"}>
      <ActionIcon variant="default" size="lg" aria-label="Toggle colour scheme" onClick={() => setColorScheme(scheme === "dark" ? "light" : "dark")}>
        {scheme === "dark" ? "☀" : "☾"}
      </ActionIcon>
    </Tooltip>
  );
}

export function App() {
  return (
    <ConfigProvider>
      <DialogProvider>
        <Console />
      </DialogProvider>
    </ConfigProvider>
  );
}

function Console() {
  const cfg = useConfig();
  const [data, setData] = useState<PrsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tab, setTab] = useState<string>("prs");
  const [order, setOrder] = useState<number[]>([]);
  const lastJson = useRef("");

  const loadPrs = useCallback(async () => {
    try {
      const d = await api.prs();
      const json = JSON.stringify(d);
      if (json !== lastJson.current) {
        lastJson.current = json;
        setData(d);
      }
      setLoadError(null);
    } catch (e) {
      setLoadError(
        e instanceof ApiFailure && e.status === 403
          ? "The server refused the token (it may have restarted). Open the URL that server.py printed."
          : e instanceof Error
            ? e.message
            : String(e),
      );
    }
  }, []);

  useEffect(() => {
    void loadPrs();
    const t = setInterval(() => void loadPrs(), AUTO_REFRESH_MS);
    return () => clearInterval(t);
  }, [loadPrs]);

  const prs = data?.prs ?? [];
  // Drop picks that are no longer open or no longer ready for a train.
  useEffect(() => {
    if (!data) return;
    setOrder((o) => {
      const keep = o.filter((n) => data.prs.some((p) => p.number === n && canTrain(cfg, p)));
      return keep.length === o.length ? o : keep;
    });
  }, [data, cfg]);

  const onPick = useCallback((n: number, on: boolean) => {
    setOrder((o) => (on ? (o.includes(n) ? o : [...o, n]) : o.filter((x) => x !== n)));
  }, []);

  const refresh = async () => {
    try {
      await api.refresh();
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
    setTimeout(() => void loadPrs(), 500);
  };

  // A chat links to its PR: show the PR list and bring that row into view.
  const [focusPr, setFocusPr] = useState<number | null>(null);
  const openPr = useCallback((n: number) => {
    setTab("prs");
    setFocusPr(n);
  }, []);
  useEffect(() => {
    if (focusPr === null || tab !== "prs") return;
    const row = document.querySelector(`[data-testid="pr-${focusPr}"]`);
    if (!row) return;
    row.scrollIntoView({ block: "center" });
    row.classList.add("pr-focus");
    const t = setTimeout(() => {
      row.classList.remove("pr-focus");
      setFocusPr(null);
    }, 2500);
    return () => clearTimeout(t);
  }, [focusPr, tab, prs]);

  const trains = prs.filter((p) => p.is_train);
  const meta = data
    ? `${data.refreshing ? "refreshing… " : ""}${data.fetched_at ? `updated ${new Date(data.fetched_at * 1000).toLocaleTimeString()}` : "loading…"}`
    : "loading…";

  return (
    <ChatsProvider>
    <JobProvider onFinished={() => void loadPrs()}>
      {/* No AppShell: it injects an inline <style>, which the CSP refuses. */}
      <header className="header">
        <Group justify="space-between" wrap="nowrap" className="header-inner">
            <Group gap="lg" wrap="nowrap">
              <Title order={3}>PR console</Title>
              <Text size="sm" c="dimmed" data-testid="repo-name">
                {cfg.gh_repo}
              </Text>
              <Tabs value={tab} onChange={(v) => setTab(v ?? "prs")} variant="pills">
                <Tabs.List>
                  <Tabs.Tab value="prs">All PRs</Tabs.Tab>
                  <Tabs.Tab value="sessions">Sessions</Tabs.Tab>
                  <Tabs.Tab value="chats">Chats</Tabs.Tab>
                  <Tabs.Tab value="history">History</Tabs.Tab>
                </Tabs.List>
              </Tabs>
            </Group>
            <Group gap="sm" wrap="nowrap">
              <Text size="sm" c="dimmed" data-testid="meta">
                {meta}
              </Text>
              <Button variant="default" size="xs" onClick={() => void refresh()} data-testid="refresh">
                Refresh
              </Button>
              <ColorToggle />
            </Group>
        </Group>
      </header>
      <main className="main">
          {loadError && (
            <Alert color="red" mb="md" title="Cannot load" data-testid="load-error">
              {loadError}
            </Alert>
          )}
          {data?.error && (
            <Alert color="red" mb="md" title="Refresh failed">
              {data.error}
            </Alert>
          )}
          {trains.length > 0 && (
            <Alert color="yellow" mb="md" title="Open train" data-testid="train-banner">
              {trains.map((t) => (
                <div key={t.number}>
                  #{t.number} {t.title} — {t.status?.line ?? ""}
                </div>
              ))}
            </Alert>
          )}
          {tab === "prs" && (
            <>
              <PrTable prs={prs} picked={order} onPick={onPick} />
              <TrainBuilder prs={prs} order={order} setOrder={setOrder} />
            </>
          )}
          {tab === "sessions" && <Sessions />}
          {tab === "chats" && <Chats onOpenPr={openPr} />}
          {tab === "history" && <History />}
      </main>
      <LogPanel />
    </JobProvider>
    </ChatsProvider>
  );
}
