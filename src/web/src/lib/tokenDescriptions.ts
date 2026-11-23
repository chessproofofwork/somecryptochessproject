/**
 * Off-chain token descriptions.
 *
 * The chain commits to a token's identity, rules, issuance and clock. It does
 * not carry prose, and it must not: a description is reviewable text that can
 * change without changing consensus. This module is therefore the explorer's
 * small editorial registry, keyed by the identifiers the node returns. A token
 * with no entry gets a neutral fallback rather than an invented story.
 */

import type { TokenView } from "../types";

export interface TokenDescription {
  /** Short prose shown on the token page. */
  body: string;
  /** Optional attribution, for a description copied from a project's docs. */
  source?: string;
}

const C960_DESCRIPTION: TokenDescription = {
  body:
    "Chess960 is chess with the back-rank pieces shuffled into one of 960 legal " +
    "starting positions. ZQWX settles it as an ordinary chain-registered token: " +
    "each match starts from a beacon-selected Chess960 position, the winner's " +
    "proof pays C960 to the miner that seals the block, and every fee is still " +
    "paid in native ZQWX. The shipped token was registered at genesis with " +
    "ticket 0960 and no premine, so every C960 in existence has been mined.",
  source: "ZQWX project documentation",
};

const DESCRIPTIONS: Readonly<Record<string, TokenDescription>> = {
  "0960": C960_DESCRIPTION,
  "token-0960": C960_DESCRIPTION,
  "3f7df4bfbdf50d69f4d2b08777358d15eab68e62d0c19fd9f2e9c32f27289243": C960_DESCRIPTION,
};

/** The off-chain description for `token`, or `undefined` when none is published. */
export function tokenDescription(token: TokenView): TokenDescription | undefined {
  return DESCRIPTIONS[token.token_id] ?? DESCRIPTIONS[token.name] ?? (token.ticket === null
    ? undefined
    : DESCRIPTIONS[token.ticket]);
}
