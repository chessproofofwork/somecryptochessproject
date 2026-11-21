/**
 * Transfers and other ledger actions this browser signed, until a block includes them.
 *
 * `GET /v1/accounts/{address}` reports *committed* state, so there is no endpoint
 * that answers "is my nonce still waiting?". The wallet knows what it submitted
 * and which nonce each transaction consumed, so that is what it keeps: written
 * before `/v1/tx` is called, updated when the node accepts it, and dropped when
 * the committed nonce passes it or the node reports a block.
 *
 * The record is local and the History tab says so. Clearing the browser forgets
 * the pending row, which is not the same as the chain forgetting the transaction;
 * the scan finds it again once a block carries it.
 */

/** Whether the node has answered for a row yet. */
export type PendingStatus = "submitting" | "pending";

/** A ledger action this wallet signed, and the nonce it consumed. */
export interface PendingTransaction {
  txid: string;
  /** Wire transaction type, or `faucet` for a grant the node built itself. */
  type: string;
  /** Human-readable action, e.g. `Swap ZQWX for C960`. */
  label: string;
  /** Counterparty address where one exists; `null` for a pool or HTLC action. */
  to: string | null;
  /** Base units moved, or `null` for an action that moves no coins. */
  amount: number | null;
  /** Asset id for a C960-style action; `null`/absent means native ZQWX. */
  asset?: string | null;
  /** Printable symbol captured when signed, so history still reads without params. */
  symbol?: string;
  fee: number;
  /** The account nonce this transaction spends; `null` for a faucet grant. */
  nonce: number | null;
  submittedAt: number;
  /** `submitting` until the node answers, `pending` once it is in the mempool. */
  status: PendingStatus;
}

/** Options for describing a transaction before it is submitted. */
export interface PendingOptions {
  /** A title such as `Swap ZQWX for C960`; defaults to the transaction type. */
  label?: string;
  /** A printable token symbol, when the caller already knows it. */
  symbol?: string;
  status?: PendingStatus;
  submittedAt?: number;
}

const PENDING_KEY = "sccp.wallet.pending";

/**
 * A row kept this long without being mined is treated as dropped.
 *
 * The node does not promise a mempool lifetime, so the wallet keeps the row for a
 * week -- long enough that a quiet testnet is not mistaken for a lost transfer,
 * short enough that the book cannot grow forever.
 */
export const PENDING_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/**
 * A `submitting` row kept this long means the request was abandoned.
 *
 * The submission call either answers or fails in seconds. A row still saying
 * "sending" after two minutes is a browser that closed mid-request, not something
 * the node is about to accept.
 */
export const SUBMITTING_TTL_MS = 2 * 60 * 1000;

/** `network:address` -> that account's pending rows. */
type PendingBook = Record<string, PendingTransaction[]>;

const bookKey = (address: string, network: string): string => `${network}:${address}`;

const DEFAULT_LABELS: Record<string, string> = {
  transfer: "Transfer",
  register: "Miner registration",
  availability: "Availability commitment",
  reveal: "Roster reveal",
  create_pool: "Create pool",
  add_liquidity: "Add liquidity",
  remove_liquidity: "Remove liquidity",
  swap: "Swap",
  htlc_lock: "Bridge lock",
  htlc_claim: "Bridge claim",
  htlc_refund: "Bridge refund",
  faucet: "Faucet grant",
};

/** A title for a transaction type when the caller did not supply one. */
export function pendingLabel(type: string): string {
  return DEFAULT_LABELS[type] ?? type.replaceAll("_", " ");
}

function normalize(value: unknown): PendingTransaction | null {
  if (typeof value !== "object" || value === null) return null;
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.txid !== "string" ||
    typeof candidate.fee !== "number" ||
    typeof candidate.submittedAt !== "number"
  ) {
    return null;
  }
  const type =
    typeof candidate.type === "string" && candidate.type !== "" ? candidate.type : "transfer";
  return {
    txid: candidate.txid,
    type,
    label:
      typeof candidate.label === "string" && candidate.label !== ""
        ? candidate.label
        : pendingLabel(type),
    to: typeof candidate.to === "string" ? candidate.to : null,
    amount:
      typeof candidate.amount === "number" && Number.isFinite(candidate.amount)
        ? candidate.amount
        : null,
    asset: typeof candidate.asset === "string" ? candidate.asset : null,
    symbol: typeof candidate.symbol === "string" ? candidate.symbol : undefined,
    fee: candidate.fee,
    nonce:
      typeof candidate.nonce === "number" && Number.isSafeInteger(candidate.nonce)
        ? candidate.nonce
        : null,
    submittedAt: candidate.submittedAt,
    status: candidate.status === "submitting" ? "submitting" : "pending",
  };
}

