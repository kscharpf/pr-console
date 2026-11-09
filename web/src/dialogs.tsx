import { Button, Code, Group, Modal, ScrollArea, Stack, Text, Textarea } from "@mantine/core";
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { useConfig } from "./config";

// In-page dialogs only: the browser may silently suppress confirm(), alert() and prompt().

export interface ConfirmOptions {
  title: string;
  message?: ReactNode;
  /** The exact command that confirming runs. */
  command?: string;
  /** Replaces "Runs in the <repo> checkout:" above the command, for actions that run no command. */
  commandLabel?: string;
  /** Command output to show, e.g. of a dry run. */
  output?: string;
  confirmLabel?: string;
  danger?: boolean;
}

export interface PromptOptions extends Omit<ConfirmOptions, "command"> {
  label: string;
  /** The command for the current input. */
  command: (value: string) => string;
}

interface Dialogs {
  confirm: (o: ConfirmOptions) => Promise<boolean>;
  prompt: (o: PromptOptions) => Promise<string | null>;
  notify: (title: string, message: string) => Promise<void>;
}

type Open =
  | { type: "confirm"; o: ConfirmOptions; done: (v: boolean) => void }
  | { type: "prompt"; o: PromptOptions; done: (v: string | null) => void }
  | { type: "notify"; title: string; message: string; done: () => void };

function dismiss(o: Open | null): void {
  if (!o) return;
  if (o.type === "confirm") o.done(false);
  else if (o.type === "prompt") o.done(null);
  else o.done();
}

const Ctx = createContext<Dialogs | null>(null);

export function useDialogs(): Dialogs {
  const d = useContext(Ctx);
  if (!d) throw new Error("useDialogs outside DialogProvider");
  return d;
}

export function DialogProvider({ children }: { children: ReactNode }) {
  const cfg = useConfig();
  const [open, setOpen] = useState<Open | null>(null);
  const [text, setText] = useState("");
  const openRef = useRef<Open | null>(null);

  const show = useCallback((o: Open) => {
    dismiss(openRef.current);
    openRef.current = o;
    setText("");
    setOpen(o);
  }, []);

  const dialogs: Dialogs = {
    confirm: useCallback((o) => new Promise<boolean>((done) => show({ type: "confirm", o, done })), [show]),
    prompt: useCallback((o) => new Promise<string | null>((done) => show({ type: "prompt", o, done })), [show]),
    notify: useCallback((title, message) => new Promise<void>((done) => show({ type: "notify", title, message, done })), [show]),
  };

  const finish = (ok: boolean) => {
    const o = openRef.current;
    openRef.current = null;
    setOpen(null);
    if (!ok) dismiss(o);
    else if (o?.type === "confirm") o.done(true);
    else if (o?.type === "prompt") o.done(text);
    else o?.done();
  };

  const opts = open && open.type !== "notify" ? open.o : null;
  const command = open?.type === "confirm" ? open.o.command : open?.type === "prompt" ? open.o.command(text) : undefined;

  return (
    <Ctx.Provider value={dialogs}>
      {children}
      <Modal
        opened={open !== null}
        onClose={() => finish(false)}
        title={<Text fw={700}>{open?.type === "notify" ? open.title : opts?.title}</Text>}
        size="lg"
        centered
        lockScroll={false} // react-remove-scroll injects an inline <style>, which the CSP refuses
        data-testid="dialog"
      >
        <Stack gap="sm">
          {open?.type === "notify" && <Text style={{ whiteSpace: "pre-wrap" }}>{open.message}</Text>}
          {opts?.message && <div>{opts.message}</div>}
          {opts?.output !== undefined && (
            <ScrollArea.Autosize mah="40vh" type="auto">
              <Code block data-testid="dialog-output">
                {opts.output || "(no output)"}
              </Code>
            </ScrollArea.Autosize>
          )}
          {open?.type === "prompt" && (
            <Textarea
              label={open.o.label}
              value={text}
              onChange={(e) => setText(e.currentTarget.value)}
              autosize
              minRows={2}
              data-autofocus
              data-testid="dialog-input"
            />
          )}
          {command !== undefined && (
            <div>
              <Text size="sm" c="dimmed">
                {open?.type === "confirm" && open.o.commandLabel ? open.o.commandLabel : `Runs in the ${cfg.name} checkout:`}
              </Text>
              <Code block data-testid="dialog-command">
                {command}
              </Code>
            </div>
          )}
          <Group justify="flex-end">
            {open?.type !== "notify" && (
              <Button variant="default" onClick={() => finish(false)} data-testid="dialog-cancel">
                Cancel
              </Button>
            )}
            <Button
              color={opts?.danger ? "red" : "blue"}
              onClick={() => finish(true)}
              data-testid="dialog-confirm"
              data-autofocus={open?.type === "prompt" ? undefined : true}
            >
              {open?.type === "notify" ? "OK" : (opts?.confirmLabel ?? "Confirm")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Ctx.Provider>
  );
}
