import type {
  Decision,
  EngineKind,
  EventIn,
  GameState,
  Mode,
  Outcome,
  PlayerAvailability,
  PlayerType,
} from "./types";

const BASE: string = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, String(detail));
  }
  return (await res.json()) as T;
}

const post = <T>(path: string, body: unknown) =>
  call<T>(path, { method: "POST", body: JSON.stringify(body) });

export interface CreateSessionBody {
  player_type: PlayerType;
  mode: Mode;
  engine: EngineKind;
  level: string;
  seed: number;
}

export interface Api {
  players(): Promise<PlayerAvailability[]>;
  createSession(body: CreateSessionBody): Promise<{ session_id: string; model_version: string }>;
  decide(sessionId: string, state: GameState, screenshot: string | null): Promise<Decision>;
  sendEvents(sessionId: string, events: EventIn[]): Promise<void>;
  endSession(
    sessionId: string,
    body: { outcome: Outcome; duration_s: number; kills: number },
  ): Promise<void>;
  /** Fire-and-forget end for a closing page: keepalive lets it outlive the page. */
  abandonSession(sessionId: string, body: { duration_s: number; kills: number }): void;
}

export const api: Api = {
  players: () => call("/players"),
  createSession: (body) => post("/sessions", body),
  decide: (id, state, screenshot) => post(`/sessions/${id}/decide`, { state, screenshot }),
  sendEvents: async (id, events) => {
    await post(`/sessions/${id}/events`, { events });
  },
  endSession: async (id, body) => {
    await post(`/sessions/${id}/end`, body);
  },
  abandonSession: (id, body) => {
    void fetch(`${BASE}/api/sessions/${id}/end`, {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ outcome: "abandoned", ...body }),
    }).catch(() => {});
  },
};