/** Parse the stored book; a malformed entry is dropped, not thrown. */
export function parsePending(text: string | null): PendingBook {
  if (text === null) return {};
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const book: PendingBook = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!Array.isArray(value)) continue;
      book[key] = value
        .map(normalize)
        .filter((entry): entry is PendingTransaction => entry !== null)
        .sort(byNonce);
    }
    return book;
  } catch {
    return {};
  }
}

function byNonce(left: PendingTransaction, right: PendingTransaction): number {
  if (left.nonce === null) return right.nonce === null ? 0 : 1;
  if (right.nonce === null) return -1;
  return left.nonce - right.nonce;
}

/** The pending rows stored for one account. */
export function pendingFor(
  book: PendingBook,
  address: string,
  network: string,
): PendingTransaction[] {
  return book[bookKey(address, network)] ?? [];
}

/** Add `tx`, replacing an earlier row with the same txid, ordered by nonce. */
export function addPending(
  entries: readonly PendingTransaction[],
  tx: PendingTransaction,
): PendingTransaction[] {
  return [...entries.filter((entry) => entry.txid !== tx.txid), tx].sort(byNonce);
}

/** Replace a draft row with the txid and status the node answered with. */
export function settlePending(
  entries: readonly PendingTransaction[],
  draftTxid: string,
  acceptedTxid: string,
  submittedAt: number = Date.now(),
): PendingTransaction[] {
  const draft = entries.find((entry) => entry.txid === draftTxid);
  if (draft === undefined) return [...entries];
  return addPending(
    entries.filter((entry) => entry.txid !== draftTxid && entry.txid !== acceptedTxid),
    { ...draft, txid: acceptedTxid, status: "pending", submittedAt },
  );
}

/**
 * Drop rows the chain has passed and rows too old to mean anything.
 *
 * `committedNonce` is the account's next unused nonce: a transaction with nonce
 * `n` is included once the committed nonce is greater than `n`, so a row is kept
 * while `n >= committedNonce`. A faucet grant has no sender nonce and is retired
 * by `/v1/transactions/{txid}` instead, so it is kept until the TTL.
 */
export function prunePending(
  entries: readonly PendingTransaction[],
  committedNonce: number,
  now: number = Date.now(),
): PendingTransaction[] {
  return entries
    .filter(
      (entry) =>
        (entry.nonce === null || entry.nonce >= committedNonce) &&
        now - entry.submittedAt <
          (entry.status === "submitting" ? SUBMITTING_TTL_MS : PENDING_TTL_MS),
    )
    .sort(byNonce);
}

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

function readBook(): PendingBook {
  const store = storage();
  if (store === null) return {};
  try {
    return parsePending(store.getItem(PENDING_KEY));
  } catch {
    return {};
  }
}

function writeBook(book: PendingBook): void {
  const store = storage();
  if (store === null) return;
  try {
    store.setItem(PENDING_KEY, JSON.stringify(book));
  } catch {
    // Losing the reminder is not losing the transaction.
  }
}

/** Read one account's pending rows. */
export function readPending(address: string, network: string): PendingTransaction[] {
  return pendingFor(readBook(), address, network);
}

/** Replace one account's pending rows; an empty list removes the key. */
export function writePending(
  address: string,
  network: string,
  entries: readonly PendingTransaction[],
): void {
  const book = readBook();
  const key = bookKey(address, network);
  if (entries.length === 0) delete book[key];
  else book[key] = [...entries];
  writeBook(book);
}

/** Forget one pending row by txid. */
export function removePending(
  address: string,
  network: string,
  txid: string,
): PendingTransaction[] {
  const entries = readPending(address, network).filter((entry) => entry.txid !== txid);
  writePending(address, network, entries);
  return entries;
}
