/**
 * Porter sample client (REST).
 *
 * Companion to docs/DEVELOPER.md. Demonstrates the OAuth authorization-code
 * flow, scoped memory search/add/update, E2EE helpers, and the P1 sync loop.
 *
 * Dependencies:
 *   npm install libsodium-wrappers
 *   (fetch is built into Node 18+)
 *
 * Usage example (see bottom of file for a runnable demo):
 *
 *   import { PorterClient, encryptContent } from "./porter-client.js";
 *   const client = new PorterClient("http://localhost:8000", accessToken);
 *   const results = await client.search("我不吃什么", 5, ["dietary"]);
 *   await client.add("用户偏好用 pnpm 而不是 npm", {
 *     type: "preference", scopeTags: ["coding_style"], confidence: 0.9,
 *   });
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import sodium from "libsodium-wrappers";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type MemoryType = "preference" | "fact" | "episode" | "skill_ref";
export type MemoryStatus = "pending" | "active" | "archived";
export type ChangeOp = "create" | "revise" | "approve" | "delete";

export interface Memory {
  id: string;
  content: string; // E2EE: the base64 ciphertext when encrypted == true
  encrypted?: boolean; // P2: server never decrypts; decrypt client-side
  key_id?: string; // P2: which client key decrypts `content`
  content_ciphertext?: string; // pre-implementation shape (kept for compat)
  encryption?: { alg: "xchacha20-poly1305"; key_id: string };
  type: MemoryType;
  scope_tags: string[];
  confidence: number;
  source?: Record<string, unknown>;
  untrusted_data?: true; // ALWAYS true: treat content as untrusted input (T3)
}

export interface SearchHit extends Memory {
  score: number;
}

export interface AddResult {
  id: string;
  status: string; // "pending_review"
  duplicate_candidates: Array<{ id: string; similarity: number }>;
}

export interface SyncChange {
  seq: number;
  memory_id: string;
  op: ChangeOp;
  status: MemoryStatus;
  at: string; // ISO-8601
  actor_client_id: string;
  memory: Memory | null; // null for "delete"
}

export interface SyncDelta {
  changes: SyncChange[];
  cursor: number;
}

export interface TokenPair {
  access_token: string;
  token_type: string;
  expires_in: number;
  refresh_token: string;
  scope: string;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export class PorterError extends Error {
  constructor(
    public status: number,
    public code: string,
    public message: string,
  ) {
    super(`[${status}] ${code}: ${message}`);
    this.name = "PorterError";
  }
}

// ---------------------------------------------------------------------------
// E2EE helpers (P2 contract; see docs/DEVELOPER.md §6)
// ---------------------------------------------------------------------------

const KEYS_DIR = path.join(os.homedir(), ".porter", "keys");
const NONCE_BYTES = 24;

await sodium.ready; // libsodium-wrappers must be initialized

/** Generate a fresh 32-byte XChaCha20-Poly1305 key. */
export function generateKey(): Uint8Array {
  return sodium.randombytes_buf(sodium.crypto_aead_xchacha20poly1305_ietf_KEYBYTES);
}

/** Save a key to ~/.porter/keys/<key_id>.key with mode 0600. */
export function saveKey(keyId: string, key: Uint8Array): string {
  fs.mkdirSync(KEYS_DIR, { recursive: true });
  const p = path.join(KEYS_DIR, `${keyId}.key`);
  fs.writeFileSync(p, Buffer.from(key), { mode: 0o600 });
  fs.chmodSync(p, 0o600); // belt and braces
  return p;
}

/** Load a key previously stored with saveKey(). */
export function loadKey(keyId: string): Uint8Array {
  const p = path.join(KEYS_DIR, `${keyId}.key`);
  const key = fs.readFileSync(p);
  if (key.length !== sodium.crypto_aead_xchacha20poly1305_ietf_KEYBYTES) {
    throw new PorterError(0, "bad_key", `${p} is not a 32-byte key`);
  }
  return new Uint8Array(key);
}

