import { useMediaQuery } from "@mantine/hooks";

/** Phone-sized screens: Mantine's `sm` breakpoint. Keep in step with the 48em media queries in app.css. */
export function useNarrow(): boolean {
  return useMediaQuery("(max-width: 48em)", false, { getInitialValueInEffect: false }) ?? false;
}
