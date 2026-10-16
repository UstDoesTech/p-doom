/** Doom's own order: M1 to M8, then the next episode's M1. M9 is the secret level and leads back
 * into the episode. null after the last level of episode 4. */
const SECRET_RETURNS: Record<number, string> = { 1: "E1M4", 2: "E2M6", 3: "E3M7", 4: "E4M3" };

export function nextLevel(level: string): string | null {
  const m = /^E(\d)M(\d)$/.exec(level);
  if (!m) return null;
  const ep = Number(m[1]);
  const map = Number(m[2]);
  if (map === 9) return SECRET_RETURNS[ep] ?? null;
  if (map < 8) return `E${ep}M${map + 1}`;
  return ep < 4 ? `E${ep + 1}M1` : null;
}