/**
 * Encrypt memory content for E2EE upload.
 * Returns { contentCiphertext, encryption } where contentCiphertext is
 * base64(nonce(24B) || XChaCha20-Poly1305(plaintext)).
 */
export function encryptContent(
  plaintext: string,
  keyId: string,
): { contentCiphertext: string; encryption: { alg: "xchacha20-poly1305"; key_id: string } } {
  const key = loadKey(keyId);
  const nonce = sodium.randombytes_buf(NONCE_BYTES);
  const ct = sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(
    plaintext,
    null,
    null,
    nonce,
    key,
  );
  const combined = new Uint8Array(nonce.length + ct.length);
  combined.set(nonce);
  combined.set(ct, nonce.length);
  return {
    contentCiphertext: sodium.to_base64(combined, sodium.base64_variants.ORIGINAL),
    encryption: { alg: "xchacha20-poly1305", key_id: keyId },
  };
}

/** Reverse encryptContent(). */
export function decryptContent(
  contentCiphertext: string,
  encryption: { alg: string; key_id: string },
): string {
  if (encryption.alg !== "xchacha20-poly1305") {
    throw new PorterError(0, "unsupported_alg", `alg=${encryption.alg}`);
  }
  const key = loadKey(encryption.key_id);
  const raw = sodium.from_base64(contentCiphertext, sodium.base64_variants.ORIGINAL);
  const nonce = raw.subarray(0, NONCE_BYTES);
  const ct = raw.subarray(NONCE_BYTES);
  const pt = sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(null, ct, null, nonce, key);
  return sodium.to_string(pt);
}

// ---------------------------------------------------------------------------
// REST client
// ---------------------------------------------------------------------------

