import { ActionIcon, Badge, Code, Group, Paper, Text } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import { useJobs } from "./jobs";

export function LogPanel() {
  const { current } = useJobs();
  const [collapsed, setCollapsed] = useState(false);
  const body = useRef<HTMLDivElement>(null);
  const lines = current?.output.length ?? 0;

  useEffect(() => {
    const el = body.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines, collapsed]);

  return (
    <Paper className={collapsed ? "log log-collapsed" : "log"} withBorder shadow="md" data-testid="log">
      <Group justify="space-between" wrap="nowrap" className="log-head">
        <Group gap="xs" wrap="nowrap" className="log-title">
          <Text fw={700} size="sm">
            Log
          </Text>
          {current && (
            <>
              <Code className="log-argv" data-testid="log-argv">
                {current.argv.join(" ")}
              </Code>
              {current.exit === null ? (
                <Badge color="yellow" variant="light">
                  running
                </Badge>
              ) : (
                <Badge color={current.exit === 0 ? "green" : "red"} variant="light" data-testid="log-exit">
                  exit {current.exit}
                </Badge>
              )}
            </>
          )}
        </Group>
        <ActionIcon variant="subtle" onClick={() => setCollapsed((c) => !c)} aria-label={collapsed ? "Expand log" : "Collapse log"}>
          {collapsed ? "▴" : "▾"}
        </ActionIcon>
      </Group>
      {!collapsed && (
        <div className="log-body" ref={body}>
          <pre data-testid="log-output">{current ? current.output.join("\n") : "No action run yet."}</pre>
        </div>
      )}
    </Paper>
  );
}
