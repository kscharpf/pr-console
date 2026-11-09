import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiFailure } from "./api";
import { useDialogs } from "./dialogs";
import type { ActionBody, Job } from "./types";

interface Jobs {
  /** The job the log panel follows. */
  current: Job | null;
  /** POST an action and follow its job; resolves to the finished job, or null on an error. */
  run: (b: ActionBody) => Promise<Job | null>;
}

const Ctx = createContext<Jobs | null>(null);

export function useJobs(): Jobs {
  const j = useContext(Ctx);
  if (!j) throw new Error("useJobs outside JobProvider");
  return j;
}

const POLL_MS = 1000;

export function JobProvider({ children, onFinished }: { children: ReactNode; onFinished: () => void }) {
  const [current, setCurrent] = useState<Job | null>(null);
  const following = useRef<string | null>(null);
  const { notify } = useDialogs();
  const finishedRef = useRef(onFinished);
  useEffect(() => {
    finishedRef.current = onFinished;
  }, [onFinished]);

  const run = useCallback(
    async (b: ActionBody): Promise<Job | null> => {
      let id: string;
      try {
        const started = await api.action(b);
        id = started.id;
        setCurrent({ id, key: "", argv: started.argv, started: Date.now() / 1000, output: [], exit: null, ended: null });
      } catch (e) {
        await notify("Action refused", e instanceof Error ? e.message : String(e));
        return null;
      }
      following.current = id;
      // bound: polls until the server reports an exit code for the job
      for (;;) {
        await new Promise((ok) => setTimeout(ok, POLL_MS));
        let j: Job;
        try {
          j = await api.job(id);
        } catch (e) {
          if (e instanceof ApiFailure && e.status === 404) return null; // the server restarted
          continue;
        }
        if (following.current === id) setCurrent(j);
        if (j.exit !== null) {
          finishedRef.current();
          return j;
        }
      }
    },
    [notify],
  );

  return <Ctx.Provider value={{ current, run }}>{children}</Ctx.Provider>;
}
