/**
 * The Play page must keep a committed seat on the reveal path.
 *
 * A normal commit names an epoch two ahead. The current epoch cannot show that
 * future commitment in its `availability` map, so treating that map as the
 * source of truth made the page offer "Commit availability" again and never
 * enable "Reveal nonce"; the reveal window then closed and the seat never made
 * a roster. These tests pin the local commitment as the authority.
 */

import { describe, expect, it } from "vitest";

import type { AccountView, EpochView, NetworkParams, RelayMatchView } from "../types";
import { stageOf, type StoredCommitment } from "./playStage";

const ADDRESS = "sccpt1qygzrgtz6gqtc8x309cp3sd9kff9efz9t5c97dr5cyqrp8k3u72jgwmq3a4";

const params = {
  min_bond: 1_000_000_000,
  min_fee: 1_000,
} as unknown as NetworkParams;

function account(overrides: Partial<AccountView> = {}): AccountView {
  return {
    registered_until_epoch: 20,
    bonded: 1_000_000_000,
    balance: 10_000_000_000,
    ...overrides,
  } as unknown as AccountView;
}

function epoch(
  number: number,
  roster: EpochView["roster"] = {},
  availability: EpochView["availability"] = {},
): EpochView {
  return { epoch: number, roster, availability } as unknown as EpochView;
}

function match(overrides: Partial<RelayMatchView> = {}): RelayMatchView {
  return {
    status: "active",
    result: null,
    ...overrides,
  } as unknown as RelayMatchView;
}

const committed: StoredCommitment = { epoch: 6, nonce: "01".repeat(32) };

describe("stageOf", () => {
  it("waits for the reveal window instead of asking for another commitment", () => {
    // This is the live failure: epoch 4 has no availability entry for a seat
    // that committed to epoch 6, because the commitment is future state.
    expect(stageOf(account(), params, epoch(4), null, ADDRESS, committed)).toBe(
      "wait-for-reveal",
    );
  });

  it("enables the reveal in the epoch immediately before the target", () => {
    expect(stageOf(account(), params, epoch(5), null, ADDRESS, committed)).toBe("reveal");
  });

  it("lets a missed reveal window be replaced by a later commitment", () => {
    expect(stageOf(account(), params, epoch(6), null, ADDRESS, committed)).toBe("commit");
    expect(stageOf(account(), params, epoch(9), null, ADDRESS, committed)).toBe("commit");
  });

  it("hands the seat to the relay once the target epoch roster names it", () => {
    const roster = { [ADDRESS]: {} } as unknown as EpochView["roster"];
    expect(stageOf(account(), params, epoch(6, roster), null, ADDRESS, committed)).toBe(
      "wait-for-pairing",
    );
  });

  it("asks for a commitment only when the tab has no unrevealed nonce", () => {
    expect(stageOf(account(), params, epoch(4), null, ADDRESS, null)).toBe("commit");
  });

  it("keeps a live match ahead of chain setup state", () => {
    expect(stageOf(account(), params, epoch(4), match(), ADDRESS, committed)).toBe("play");
    expect(
      stageOf(account(), params, epoch(4), match({ status: "pending" }), ADDRESS, committed),
    ).toBe("join");
    expect(
      stageOf(
        account(),
        params,
        epoch(4),
        match({ result: {} as RelayMatchView["result"] }),
        ADDRESS,
        committed,
      ),
    ).toBe("settle");
  });

  it("still funds or registers a seat that has not started", () => {
    expect(
      stageOf(
        account({ registered_until_epoch: null, balance: 0, bonded: 0 }),
        params,
        epoch(4),
        null,
        ADDRESS,
        null,
      ),
    ).toBe("fund");
    expect(
      stageOf(
        account({ registered_until_epoch: null }),
        params,
        epoch(4),
        null,
        ADDRESS,
        null,
      ),
    ).toBe("register");
  });
});