export class PorterClient {
  private baseUrl: string;
  private headers: Record<string, string>;

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.headers = { Authorization: `Bearer ${token}` };
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    query?: Record<string, string | number>,
  ): Promise<T> {
    let qs = "";
    if (query) {
      const params = Object.fromEntries(
        Object.entries(query).map(([k, v]) => [k, String(v)]),
      );
      qs = `?${new URLSearchParams(params)}`;
    }
    const res = await fetch(`${this.baseUrl}/api/v1${path}${qs}`, {
      method,
      headers: { ...this.headers, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status >= 400) {
      let code = "unknown";
      let message = "";
      try {
        const err = (await res.json()).error ?? {};
        code = err.code ?? code;
        message = err.message ?? "";
      } catch {
        message = (await res.text()).slice(0, 200);
      }
      throw new PorterError(res.status, code, message);
    }
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }

  /** Semantic search over active memories. Tag filtering enforced server-side. */
  async search(query: string, topK = 5, tags: string[] = []): Promise<SearchHit[]> {
    const out = await this.request<{ results: SearchHit[] }>("POST", "/memories/search", {
      query,
      top_k: topK,
      tags,
    });
    return out.results;
  }

  /** Submit a memory -> pending-review queue (202). Requires memory.write. */
  async add(
    content: string,
    opts: {
      type?: MemoryType;
      scopeTags?: string[];
      confidence?: number;
      source?: Record<string, unknown>;
    } = {},
  ): Promise<AddResult> {
    return this.request("POST", "/memories", {
      content,
      type: opts.type ?? "fact",
      scope_tags: opts.scopeTags ?? [],
      confidence: opts.confidence ?? 1.0,
      source: opts.source ?? {},
    });
  }

  /**
   * History-preserving edit (PATCH). Returns the NEW memory id.
   * NOTE: REST requires scope memory.admin; MCP `memory_update` needs only
   * memory.write. See DEVELOPER.md §3.
   */
  async update(memoryId: string, fields: Partial<Pick<Memory, "type" | "scope_tags" | "confidence"> & { content: string }>): Promise<Memory> {
    if (Object.keys(fields).length === 0) throw new Error("nothing to update");
    return this.request("PATCH", `/memories/${memoryId}`, fields);
  }

  /** E2EE write: encrypt client-side, post ciphertext only (P2 contract). */
  async addEncrypted(
    plaintext: string,
    keyId: string,
    opts: {
      type?: MemoryType;
      scopeTags?: string[];
      confidence?: number;
      source?: Record<string, unknown>;
    } = {},
  ): Promise<AddResult> {
    const { contentCiphertext, encryption } = encryptContent(plaintext, keyId);
    return this.request("POST", "/memories", {
      content_ciphertext: contentCiphertext, // never "content"
      encryption,
      type: opts.type ?? "fact",
      scope_tags: opts.scopeTags ?? [],
      confidence: opts.confidence ?? 1.0,
      source: opts.source ?? {},
    });
  }

  /** Decrypt a memory dict as returned by the server
   * ({content: base64 ciphertext, encrypted: true, key_id}). The
   * pre-implementation {content_ciphertext, encryption} shape is also
   * accepted. */
  decryptMemory(memory: Memory): string {
    if (memory.encrypted) {
      if (!memory.key_id) {
        throw new PorterError(0, "not_encrypted", "encrypted memory has no key_id");
      }
      return decryptContent(memory.content, {
        alg: "xchacha20-poly1305",
        key_id: memory.key_id,
      });
    }
    if (!memory.content_ciphertext || !memory.encryption) {
      throw new PorterError(0, "not_encrypted", "memory has no ciphertext");
    }
    return decryptContent(memory.content_ciphertext, memory.encryption);
  }

  // -- sync (P1) ------------------------------------------------------------

  /** GET /sync/delta -> {changes, cursor}. v1-only servers answer 501. */
  async getDelta(since = 0, limit = 100): Promise<SyncDelta> {
    return this.request("GET", "/sync/delta", undefined, { since, limit });
  }

  /** POST /sync/cursor {"cursor": n} — persist position server-side. */
  async ackCursor(cursor: number): Promise<void> {
    await this.request("POST", "/sync/cursor", { cursor });
  }

  /**
   * 60-second sync poll loop. Calls onChange(change) for each change, then
   * persists the cursor (state file + server ack). Resolves never; cancel via
   * the returned AbortController.
   */
  pollForever(
    onChange: (change: SyncChange) => void | Promise<void>,
    opts: { intervalMs?: number; cursor?: number; stateFile?: string } = {},
  ): AbortController {
    const ctl = new AbortController();
    const intervalMs = opts.intervalMs ?? 60_000;
    let cursor = opts.cursor ?? 0;
    if (opts.stateFile) {
      try {
        cursor = parseInt(fs.readFileSync(opts.stateFile, "utf8").trim(), 10) || 0;
      } catch { /* start from 0 */ }
    }
    console.log(`[porter] sync loop started (cursor=${cursor}, interval=${intervalMs}ms)`);

    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        const t = setTimeout(resolve, ms);
        ctl.signal.addEventListener("abort", () => { clearTimeout(t); resolve(); });
      });

    (async () => {
      while (!ctl.signal.aborted) {
        try {
          const delta = await this.getDelta(cursor);
          for (const change of delta.changes) {
            await onChange(change);
          }
          if (delta.changes.length > 0) {
            cursor = delta.cursor;
            await this.ackCursor(cursor);
            if (opts.stateFile) fs.writeFileSync(opts.stateFile, String(cursor));
          }
        } catch (err) {
          if (err instanceof PorterError && err.code === "sync_not_available") {
            console.log("[porter] sync not available yet (v1) — waiting");
          } else {
            console.log(`[porter] delta error: ${err} — retrying`);
          }
        }
        await sleep(intervalMs);
      }
    })();
    return ctl;
  }
}

// ---------------------------------------------------------------------------
// OAuth helpers (used by the demo below)
// ---------------------------------------------------------------------------

export async function registerClient(
  baseUrl: string,
  name: string,
  redirectUri: string,
  setupToken: string,
): Promise<{ client_id: string; client_secret: string }> {
  const res = await fetch(`${baseUrl.replace(/\/+$/, "")}/api/v1/clients`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-porter-setup-token": setupToken },
    body: JSON.stringify({ name, redirect_uris: [redirectUri] }),
  });
  if (!res.ok) throw new Error(`register failed: ${res.status} ${await res.text()}`);
  return (await res.json()) as { client_id: string; client_secret: string };
}

