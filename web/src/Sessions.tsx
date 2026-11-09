import { Alert, Badge, Button, Card, Code, Group, Loader, Stack, Table, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { enabled, useConfig } from "./config";
import { useDialogs } from "./dialogs";
import { shortPath } from "./status";
import { useJobs } from "./jobs";
import { commandFor, type SessionsResponse, type Worktree } from "./types";


export function Sessions() {
  const cfg = useConfig();
  const showReverify = cfg.commands.reverify_status !== undefined || cfg.conventions.reverify_lock_dir !== undefined;
  const [data, setData] = useState<SessionsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.sessions());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on mount; the synchronous setLoading(true) only shows the spinner
    void load();
  }, [load]);

  return (
    <Stack gap="md" data-testid="sessions">
      <Group>
        <Button variant="default" size="xs" onClick={() => void load()} loading={loading}>
          Reload sessions
        </Button>
        {loading && !data && <Loader size="sm" />}
      </Group>
      {error && <Alert color="red">{error}</Alert>}
      {data &&
        Object.entries(data.groups).map(([group, wts]) => (
          <Card withBorder key={group}>
            <Title order={5} mb="xs">
              {group}
            </Title>
            <Table.ScrollContainer minWidth={800}>
              <Table verticalSpacing={4}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Worktree</Table.Th>
                    <Table.Th>Branch</Table.Th>
                    <Table.Th>PR</Table.Th>
                    <Table.Th>Lock</Table.Th>
                    <Table.Th>Dirty</Table.Th>
                    <Table.Th />
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {wts.map((w) => (
                    <WorktreeRow key={w.path} w={w} onDone={load} />
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          </Card>
        ))}
      {data && showReverify && (
        <Card withBorder>
          <Title order={5}>
            reverify locks{" "}
            <Text span size="xs" c="dimmed">
              {data.reverify_state_dir}
            </Text>
          </Title>
          {data.reverify_locks.length === 0 ? (
            <Text size="sm" c="dimmed">
              none held
            </Text>
          ) : (
            data.reverify_locks.map((l) => (
              <Text size="sm" key={l.pr}>
                #{l.pr} pid {l.holder?.pid ?? "?"}{" "}
                <Badge size="xs" color={l.alive ? "green" : "gray"}>
                  {l.alive ? "alive" : "gone"}
                </Badge>{" "}
                {l.status}
              </Text>
            ))
          )}
          <Title order={6} mt="sm">
            train PRs: reverify --status
          </Title>
          {data.train_reverify_status.length === 0 ? (
            <Text size="sm" c="dimmed">
              no open train
            </Text>
          ) : (
            data.train_reverify_status.map((s) => (
              <Text size="sm" key={s.pr}>
                {s.status}
              </Text>
            ))
          )}
        </Card>
      )}
    </Stack>
  );
}

function WorktreeRow({ w, onDone }: { w: Worktree; onDone: () => Promise<void> }) {
  const cfg = useConfig();
  const { confirm, notify } = useDialogs();
  const { run } = useJobs();
  const [busy, setBusy] = useState(false);

  const cleanup = async () => {
    setBusy(true);
    try {
      const dry = await run({ kind: "cleanup", branch: w.branch, apply: false });
      if (!dry) return;
      if (dry.exit !== 0) {
        // The dry run declined (e.g. the branch's PR is still open): a normal answer, not a failure.
        await notify(`Nothing to clean up: ${w.branch}`, `The cleanup command kept this branch:\n\n${dry.output.join("\n")}`);
        return;
      }
      const apply = { kind: "cleanup", branch: w.branch, apply: true } as const;
      const first = await confirm({
        title: `Clean up ${w.branch}?`,
        message: "Dry run output:",
        output: dry.output.join("\n"),
        command: commandFor(cfg, apply),
        confirmLabel: "Apply",
        danger: true,
      });
      if (!first) return;
      const second = await confirm({
        title: `Really delete ${w.branch}?`,
        message: "This removes the worktree and deletes the branch. It cannot be undone.",
        command: commandFor(cfg, apply),
        confirmLabel: `Delete ${w.branch}`,
        danger: true,
      });
      if (!second) return;
      const done = await run(apply);
      if (done && done.exit !== 0) await notify("Cleanup failed", `exit ${done.exit}; see the log.`);
      await onDone();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Table.Tr data-testid={`wt-${w.branch || w.path}`}>
      <Table.Td>
        <Code>{shortPath(cfg, w.path)}</Code>
      </Table.Td>
      <Table.Td>
        <Code>{w.branch || (w.detached ? "(detached)" : "")}</Code>
      </Table.Td>
      <Table.Td>{w.open_pr ? `#${w.open_pr} open` : w.merged_pr ? `#${w.merged_pr} merged` : ""}</Table.Td>
      <Table.Td>
        {w.locked && (
          <Text size="xs">
            {w.lock_reason}{" "}
            {w.claude_lock && (
              <Badge size="xs" color={w.claude_lock.alive ? "green" : "gray"}>
                {w.claude_lock.alive ? "alive" : "pid gone"}
              </Badge>
            )}
          </Text>
        )}
      </Table.Td>
      <Table.Td>
        {w.dirty === null ? (
          <Badge color="gray">?</Badge>
        ) : w.dirty ? (
          <Badge color="orange">dirty</Badge>
        ) : (
          <Badge color="green" variant="light">
            clean
          </Badge>
        )}
      </Table.Td>
      <Table.Td>
        <Group gap={4} wrap="nowrap">
          {w.ready_to_clean && (
            <Badge color="green" variant="filled">
              ready to clean up
            </Badge>
          )}
          {w.branch && !w.is_main && enabled(cfg, "cleanup") && (
            <Button size="compact-xs" variant="outline" color="red" loading={busy} onClick={() => void cleanup()} data-testid={`cleanup-${w.branch}`}>
              cleanup…
            </Button>
          )}
        </Group>
      </Table.Td>
    </Table.Tr>
  );
}
