/**
 * Off-chain token descriptions.
 *
 * The chain commits to a token's identity, rules, issuance and clock. It does
 * not carry prose, and it must not: a description is reviewable text that can
 * change without changing consensus. This module is therefore the explorer's
 * small editorial registry, keyed by the identifiers the node returns. A token
 * with no entry gets a neutral fallback rather than an invented story, and an
 * unapproved token always gets a generic warning about its review status.
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

const UNAPPROVED_DESCRIPTION: TokenDescription = {
  body:
    "This is not a preapproved token. Its existence, rules, issuance and activity " +
    "above are recorded on chain, but the display name and ticker are supplied by " +
    "the token's creator and have not been reviewed by this explorer. Verify the " +
    "token id before trading or relying on it.",
};

const DESCRIPTIONS: Readonly<Record<string, TokenDescription>> = {
  "0960": C960_DESCRIPTION,
  "token-0960": C960_DESCRIPTION,
  "3f7df4bfbdf50d69f4d2b08777358d15eab68e62d0c19fd9f2e9c32f27289243": C960_DESCRIPTION,
};

/**
 * The off-chain description for `token`.
 *
 * Approval is checked first: a token that the node has not approved always gets
 * the generic warning, even if its identifiers happen to match an editorial
 * entry. An approved token with no entry returns `undefined`.
 */
export function tokenDescription(token: TokenView): TokenDescription | undefined {
  if (!token.approved) return UNAPPROVED_DESCRIPTION;
  return (
    DESCRIPTIONS[token.token_id] ??
    DESCRIPTIONS[token.name] ??
    (token.ticket === null ? undefined : DESCRIPTIONS[token.ticket])
  );
}
