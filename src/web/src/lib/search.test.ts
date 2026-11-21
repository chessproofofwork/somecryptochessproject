/**
 * The Blocks search box.
 *
 * A block hash and a txid are both 32-byte hex, so only the node can tell them
 * apart. These tests pin the order of the two lookups: block first, transaction
 * second, and never hide a transport failure as "not found". An address is the
 * one reference the page can recognise before asking the node.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    fetchAccount: vi.fn(),
    fetchBlock: vi.fn(),
    fetchTransaction: vi.fn(),
  };
});

import { ApiError, fetchAccount, fetchBlock, fetchTransaction, NotFoundError } from "../api";
import type { AccountView, BlockView, TransactionRecord } from "../types";
import { resolveSearchTarget } from "./search";
import vectors from "./vectors.json";

const BLOCK_HASH = "cd".repeat(32);
const TXID = "ab".repeat(32);
const TESTNET_ADDRESS = vectors.keys.address_testnet;
const MAINNET_ADDRESS = vectors.keys.address_mainnet;

const RECORD: TransactionRecord = {
  txid: TXID,
  network: "devnet",
  type: "transfer",
  height: 7,
  in_mempool: false,
  block_hash: BLOCK_HASH,
};

beforeEach(() => {
  vi.mocked(fetchAccount).mockReset();
  vi.mocked(fetchBlock).mockReset();
  vi.mocked(fetchTransaction).mockReset();
});

describe("resolveSearchTarget", () => {
  it("takes a height straight to the block page", async () => {
    await expect(resolveSearchTarget(" 12 ")).resolves.toEqual({
      name: "block",
      reference: "12",
    });
    expect(fetchAccount).not.toHaveBeenCalled();
    expect(fetchBlock).not.toHaveBeenCalled();
    expect(fetchTransaction).not.toHaveBeenCalled();
  });

  it("opens the account page for a well-formed address", async () => {
    vi.mocked(fetchAccount).mockResolvedValue({} as AccountView);
    await expect(
      resolveSearchTarget(` ${TESTNET_ADDRESS} `, undefined, "sccpt"),
    ).resolves.toEqual({ name: "account", address: TESTNET_ADDRESS });
    expect(fetchAccount).toHaveBeenCalledWith(TESTNET_ADDRESS, undefined);
    expect(fetchBlock).not.toHaveBeenCalled();
    expect(fetchTransaction).not.toHaveBeenCalled();
  });

  it("normalises a pasted uppercase address to the chain's lowercase spelling", async () => {
    vi.mocked(fetchAccount).mockResolvedValue({} as AccountView);
    await expect(
      resolveSearchTarget(TESTNET_ADDRESS.toUpperCase(), undefined, "sccpt"),
    ).resolves.toEqual({ name: "account", address: TESTNET_ADDRESS });
    expect(fetchAccount).toHaveBeenCalledWith(TESTNET_ADDRESS, undefined);
  });

  it("treats an address for another network as a miss", async () => {
    await expect(resolveSearchTarget(MAINNET_ADDRESS, undefined, "sccpt")).resolves.toBeNull();
    expect(fetchAccount).not.toHaveBeenCalled();
    expect(fetchBlock).not.toHaveBeenCalled();
    expect(fetchTransaction).not.toHaveBeenCalled();
  });

  it("does not send a malformed address to the account endpoint", async () => {
    const broken = `${TESTNET_ADDRESS.slice(0, -1)}${TESTNET_ADDRESS.endsWith("q") ? "p" : "q"}`;
    vi.mocked(fetchBlock).mockRejectedValue(new NotFoundError("unknown block"));
    vi.mocked(fetchTransaction).mockRejectedValue(new NotFoundError("unknown transaction"));
    await expect(resolveSearchTarget(broken, undefined, "sccpt")).resolves.toBeNull();
    expect(fetchAccount).not.toHaveBeenCalled();
  });

  it("does not turn an account lookup outage into a miss", async () => {
    const offline = new ApiError("cannot reach the node", 0);
    vi.mocked(fetchAccount).mockRejectedValue(offline);
    await expect(resolveSearchTarget(TESTNET_ADDRESS, undefined, "sccpt")).rejects.toBe(offline);
    expect(fetchBlock).not.toHaveBeenCalled();
  });

  it("resolves a block hash before trying the transaction route", async () => {
    vi.mocked(fetchBlock).mockResolvedValue({} as BlockView);
    await expect(resolveSearchTarget(BLOCK_HASH)).resolves.toEqual({
      name: "block",
      reference: BLOCK_HASH,
    });
    expect(fetchTransaction).not.toHaveBeenCalled();
  });

  it("falls back to the transaction route when no block has that hash", async () => {
    vi.mocked(fetchBlock).mockRejectedValue(new NotFoundError("unknown block"));
    vi.mocked(fetchTransaction).mockResolvedValue(RECORD);
    await expect(resolveSearchTarget(TXID)).resolves.toEqual({
      name: "transaction",
      txid: TXID,
    });
    expect(fetchBlock).toHaveBeenCalledWith(TXID, undefined);
    expect(fetchTransaction).toHaveBeenCalledWith(TXID, undefined);
  });

  it("normalises a pasted hex id to the chain's lowercase spelling", async () => {
    const upper = TXID.toUpperCase();
    vi.mocked(fetchBlock).mockRejectedValue(new NotFoundError("unknown block"));
    vi.mocked(fetchTransaction).mockResolvedValue(RECORD);
    await resolveSearchTarget(upper);
    expect(fetchBlock).toHaveBeenCalledWith(TXID, undefined);
    expect(fetchTransaction).toHaveBeenCalledWith(TXID, undefined);
  });

  it("reports a miss when neither route knows the reference", async () => {
    vi.mocked(fetchBlock).mockRejectedValue(new NotFoundError("unknown block"));
    vi.mocked(fetchTransaction).mockRejectedValue(new NotFoundError("unknown transaction"));
    await expect(resolveSearchTarget("not-a-reference")).resolves.toBeNull();
  });

  it("does not turn a node outage into a miss", async () => {
    const offline = new ApiError("cannot reach the node", 0);
    vi.mocked(fetchBlock).mockRejectedValue(offline);
    await expect(resolveSearchTarget(TXID)).rejects.toBe(offline);
    expect(fetchTransaction).not.toHaveBeenCalled();
  });
});
