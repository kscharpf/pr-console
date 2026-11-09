/** Each chat's transcript position, kept across tab switches (the pane unmounts). */
export const SCROLL = new Map<string, { top: number; stick: boolean }>();

export function forgetScroll(id: string): void {
  SCROLL.delete(id);
}
