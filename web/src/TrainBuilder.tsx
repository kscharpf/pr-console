import { ActionIcon, Alert, Button, Card, Code, Group, Stack, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { enabled, hasDryRun, useConfig } from "./config";
import { useDialogs } from "./dialogs";
import { useJobs } from "./jobs";
import { commandFor, type ActionBody, type PrRow } from "./types";

interface Props {
  prs: PrRow[];
  order: number[];
  setOrder: (o: number[]) => void;
}

function openTrainWarning(prs: PrRow[]): string | null {
  const t = prs.filter((p) => p.is_train);
  return t.length ? `Train ${t.map((x) => `#${x.number}`).join(", ")} is already open.` : null;
}

function move(order: number[], from: number, to: number): number[] {
  if (from === to || from < 0 || to < 0 || from >= order.length || to >= order.length) return order;
  const next = [...order];
  const [x] = next.splice(from, 1);
  if (x === undefined) return order;
  next.splice(to, 0, x);
  return next;
}

export function TrainBuilder(props: Props) {
  const cfg = useConfig();
  if (!enabled(cfg, "train") && !enabled(cfg, "rebuild")) return null;
  return <Builder {...props} />;
}

function Builder({ prs, order, setOrder }: Props) {
  const cfg = useConfig();
  const { confirm } = useDialogs();
  const { run } = useJobs();
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const warning = openTrainWarning(prs);

  const build = async () => {
    const b: ActionBody = { kind: "train", prs: order, dry_run: false };
    const ok = await confirm({
      title: "Build train",
      message: (
        <Stack gap="xs">
          {warning && (
            <Alert color="yellow" title="Warning" data-testid="train-open-warning">
              {warning}
            </Alert>
          )}
          <Text>Builds and pushes a train of {order.map((n) => `#${n}`).join(", ")} and opens its PR.</Text>
        </Stack>
      ),
      command: commandFor(cfg, b),
      confirmLabel: "Build and push",
      danger: true,
    });
    if (ok) void run(b);
  };

  return (
    <Card withBorder mt="md" data-testid="train-builder">
      {enabled(cfg, "train") && (
        <>
          <Title order={4}>Train builder</Title>
          <Text size="sm" c="dimmed">
            Tick {cfg.verdicts.ready_for_train} PRs in the table; order them here by dragging or with the arrows.
          </Text>
          {order.length === 0 ? (
            <Text size="sm" mt="xs" c="dimmed">
              No PR picked.
            </Text>
          ) : (
            <ol className="train-list" data-testid="train-list">
              {order.map((n, i) => (
                <li
                  key={n}
                  draggable
                  data-testid={`train-item-${n}`}
                  className={dragFrom === i ? "dragging" : undefined}
                  onDragStart={(e) => {
                    setDragFrom(i);
                    e.dataTransfer.effectAllowed = "move";
                    e.dataTransfer.setData("text/plain", String(i));
                  }}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => {
                    e.preventDefault();
                    const from = dragFrom ?? Number(e.dataTransfer.getData("text/plain"));
                    setOrder(move(order, from, i));
                    setDragFrom(null);
                  }}
                  onDragEnd={() => setDragFrom(null)}
                >
                  <Group gap="xs" wrap="nowrap">
                    <span className="grip" aria-hidden>
                      ⠿
                    </span>
                    <Text size="sm" className="train-title">
                      #{n} {prs.find((p) => p.number === n)?.title ?? "(no longer open)"}
                    </Text>
                    <ActionIcon size="sm" variant="subtle" aria-label={`Move #${n} up`} disabled={i === 0} onClick={() => setOrder(move(order, i, i - 1))}>
                      ↑
                    </ActionIcon>
                    <ActionIcon
                      size="sm"
                      variant="subtle"
                      aria-label={`Move #${n} down`}
                      disabled={i === order.length - 1}
                      onClick={() => setOrder(move(order, i, i + 1))}
                    >
                      ↓
                    </ActionIcon>
                  </Group>
                </li>
              ))}
            </ol>
          )}
          {warning && (
            <Alert color="yellow" mt="xs" p="xs">
              {warning}
            </Alert>
          )}
          <Group mt="sm">
            {hasDryRun(cfg, "train") && (
              <Button variant="default" disabled={!order.length} data-testid="train-dry" onClick={() => void run({ kind: "train", prs: order, dry_run: true })}>
                Dry run
              </Button>
            )}
            <Button color="red" disabled={!order.length} data-testid="train-build" onClick={() => void build()}>
              Build train
            </Button>
            {order.length > 0 && <Code>{commandFor(cfg, { kind: "train", prs: order, dry_run: false })}</Code>}
          </Group>
        </>
      )}
      {enabled(cfg, "rebuild") && <Rebuild prs={prs} />}
    </Card>
  );
}

const parseNums = (s: string): number[] | null => {
  const parts = s.split(/[\s,#]+/).filter(Boolean);
  if (!parts.every((x) => /^\d{1,6}$/.test(x))) return null;
  return parts.map(Number);
};

function Rebuild({ prs }: { prs: PrRow[] }) {
  const cfg = useConfig();
  const { confirm } = useDialogs();
  const { run } = useJobs();
  const firstTrain = prs.find((p) => p.is_train)?.number;
  const [train, setTrain] = useState("");
  const [without, setWithout] = useState("");
  const t = (train.trim() || (firstTrain !== undefined ? String(firstTrain) : "")).replace(/^#/, "");
  const w = parseNums(without);
  const valid = /^\d{1,6}$/.test(t) && w !== null && w.length > 0;

  const go = async (dry: boolean) => {
    if (!valid || w === null) return;
    const b: ActionBody = { kind: "rebuild", train: Number(t), without: w, dry_run: dry };
    if (!dry) {
      const ok = await confirm({
        title: `Rebuild train #${t}`,
        message: `Rebuilds the train without ${w.map((x) => `#${x}`).join(", ")}. The old train PR is closed.`,
        command: commandFor(cfg, b),
        confirmLabel: "Rebuild",
        danger: true,
      });
      if (!ok) return;
    }
    void run(b);
  };

  return (
    <Stack gap="xs" mt="lg">
      <Title order={5}>Rebuild</Title>
      <Group align="flex-end">
        <TextInput
          label="Train PR"
          placeholder={firstTrain !== undefined ? String(firstTrain) : "e.g. 371"}
          value={train}
          onChange={(e) => setTrain(e.currentTarget.value)}
          w={120}
          data-testid="rb-train"
        />
        <TextInput
          label="Without"
          placeholder="e.g. 351 352"
          value={without}
          onChange={(e) => setWithout(e.currentTarget.value)}
          error={w === null ? "PR numbers only" : undefined}
          w={220}
          data-testid="rb-without"
        />
        {hasDryRun(cfg, "rebuild") && (
          <Button variant="default" disabled={!valid} data-testid="rb-dry" onClick={() => void go(true)}>
            Dry run
          </Button>
        )}
        <Button color="red" disabled={!valid} data-testid="rb-run" onClick={() => void go(false)}>
          Rebuild
        </Button>
      </Group>
    </Stack>
  );
}
