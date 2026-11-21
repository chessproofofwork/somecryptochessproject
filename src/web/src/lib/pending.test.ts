/**
 * Pending rows: what the wallet remembers between "the node accepted it" and
 * "a block includes it". The rules that matter are the nonce comparison, the
 * dedupe, and that a cleared browser reads as empty rather than as an error.
 */

import { beforeEach, describe, expect, it } from "vitest";

import {
  PENDING_TTL_MS,
  SUBMITTING_TTL_MS,
  addPending,
  parsePending,
  pendingFor,
  prunePending,
  readPending,
  removePending,
  settlePending,
  writePending,
  type PendingTransaction,
} from "./pending";
import { pendingFromTransaction } from "./pendingTx";

const ADDRESS = "sccpt1qalice";
const NETWORK = "testnet";

const FIRST: PendingTransaction = {
  txid: "aa".repeat(32),
  type: "transfer",
  label: "Send ZQWX",
  to: "sccpt1qbob",
  amount: 5_000_000_000,
  asset: null,
  symbol: "ZQWX",
  fee: 1_000,
  nonce: 7,
  submittedAt: 1_700_000_000_000,
  status: "pending",
};

const SECOND: PendingTransaction = { ...FIRST, txid: "bb".repeat(32), nonce: 8 };

function memoryStorage(): Storage {
  const items = new Map<string, string>();
  return {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: (key: string) => items.get(key) ?? null,
    key: (index: number) => [...items.keys()][index] ?? null,
    removeItem: (key: string) => {
      items.delete(key);
    },
    setItem: (key: string, value: string) => {
      items.set(key, value);
    },
  } as Storage;
}

beforeEach(() => {
  Object.defineProperty(globalThis, "localStorage", {
    value: memoryStorage(),
    configurable: true,
  });
});

describe("addPending", () => {
  it("orders by nonce and replaces a resent txid", () => {
    const added = addPending(addPending([], SECOND), FIRST);
    expect(added.map((entry) => entry.nonce)).toEqual([7, 8]);
    const replaced = addPending(added, { ...FIRST, to: "sccpt1qcarol" });
    expect(replaced).toHaveLength(2);
    expect(replaced[0]?.to).toBe("sccpt1qcarol");
  });
});

describe("settlePending", () => {
  it("replaces the draft with the accepted txid and marks it pending", () => {
    const settled = settlePending([FIRST], FIRST.txid, "cc".repeat(32), FIRST.submittedAt);
    expect(settled).toHaveLength(1);
    expect(settled[0]).toMatchObject({
      txid: "cc".repeat(32),
      status: "pending",
      nonce: FIRST.nonce,
    });
  });

  it("does nothing if the draft was already removed", () => {
    expect(settlePending([FIRST], "dd".repeat(32), "ee".repeat(32))).toEqual([FIRST]);
  });
});

describe("pendingFromTransaction", () => {
  it("takes the input leg and counterparty out of a signed swap", () => {
    const row = pendingFromTransaction(
      {
        network: NETWORK,
        type: "swap",
        from: ADDRESS,
        nonce: 8,
        fee: 1_000,
        payload: {
          pool_id: "ab".repeat(32),
          amount_in: 25,
          asset_in: "cd".repeat(32),
          min_amount_out: 3,
          public_key: "ef".repeat(32),
        },
      },
      { label: "Swap C960 for ZQWX", symbol: "C960", status: "submitting" },
    );
    expect(row.txid).toHaveLength(64);
    expect(row).toMatchObject({
      type: "swap",
      label: "Swap C960 for ZQWX",
      to: null,
      amount: 25,
      asset: "cd".repeat(32),
      symbol: "C960",
      fee: 1_000,
      nonce: 8,
      status: "submitting",
    });
  });

  it("records a transfer recipient and a native symbol", () => {
    const row = pendingFromTransaction({
      network: NETWORK,
      type: "transfer",
      from: ADDRESS,
      nonce: 9,
      fee: 2,
      payload: { to: "sccpt1qbob", amount: 50, public_key: "ef".repeat(32) },
    });
    expect(row).toMatchObject({
      type: "transfer",
      label: "Transfer",
      to: "sccpt1qbob",
      amount: 50,
      asset: null,
      symbol: "ZQWX",
    });
  });
});

describe("prunePending", () => {
  it("drops rows the committed nonce has passed, and keeps the next one", () => {
    const now = FIRST.submittedAt + 1000;
    expect(prunePending([FIRST, SECOND], 8, now)).toEqual([SECOND]);
    expect(prunePending([FIRST, SECOND], 7, now)).toEqual([FIRST, SECOND]);
    expect(prunePending([FIRST, SECOND], 9, now)).toEqual([]);
  });

  it("keeps a faucet row that has no account nonce", () => {
    const faucet: PendingTransaction = { ...FIRST, type: "faucet", nonce: null };
    const now = FIRST.submittedAt + 1000;
    expect(prunePending([faucet], 9, now)).toEqual([faucet]);
    expect(prunePending([faucet], 9, FIRST.submittedAt + PENDING_TTL_MS + 1)).toEqual([]);
  });

  it("drops rows older than the TTL, and abandons a stale submission sooner", () => {
    const now = FIRST.submittedAt + PENDING_TTL_MS + 1;
    expect(prunePending([FIRST], 0, now)).toEqual([]);
    const submitting: PendingTransaction = { ...FIRST, status: "submitting" };
    expect(prunePending([submitting], 0, FIRST.submittedAt + SUBMITTING_TTL_MS + 1)).toEqual([]);
  });
});

describe("the stored book", () => {
  it("reads junk as empty", () => {
    expect(parsePending("not json")).toEqual({});
    expect(parsePending("[]")).toEqual({});
    expect(parsePending('{"testnet:sccpt1qx":[{"txid":1}]}')).toEqual({ "testnet:sccpt1qx": [] });
  });

  it("fills action and status defaults for a row written by an older wallet", () => {
    const book = parsePending(
      JSON.stringify({
        "testnet:sccpt1qx": [
          {
            txid: "aa".repeat(32),
            to: "sccpt1qbob",
            amount: 5,
            fee: 1,
            nonce: 3,
            submittedAt: 1_700_000_000_000,
          },
        ],
      }),
    );
    expect(book["testnet:sccpt1qx"]?.[0]).toMatchObject({
      type: "transfer",
      label: "Transfer",
      status: "pending",
    });
  });

  it("round-trips per network and address", () => {
    expect(readPending(ADDRESS, NETWORK)).toEqual([]);
    writePending(ADDRESS, NETWORK, [FIRST, SECOND]);
    expect(readPending(ADDRESS, NETWORK)).toEqual([FIRST, SECOND]);
    expect(readPending(ADDRESS, "devnet")).toEqual([]);
    expect(pendingFor(parsePending(null), ADDRESS, NETWORK)).toEqual([]);
    expect(removePending(ADDRESS, NETWORK, FIRST.txid)).toEqual([SECOND]);
    writePending(ADDRESS, NETWORK, []);
    expect(readPending(ADDRESS, NETWORK)).toEqual([]);
  });
});
