import { Anchor, Badge, Button, Checkbox, Code, Group, Stack, Table, Text, Tooltip } from "@mantine/core";
import { memo } from "react";
import { useDialogs } from "./dialogs";
import { useJobs } from "./jobs";
import { enabled, useConfig } from "./config";
import { age, canTrain, isRunError, reviewColor, runColor, verdictColor } from "./status";
import { commandFor, type ActionBody, type PrRow } from "./types";

interface Props {
  prs: PrRow[];
  picked: number[];
  onPick: (n: number, on: boolean) => void;
}

export function PrTable({ prs, picked, onPick }: Props) {
  return (
    <Table.ScrollContainer minWidth={1100}>
      <Table striped highlightOnHover verticalSpacing="xs" data-testid="pr-table">
        <Table.Thead>
          <Table.Tr>
            <Table.Th />
            <Table.Th>#</Table.Th>
            <Table.Th>Title</Table.Th>
            <Table.Th>Branch / scope</Table.Th>
            <Table.Th>Author</Table.Th>
            <Table.Th>Head</Table.Th>
            <Table.Th>Age</Table.Th>
            <Table.Th>Labels</Table.Th>
            <Table.Th>Review</Table.Th>
            <Table.Th>Verdict</Table.Th>
            <Table.Th>Actions</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {prs.length === 0 && (
            <Table.Tr>
              <Table.Td colSpan={11}>
                <Text c="dimmed">No open pull requests (yet).</Text>
              </Table.Td>
            </Table.Tr>
          )}
          {prs.map((p) => (
            <PrRowView key={p.number} p={p} picked={picked.includes(p.number)} onPick={onPick} />
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

const PrRowView = memo(function PrRowView({ p, picked, onPick }: { p: PrRow; picked: boolean; onPick: Props["onPick"] }) {
  const cfg = useConfig();
  const v = p.status;
  const rv = p.review;
  return (
    <Table.Tr data-testid={`pr-${p.number}`}>
      <Table.Td>
        {canTrain(cfg, p) && (
          <Checkbox
            checked={picked}
            onChange={(e) => onPick(p.number, e.currentTarget.checked)}
            aria-label={`Add #${p.number} to the train`}
            data-testid={`pick-${p.number}`}
          />
        )}
      </Table.Td>
      <Table.Td>{p.number}</Table.Td>
      <Table.Td>
        <Tooltip label={<Text size="xs" className="pr-summary">{p.summary || "no summary"}</Text>} multiline maw={560} openDelay={300} withinPortal>
          <Text size="sm" span data-testid={`title-${p.number}`}>{p.title}</Text>
        </Tooltip>{" "}
        <Anchor size="xs" href={`${p.url}/files`} target="_blank" rel="noreferrer" data-testid={`diff-${p.number}`}>
          diff ↗
        </Anchor>
        <Text size="sm" span>
          {" "}
          {p.draft && (
            <Badge size="xs" color="gray" variant="outline">
              draft
            </Badge>
          )}
        </Text>
        {p.is_train && (
          <Stack gap={2} mt={4}>
            <Text size="xs" c="dimmed">
              constituents: {p.constituents.length ? p.constituents.map((c) => `#${c}`).join(", ") : "none found"}
            </Text>
            <Group gap={4}>
              <Text size="xs" c="dimmed">
                verify:
              </Text>
              {p.verify_run === null ? (
                <Text size="xs" c="dimmed">
                  no run
                </Text>
              ) : isRunError(p.verify_run) ? (
                <Text size="xs" c="red">
                  {p.verify_run.error}
                </Text>
              ) : (
                <Anchor href={p.verify_run.url} target="_blank" rel="noreferrer" size="xs">
                  <Badge size="xs" color={runColor(p.verify_run)} variant="light">
                    {p.verify_run.status}/{p.verify_run.conclusion || "-"}
                  </Badge>
                </Anchor>
              )}
            </Group>
          </Stack>
        )}
      </Table.Td>
      <Table.Td className="nowrap">
        <Code>{p.branch}</Code>
        <Text size="xs" c="dimmed">
          {p.scope}
        </Text>
      </Table.Td>
      <Table.Td>{p.author}</Table.Td>
      <Table.Td>
        <Code>{p.head.slice(0, 7)}</Code>
      </Table.Td>
      <Table.Td>{age(p.created_at)}</Table.Td>
      <Table.Td className="nowrap">
        <Group gap={4}>
          {p.labels.map((l) => (
            <Badge key={l} size="sm" variant="outline" color={l === cfg.conventions.maintainer_label ? "orange" : "gray"}>
              {l}
            </Badge>
          ))}
        </Group>
      </Table.Td>
      <Table.Td className="nowrap">
        <Badge size="sm" variant="light" color={reviewColor(rv.state)}>
          {rv.state}
        </Badge>
        {rv.state !== "NONE" &&
          (rv.answered ? (
            <Text size="xs" c="green">
              answered
            </Text>
          ) : (
            <Text size="xs" c="red" fw={700}>
              unanswered
            </Text>
          ))}
        <Text size="xs" c="dimmed">
          {rv.author}
        </Text>
      </Table.Td>
      <Table.Td className="nowrap">
        <Tooltip label={v?.line || "no verdict line"} multiline maw={500} openDelay={300}>
          <Badge color={verdictColor(cfg, v?.verdict)} variant="filled" data-testid={`verdict-${p.number}`}>
            {v?.verdict ?? "…"}
          </Badge>
        </Tooltip>
        {v && v.reasons.length > 0 && (
          <Text size="xs" c="dimmed" className="wrap">
            {v.reasons.join("; ")}
          </Text>
        )}
      </Table.Td>
      <Table.Td>
        <Actions p={p} />
      </Table.Td>
    </Table.Tr>
  );
});

function Actions({ p }: { p: PrRow }) {
  const { confirm, prompt } = useDialogs();
  const { run } = useJobs();
  const cfg = useConfig();
  const n = p.number;
  const label = cfg.conventions.maintainer_label ?? "";
  const nm = label !== "" && p.labels.includes(label);

  const ask = async (b: ActionBody, title: string, message: string, confirmLabel: string, danger = true) => {
    if (await confirm({ title, message, command: commandFor(cfg, b), confirmLabel, danger })) void run(b);
  };

  const close = async () => {
    const comment = await prompt({
      title: `Close #${n}`,
      message: p.title,
      label: "Comment (optional)",
      command: (c) => commandFor(cfg, { kind: "close", pr: n, comment: c }),
      confirmLabel: "Close PR",
      danger: true,
    });
    if (comment !== null) void run({ kind: "close", pr: n, comment });
  };

  return (
    <Group gap={4} wrap="wrap">
      {p.is_train && (
        <>
          {enabled(cfg, "reverify") && (
            <Button
              size="compact-xs"
              variant="light"
              data-testid={`reverify-${n}`}
              onClick={() =>
                ask({ kind: "reverify", pr: n }, `Reverify train #${n}`, "Merges main into the train, pushes, and starts a verify run.", "Reverify", false)
              }
            >
              reverify
            </Button>
          )}
          {enabled(cfg, "reverify_cancel") && (
            <Button
              size="compact-xs"
              variant="light"
              color="orange"
              data-testid={`reverify_cancel-${n}`}
              onClick={() =>
                ask({ kind: "reverify_cancel", pr: n }, `Cancel and reverify #${n}`, "Cancels the stale verify run of this train and reverifies it.", "Cancel + reverify")
              }
            >
              cancel+reverify
            </Button>
          )}
          <Button
            size="compact-xs"
            variant="light"
            color="red"
            data-testid={`merge-${n}`}
            onClick={() => ask({ kind: "merge", pr: n }, `MERGE train #${n}`, "Merges this train into main with a merge commit.", "Merge")}
          >
            merge
          </Button>
        </>
      )}
      {enabled(cfg, "label") && (
        <Button
          size="compact-xs"
          variant="light"
          color="gray"
          data-testid={`label-${n}`}
          onClick={() =>
            ask(
              { kind: "label", pr: n, add: !nm },
              `${nm ? "Remove" : "Add"} label ${label} on #${n}`,
              p.title,
              nm ? "Remove label" : "Add label",
              false,
            )
          }
        >
          {nm ? `− ${label}` : `+ ${label}`}
        </Button>
      )}
      <Button size="compact-xs" variant="light" color="red" data-testid={`close-${n}`} onClick={() => void close()}>
        close
      </Button>
    </Group>
  );
}
