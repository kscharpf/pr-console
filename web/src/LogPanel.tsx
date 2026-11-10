import { ActionIcon, Badge, Code, Group, Paper, Text } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import { useJobs } from "./jobs";

export function LogPanel() {
  const { current } = useJobs();
  const [collapsed, setCollapsed] = useState(false);
  const [height, setHeight] = useState<number | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const lines = current?.output.length ?? 0;

  useEffect(() => {
    const el = body.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines, collapsed]);

  useEffect(() => {
    return () => {
      cleanupRef.current?.();
    };
  }, []);

  useEffect(() => {
    if (!collapsed && height !== null) {
      document.documentElement.style.setProperty("--log-height", `${height}px`);
    } else if (collapsed) {
      document.documentElement.style.setProperty("--log-height", "40px");
    } else {
      document.documentElement.style.removeProperty("--log-height");
    }
    return () => {
      document.documentElement.style.removeProperty("--log-height");
    };
  }, [height, collapsed]);

  useEffect(() => {
    const onResize = () => {
      setHeight((h) => {
        if (h === null) return null;
        const maxH = Math.max(70, Math.floor(window.innerHeight * 0.8));
        return Math.min(h, maxH);
      });
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const onSplitterPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();

    cleanupRef.current?.();
    setIsDragging(true);

    const minHeight = 70;
    const maxHeight = Math.max(minHeight, Math.floor(window.innerHeight * 0.8));

    const onPointerMove = (ev: PointerEvent) => {
      const rawHeight = window.innerHeight - ev.clientY;
      const clamped = Math.max(minHeight, Math.min(maxHeight, Math.round(rawHeight)));
      setHeight(clamped);
    };

    const cleanup = () => {
      setIsDragging(false);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", cleanup);
      window.removeEventListener("pointercancel", cleanup);
      document.body.style.removeProperty("user-select");
      document.body.style.removeProperty("cursor");
      cleanupRef.current = null;
    };

    cleanupRef.current = cleanup;
    document.body.style.setProperty("user-select", "none");
    document.body.style.setProperty("cursor", "row-resize");
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", cleanup);
    window.addEventListener("pointercancel", cleanup);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const currentHeight = height ?? (panelRef.current ? Math.round(panelRef.current.getBoundingClientRect().height) : Math.round(window.innerHeight * 0.28));
    const minHeight = 70;
    const maxHeight = Math.max(minHeight, Math.floor(window.innerHeight * 0.8));
    const step = e.shiftKey ? 32 : 16;

    if (e.key === "ArrowUp") {
      e.preventDefault();
      const next = Math.max(minHeight, Math.min(maxHeight, currentHeight + step));
      setHeight(next);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = Math.max(minHeight, Math.min(maxHeight, currentHeight - step));
      setHeight(next);
    } else if (e.key === "Home" || e.key === "Enter") {
      e.preventDefault();
      setHeight(null);
    }
  };

  return (
    <Paper
      ref={panelRef}
      className={collapsed ? "log log-collapsed" : "log"}
      style={{ height: !collapsed && height !== null ? `${height}px` : undefined }}
      withBorder
      shadow="md"
      data-testid="log"
    >
      {!collapsed && (
        <div
          className={`log-splitter${isDragging ? " log-splitter-active" : ""}`}
          data-testid="log-splitter"
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize log panel"
          aria-valuenow={height ?? undefined}
          tabIndex={0}
          onPointerDown={onSplitterPointerDown}
          onKeyDown={onKeyDown}
          onDoubleClick={() => setHeight(null)}
        >
          <div className="log-splitter-bar" />
        </div>
      )}
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