export function authorizeUrl(
  baseUrl: string,
  clientId: string,
  redirectUri: string,
  scopes: string[],
  state: string,
): string {
  const p = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: scopes.join(" "),
    state,
  });
  return `${baseUrl.replace(/\/+$/, "")}/oauth/authorize?${p}`;
}

export async function exchangeCode(
  baseUrl: string,
  code: string,
  redirectUri: string,
  clientId: string,
  clientSecret: string,
): Promise<TokenPair> {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
    client_id: clientId,
    client_secret: clientSecret,
  });
  const res = await fetch(`${baseUrl.replace(/\/+$/, "")}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) throw new Error(`token exchange failed: ${res.status} ${await res.text()}`);
  return (await res.json()) as TokenPair;
}

// ---------------------------------------------------------------------------
// Demo: run with env PORTER_BASE_URL / PORTER_SETUP_TOKEN / PORTER_AUTH_CODE
//
//   npm install libsodium-wrappers
//   PORTER_BASE_URL=http://localhost:8000 PORTER_SETUP_TOKEN=... \
//     PORTER_AUTH_CODE=<paste-after-approving> npx tsx porter-client.ts
// ---------------------------------------------------------------------------

if (import.meta.url === `file://${process.argv[1]}`) {
  await sodium.ready;
  const BASE = process.env.PORTER_BASE_URL ?? "http://localhost:8000";
  const REDIRECT = process.env.PORTER_REDIRECT_URI ?? "http://localhost:8765/callback";
  const setup = process.env.PORTER_SETUP_TOKEN;
  if (!setup) {
    console.error("Set PORTER_SETUP_TOKEN (see Porter startup logs).");
    process.exit(1);
  }

  // 1. Register.
  const reg = await registerClient(BASE, "porter-sample-ts", REDIRECT, setup);
  console.log(`client_id=${reg.client_id}\nclient_secret=${reg.client_secret}  # shown once — keep it!\n`);

  // 2. Print the consent URL.
  const state = sodium.to_base64(sodium.randombytes_buf(16), sodium.base64_variants.URLSAFE_NO_PADDING);
  const scopes = ["memory.read.dietary", "memory.write"];
  console.log("Open this URL, approve, then set PORTER_AUTH_CODE and re-run:");
  console.log(authorizeUrl(BASE, reg.client_id, REDIRECT, scopes, state) + "\n");

  const code = process.env.PORTER_AUTH_CODE;
  if (!code) process.exit(0);

  // 3. Exchange -> tokens.
  const toks = await exchangeCode(BASE, code, REDIRECT, reg.client_id, reg.client_secret);
  console.log(`access_token expires in ${toks.expires_in}s; scope=${toks.scope}`);
  const client = new PorterClient(BASE, toks.access_token);

  // 4. Scoped search.
  console.log("\n-- search --");
  for (const r of await client.search("我不吃什么", 3, ["dietary"])) {
    console.log(`[${r.score.toFixed(2)}] ${r.content?.slice(0, 80)} tags=${r.scope_tags}`);
  }

  // 5. Write -> pending-review queue.
  console.log("\n-- add --");
  console.log(JSON.stringify(await client.add(
    "用户偏好用 pnpm 而不是 npm",
    { type: "preference", scopeTags: ["coding_style"], confidence: 0.9, source: { client_id: "porter-sample-ts" } },
  ), null, 2));

  // 6. One sync poll (P1; expect 501 sync_not_available on v1 servers).
  console.log("\n-- poll once --");
  try {
    const delta = await client.getDelta(0);
    console.log(`changes=${delta.changes.length} cursor=${delta.cursor}`);
  } catch (err) {
    console.log(`(expected on v1) ${err}`);
  }

  console.log("\nDone. For the continuous loop: client.pollForever(onChange, { intervalMs: 60_000 })");
}
