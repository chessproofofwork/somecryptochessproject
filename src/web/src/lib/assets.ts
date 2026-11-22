/**
 * Tokens, variants and account balances.
 *
 * Standard chess is native ZQWX (`GET /v1/params`). Every other token, C960
 * included, is a chain registration published by `GET /v1/tokens`; it has an
 * immutable asset id and a printable symbol, and its internal variant is
 * `token-<ticket>`. The explorer and wallet keep those two namespaces apart: a
 * transfer of C960 pays its fee in ZQWX, and an account's `assets` map is keyed
 * by the asset id, never a nickname.
 */

import type {
  AccountView,
  NetworkParams,
  NodeStatus,
  TimeControlPeriod,
  TokenView,
  VariantName,
  VariantParams,
} from "../types";

/** The node's fallback clock for networks defined before token time controls. */
const DEFAULT_TIME_CONTROL_INITIAL_MS = 600_000;
const DEFAULT_TIME_CONTROL_INCREMENT_MS = 0;
const DEFAULT_TIME_CONTROL_DELAY_MS = 0;

/** One tradeable token, native ZQWX included. */
export interface Token {
  /** The variant whose proofs mint this token. */
  variant: VariantName;
  /** `null` for native ZQWX; a 64-hex asset id otherwise. */
  asset: string | null;
  symbol: string;
  name: string;
  decimals: number;
  native: boolean;
  subsidy: number;
  halvingInterval: number | null;
  maxSupply: number | null;
  minPlies: number;
  timeControlInitialMs: number;
  timeControlIncrementMs: number;
  timeControlDelayMs: number;
  /** First period length in full moves, or `null` for plain sudden death. */
  timeControlFirstMoves: number | null;
  /** Later periods; empty for plain sudden death. */
  timeControlPeriods: readonly TimeControlPeriod[];
  /** Four-digit registration serial for chain-registered tokens, `null` for built-ins. */
  ticket: string | null;
  /** The ruleset the verifier replays, when it differs from `variant`. */
  ruleset: string | null;
  /** Canonical SAN opening lines, empty when the shared book applies. */
  openings: readonly (readonly string[])[];
  /** Stable network-assigned token id; `null` for native ZQWX. */
  tokenId: string | null;
  /** Whether the node approved the token, or this browser pinned it manually. */
  approved: boolean;
  registeredAtHeight: number | null;
}

const ZQWX: Token = {
  variant: "standard",
  asset: null,
  symbol: "ZQWX",
  name: "Standard chess",
  decimals: 8,
  native: true,
  subsidy: 0,
  halvingInterval: null,
  maxSupply: null,
  minPlies: 0,
  timeControlInitialMs: DEFAULT_TIME_CONTROL_INITIAL_MS,
  timeControlIncrementMs: DEFAULT_TIME_CONTROL_INCREMENT_MS,
  timeControlDelayMs: DEFAULT_TIME_CONTROL_DELAY_MS,
  timeControlFirstMoves: null,
  timeControlPeriods: [],
  ticket: null,
  ruleset: null,
  openings: [],
  tokenId: null,
  approved: true,
  registeredAtHeight: null,
};

/** Project one network variant onto the token the UI spends and displays. */
export function tokenForVariantParams(variant: VariantParams): Token {
  return {
    variant: variant.name,
    asset: variant.asset,
    symbol: variant.symbol,
    name: variant.display_name,
    decimals: variant.decimals,
    native: variant.asset === null,
    subsidy: variant.subsidy,
    halvingInterval: variant.halving_interval,
    maxSupply: variant.max_supply,
    minPlies: variant.min_plies,
    timeControlInitialMs:
      variant.time_control_initial_ms ?? DEFAULT_TIME_CONTROL_INITIAL_MS,
    timeControlIncrementMs:
      variant.time_control_increment_ms ?? DEFAULT_TIME_CONTROL_INCREMENT_MS,
    timeControlDelayMs: variant.time_control_delay_ms ?? DEFAULT_TIME_CONTROL_DELAY_MS,
    timeControlFirstMoves: variant.time_control_first_moves ?? null,
    timeControlPeriods: variant.time_control_periods ?? [],
    ticket: variant.ticket ?? null,
    ruleset: variant.ruleset ?? null,
    openings: variant.openings ?? [],
    tokenId: variant.asset,
    approved: true,
    registeredAtHeight: null,
  };
}

/** Project one chain-registered token onto the token the UI spends and displays. */
export function tokenForTokenView(view: TokenView): Token {
  return {
    variant: view.name,
    asset: view.asset,
    symbol: view.symbol,
    name: view.display_name,
    decimals: view.decimals,
    native: false,
    subsidy: view.subsidy,
    halvingInterval: view.halving_interval,
    maxSupply: view.max_supply,
    minPlies: view.min_plies,
    timeControlInitialMs: view.time_control_initial_ms,
    timeControlIncrementMs: view.time_control_increment_ms,
    timeControlDelayMs: view.time_control_delay_ms,
    timeControlFirstMoves: view.time_control_first_moves,
    timeControlPeriods: view.time_control_periods ?? [],
    ticket: view.ticket,
    ruleset: view.ruleset,
    openings: view.openings ?? [],
    tokenId: view.token_id,
    approved: view.approved,
    registeredAtHeight: view.registered_at_height,
  };
}

