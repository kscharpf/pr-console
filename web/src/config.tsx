import { Alert, Text } from "@mantine/core";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiFailure } from "./api";
import type { ActionKind, RepoConfig } from "./types";

const Ctx = createContext<RepoConfig | null>(null);

/** The repo config of GET /api/config (config.public); the page renders only once it has it. */
export function useConfig(): RepoConfig {
  const c = useContext(Ctx);
  if (!c) throw new Error("useConfig outside ConfigProvider");
  return c;
}

export const enabled = (cfg: RepoConfig, kind: ActionKind): boolean => cfg.actions.includes(kind);

/** Whether a command takes the dry-run flag. */
export const hasDryRun = (cfg: RepoConfig, name: "train" | "rebuild"): boolean =>
  (cfg.commands[name] ?? []).some((a) => a === "{dry_run}");

export function ConfigProvider({ children }: { children: ReactNode }) {
  const [cfg, setCfg] = useState<RepoConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .config()
      .then(setCfg)
      .catch((e: unknown) =>
        setError(
          e instanceof ApiFailure && e.status === 403
            ? "The server refused the token (it may have restarted). Open the URL that server.py printed."
            : e instanceof Error
              ? e.message
              : String(e),
        ),
      );
  }, []);

  if (error)
    return (
      <Alert color="red" m="md" title="Cannot load the config" data-testid="load-error">
        {error}
      </Alert>
    );
  if (!cfg)
    return (
      <Text m="md" c="dimmed">
        loading…
      </Text>
    );
  return <Ctx.Provider value={cfg}>{children}</Ctx.Provider>;
}
