import { Alert, Badge, Button, Code, Table, Text } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { HistoryEntry } from "./types";

export function History() {
  const [rows, setRows] = useState<HistoryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows([...(await api.history())].reverse());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div data-testid="history">
      <Button variant="default" size="xs" mb="sm" onClick={() => void load()}>
        Reload history
      </Button>
      {error && <Alert color="red">{error}</Alert>}
      {rows && rows.length === 0 && <Text c="dimmed">No actions yet.</Text>}
      {rows && rows.length > 0 && (
        <Table.ScrollContainer minWidth={700}>
          <Table verticalSpacing={4}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Started</Table.Th>
                <Table.Th>Command</Table.Th>
                <Table.Th>Exit</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows.flatMap((x) => {
                const r = [
                  <Table.Tr key={x.id}>
                    <Table.Td>{new Date(x.started * 1000).toLocaleString()}</Table.Td>
                    <Table.Td>
                      <Code>{x.argv.join(" ")}</Code>
                    </Table.Td>
                    <Table.Td>
                      <Badge color={x.exit === 0 ? "green" : "red"} variant="light">
                        exit {x.exit}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <Button size="compact-xs" variant="subtle" onClick={() => setOpen(open === x.id ? null : x.id)}>
                        {open === x.id ? "hide output" : "output"}
                      </Button>
                    </Table.Td>
                  </Table.Tr>,
                ];
                if (open === x.id)
                  r.push(
                    <Table.Tr key={`${x.id}-out`}>
                      <Table.Td colSpan={4}>
                        <Code block>{x.output_tail.join("\n") || "(no output)"}</Code>
                      </Table.Td>
                    </Table.Tr>,
                  );
                return r;
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
    </div>
  );
}
