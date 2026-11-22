/**
 * The browser's approved-token registry is a display preference, not evidence.
 *
 * These tests pin the rules that keep it harmless: storage junk reads as empty,
 * one network+token id has one entry, a ticket is only an alias, and an
 * approval survives a reload while an unapproval removes it.
 */

import { beforeEach, describe, expect, it } from "vitest";

import {
  approveToken,
  approvedReferences,
  forgetApprovedToken,
  parseApprovedTokens,
  readApprovedTokens,
  unapproveToken,
  upsertApprovedToken,
  type ApprovedToken,
} from "./tokenRegistry";

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

const TOKEN_ID = "a".repeat(64);
const OTHER_ID = "b".repeat(64);

const T0042: ApprovedToken = {
  network: "devnet",
  ticket: "0042",
  tokenId: TOKEN_ID,
  symbol: "T0042",
  addedAt: 1,
};

describe("parseApprovedTokens", () => {
  it("drops junk entries and reads malformed storage as empty", () => {
    expect(parseApprovedTokens(null)).toStrictEqual([]);
    expect(parseApprovedTokens("{")).toStrictEqual([]);
    expect(parseApprovedTokens(JSON.stringify({ tokens: [T0042] }))).toStrictEqual([]);
    expect(
      parseApprovedTokens(
        JSON.stringify([T0042, { ...T0042, tokenId: 7 }, { ticket: "0043" }]),
      ),
    ).toStrictEqual([T0042]);
  });

  it("keeps one entry per network and token id, sorted for a stable table", () => {
    const later = { ...T0042, symbol: "T0042b", addedAt: 2 };
    const otherNetwork = { ...T0042, network: "testnet", tokenId: OTHER_ID, symbol: "T0001" };
    const parsed = parseApprovedTokens(JSON.stringify([later, otherNetwork, T0042]));
    expect(parsed).toStrictEqual([T0042, otherNetwork]);
  });
});

describe("registry edits", () => {
  it("upserts by network and token id and forgets by the same key", () => {
    const added = upsertApprovedToken([], T0042);
    expect(added).toStrictEqual([T0042]);
    const replaced = upsertApprovedToken(added, { ...T0042, symbol: "T0042x", addedAt: 2 });
    expect(replaced).toStrictEqual([{ ...T0042, symbol: "T0042x", addedAt: 2 }]);
    expect(forgetApprovedToken(replaced, "devnet", TOKEN_ID)).toStrictEqual([]);
    expect(forgetApprovedToken(replaced, "testnet", TOKEN_ID)).toStrictEqual(replaced);
  });

  it("offers both the token id and the ticket as lookup aliases", () => {
    expect(approvedReferences([T0042, { ...T0042, ticket: null, tokenId: OTHER_ID }])).toStrictEqual(
      [TOKEN_ID, "0042", OTHER_ID],
    );
  });
});

describe("localStorage", () => {
  it("round-trips approvals and unapprovals", () => {
    expect(readApprovedTokens()).toStrictEqual([]);
    expect(approveToken(T0042)).toStrictEqual([T0042]);
    expect(readApprovedTokens()).toStrictEqual([T0042]);
    expect(unapproveToken("devnet", TOKEN_ID)).toStrictEqual([]);
    expect(readApprovedTokens()).toStrictEqual([]);
  });

  it("does not fail when localStorage is unavailable", () => {
    Object.defineProperty(globalThis, "localStorage", {
      get() {
        throw new Error("private mode");
      },
      configurable: true,
    });
    expect(readApprovedTokens()).toStrictEqual([]);
    expect(approveToken(T0042)).toStrictEqual([T0042]);
  });
});
