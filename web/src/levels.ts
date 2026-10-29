export const MAP_NAMES: Record<number, string[]> = {
  1: ["Hangar", "Nuclear Plant", "Toxin Refinery", "Command Control", "Phobos Lab", "Central Processing", "Computer Station", "Phobos Anomaly", "Military Base"],
  2: ["Deimos Anomaly", "Containment Area", "Refinery", "Deimos Lab", "Command Center", "Halls of the Damned", "Spawning Vats", "Tower of Babel", "Fortress of Mystery"],
  3: ["Hell Keep", "Slough of Despair", "Pandemonium", "House of Pain", "Unholy Cathedral", "Mt. Erebus", "Limbo", "Dis", "Warrens"],
  4: ["Hell Beneath", "Perfect Hatred", "Sever the Wicked", "Unruly Evil", "They Will Repent", "Against Thee Wickedly", "And Hell Followed", "Unto the Cruel", "Fear"],
};
export const EPISODE_TITLES: Record<number, string> = {
  1: "Episode 1: Knee-Deep in the Dead",
  2: "Episode 2: The Shores of Hell",
  3: "Episode 3: Inferno",
  4: "Episode 4: Thy Flesh Consumed",
};

export const OUTCOME_LABEL: Record<string, string> = {
  cleared: "Cleared",
  died: "Died",
  timeout: "Timed out",
  aborted: "Stopped",
  abandoned: "Abandoned",
  error: "Error",
};


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
