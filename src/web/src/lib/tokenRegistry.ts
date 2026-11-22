/**
 * The browser's approved-token registry.
 *
 * Chain registration is permissionless, so a token existing is not a reason to
 * put it in every wallet. A token reaches the tables when the node operator
 * approved it (`TokenView.approved`) or when the user looked it up by ticket,
 * token id, name or ticker and chose to keep it. This store is the second half
 * of that rule: one JSON array of the ids this browser pinned, beside the
 * contacts and other non-secret preferences.
 *
 * Nothing here is evidence. The chain is still the authority on the token's
 * parameters; an entry only says "show this one even if the node did not
 * approve it". Malformed storage reads as an empty registry.
 */

export interface ApprovedToken {
  network: string;
  /** Registration serial, or `null` for a token registered before tickets. */
  ticket: string | null;
  /** The network-assigned token id (the asset id). */
  tokenId: string;
  symbol: string;
  addedAt: number;
}

const APPROVED_TOKENS_KEY = "sccp.wallet.approvedTokens";

function isApprovedToken(value: unknown): value is ApprovedToken {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.network === "string" &&
    (candidate.ticket === null || typeof candidate.ticket === "string") &&
    typeof candidate.tokenId === "string" &&
    typeof candidate.symbol === "string" &&
    typeof candidate.addedAt === "number"
  );
}

function unique(tokens: readonly ApprovedToken[]): ApprovedToken[] {
  const byKey = new Map<string, ApprovedToken>();
  for (const token of tokens) {
    byKey.set(`${token.network}:${token.tokenId}`, token);
  }
  return [...byKey.values()].sort(
    (left, right) =>
      left.network.localeCompare(right.network) ||
      left.symbol.localeCompare(right.symbol) ||
      left.tokenId.localeCompare(right.tokenId),
  );
}

/** Parse stored JSON; junk entries are dropped one by one. */
export function parseApprovedTokens(text: string | null): ApprovedToken[] {
  if (text === null) return [];
  try {
    const parsed: unknown = JSON.parse(text);
    if (!Array.isArray(parsed)) return [];
    return unique(parsed.filter(isApprovedToken));
  } catch {
    return [];
  }
}

/** Add or replace one approval and return the new list. */
export function upsertApprovedToken(
  tokens: readonly ApprovedToken[],
  token: ApprovedToken,
): ApprovedToken[] {
  return unique([
    ...tokens.filter(
      (entry) => !(entry.network === token.network && entry.tokenId === token.tokenId),
    ),
    token,
  ]);
}

/** Remove one approval and return the new list. */
export function forgetApprovedToken(
  tokens: readonly ApprovedToken[],
  network: string,
  tokenId: string,
): ApprovedToken[] {
  return tokens.filter(
    (entry) => !(entry.network === network && entry.tokenId === tokenId),
  );
}

/** The ticket or token id references the token tables accept as approved. */
export function approvedReferences(tokens: readonly ApprovedToken[]): string[] {
  const references: string[] = [];
  for (const token of tokens) {
    references.push(token.tokenId);
    if (token.ticket !== null) references.push(token.ticket);
  }
  return references;
}

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    // A private mode can throw on the getter itself.
    return null;
  }
}

function write(tokens: readonly ApprovedToken[]): void {
  const store = storage();
  if (store === null) return;
  try {
    store.setItem(APPROVED_TOKENS_KEY, JSON.stringify(tokens));
  } catch {
    // A registry that cannot be stored is not worth an error dialog.
  }
}

/** Every token this browser pinned, across networks. */
export function readApprovedTokens(): ApprovedToken[] {
  const store = storage();
  if (store === null) return [];
  try {
    return parseApprovedTokens(store.getItem(APPROVED_TOKENS_KEY));
  } catch {
    return [];
  }
}

/** Pin one token and return the new registry. */
export function approveToken(token: ApprovedToken): ApprovedToken[] {
  const tokens = upsertApprovedToken(readApprovedTokens(), token);
  write(tokens);
  return tokens;
}

/** Unpin one token and return the new registry. */
export function unapproveToken(network: string, tokenId: string): ApprovedToken[] {
  const tokens = forgetApprovedToken(readApprovedTokens(), network, tokenId);
  write(tokens);
  return tokens;
}