/** The network's static tokens, native ZQWX first. */
export function tokensFor(params: NetworkParams | undefined): Token[] {
  if (params === undefined) return [ZQWX];
  const tokens = (params.variants ?? []).map(tokenForVariantParams);
  if (!tokens.some((token) => token.native)) tokens.unshift(ZQWX);
  return tokens.sort((left, right) => Number(right.native) - Number(left.native));
}

/**
 * The static tokens plus the chain-registered tokens this client trusts.
 *
 * A token appears when the node operator approved it (``TokenView.approved``)
 * or when this browser pinned its ticket or token id. C960 is a genesis token,
 * so it arrives here like any other registration. Permissionless registrations
 * therefore never appear in a wallet by accident, while a user who looked one up
 * manually keeps it across reloads.
 */
export function tokensWithRegistry(
  params: NetworkParams | undefined,
  registered: readonly TokenView[],
  locallyApproved: readonly string[] = [],
): Token[] {
  const tokens = tokensFor(params);
  const pinned = new Set(locallyApproved);
  const known = new Set(tokens.map((token) => token.asset).filter((asset) => asset !== null));
  for (const view of registered) {
    if (params !== undefined && view.network !== params.network_id) continue;
    if (known.has(view.asset)) continue;
    const approved =
      view.approved ||
      pinned.has(view.token_id) ||
      (view.ticket !== null && pinned.has(view.ticket));
    if (!approved) continue;
    known.add(view.asset);
    tokens.push({ ...tokenForTokenView(view), approved: true });
  }
  return tokens.sort((left, right) => Number(right.native) - Number(left.native));
}

/** A looked-up token, marked as approved because the user asked for it. */
export function tokenFromLookup(view: TokenView): Token {
  return { ...tokenForTokenView(view), approved: true };
}

/** The short form of an asset id, for a table cell: `abcd12…ef3456`. */
export function shortTokenId(tokenId: string | null): string {
  if (tokenId === null) return "-";
  if (tokenId.length <= 14) return tokenId;
  return `${tokenId.slice(0, 6)}…${tokenId.slice(-6)}`;
}

/**
 * Total issuance of `token` so far, in its base units, excluding fees.
 *
 * Native ZQWX reads the node's subsidy-only counter when it is present; older
 * nodes only report `minted`, which folds transaction fees into the total.
 * Variant totals have never included fees, so they are read directly.
 */
export function tokenMinted(
  status: Pick<NodeStatus, "minted" | "minted_subsidy" | "minted_assets">,
  token: Token,
): number {
  if (token.native) return status.minted_subsidy ?? status.minted;
  return status.minted_assets?.[token.asset ?? ""] ?? 0;
}

/**
 * The subsidy a block can still mint for `token`, in base units.
 *
 * Halvings are counted from height 0, matching the node's issuance schedule,
 * and a capped token never rewards more than its remaining supply. `minted` is
 * the total counted against that cap: for native ZQWX that is the fee-inclusive
 * `NodeStatus.minted`, because the node's own cap check sees fees too.
 */
export function currentBlockReward(token: Token, height: number, minted: number): number {
  const halvings =
    token.halvingInterval === null || token.halvingInterval <= 0
      ? 0
      : Math.floor(Math.max(0, height) / token.halvingInterval);
  const scheduled = Math.floor(token.subsidy / 2 ** halvings);
  if (token.maxSupply === null) return Math.max(0, scheduled);
  const cap = token.maxSupply * 10 ** token.decimals;
  return Math.max(0, Math.min(scheduled, cap - minted));
}

/** The token a variant mints, or `undefined` if this network does not settle it. */
export function tokenForVariant(
  tokens: readonly Token[],
  variant: VariantName,
): Token | undefined {
  return tokens.find((token) => token.variant === variant);
}

/** The token an asset id names; `null` is native ZQWX. */
export function tokenForAsset(
  tokens: readonly Token[],
  asset: string | null | undefined,
): Token | undefined {
  if (asset === null || asset === undefined || asset === "") return tokens.find((token) => token.native);
  return tokens.find((token) => token.asset === asset);
}

/** Spendable balance of `token` for `account`, in that token's base units. */
export function tokenBalance(account: AccountView, token: Token): number {
  return token.native ? account.balance : (account.assets?.[token.asset ?? ""] ?? 0);
}

/** Immature (not yet spendable) balance of `token`, in its base units. */
export function tokenImmature(account: AccountView, token: Token): number {
  return token.native ? account.immature : (account.immature_assets?.[token.asset ?? ""] ?? 0);
}

/** A title-cased label for a variant name, used before params have loaded. */
export function variantLabel(variant: VariantName | null | undefined): string {
  if (variant === null || variant === undefined || variant === "") return "Unknown";
  if (variant === "standard") return "Standard";
  if (variant === "chess960") return "Chess960";
  return variant.replace(/(^|[_-])([a-z])/g, (_, prefix: string, letter: string) =>
    `${prefix === "" ? "" : " "}${letter.toUpperCase()}`,
  );
}

/** A short label suitable for a table cell or pill. */
export function variantShort(variant: VariantName | null | undefined): string {
  if (variant === "standard") return "ZQWX";
  if (variant === "chess960") return "C960";
  return variantLabel(variant);
}
