/**
 * The Play page's stage machine, kept pure so the chain rules stay testable.
 *
 * A seat has to move through register -> availability -> reveal -> roster. The
 * availability transaction names an epoch two ahead; the reveal is only valid in
 * the epoch immediately before that target. The browser is the only place that
 * remembers the availability nonce, so the local commitment -- not the current
 * epoch's committed availability -- has to drive the wait/reveal decision.
 */

import type { AccountView, EpochView, NetworkParams, RelayMatchView } from "../types";

/** What the page is waiting for, derived from chain and relay state. */
export type Stage =
  | "fund"
  | "register"
  | "commit"
  | "wait-for-reveal"
  | "reveal"
  | "wait-for-pairing"
  | "join"
  | "play"
  | "settle";

/** The availability nonce this tab still has to reveal. */
export interface StoredCommitment {
  epoch: number;
  nonce: string;
}

export function stageOf(
  account: AccountView,
  params: NetworkParams,
  epoch: EpochView | undefined,
  match: RelayMatchView | null,
  address: string,
  commitment: StoredCommitment | null,
): Stage {
  if (match !== null) {
    if (match.status === "pending") return "join";
    if (match.result !== null) return "settle";
    return "play";
  }
  if (account.registered_until_epoch === null) {
    return account.balance + account.bonded >= params.min_bond + params.min_fee
      ? "register"
      : "fund";
  }
  if (account.bonded < params.min_bond) return "fund";
  if (epoch !== undefined && epoch.roster[address] !== undefined) return "wait-for-pairing";

  // A committed seat is already on the availability path for `target`. Do not
  // look at the current epoch's `availability` map here: it only names seats
  // whose reveals made the epoch's roster, not this seat's future commitment.
  // Checking it first made a normal commit look like "commit" again until the
  // target epoch opened, by which point the reveal window had passed.
  const target = commitment === null ? null : commitment.epoch;
  if (target !== null) {
    if (epoch !== undefined && epoch.epoch === target - 1) return "reveal";
    if (epoch === undefined || epoch.epoch < target - 1) return "wait-for-reveal";
    // The reveal window has closed without a reveal. A fresh commitment for a
    // later epoch is the only useful action left.
    return "commit";
  }
  return "commit";
}
