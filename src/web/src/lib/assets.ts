/**
 * Tokens, variants and account balances.
 *
 * The network decides both the chess variants it settles and the asset each one
 * mints (`GET /v1/params`). Standard chess is native ZQWX; every other variant
 * has an immutable asset id and a printable symbol such as C960. The explorer and
 * wallet keep those two namespaces apart: a transfer of C960 pays its fee in
 * ZQWX, and an account's `assets` map is keyed by the asset id, never a nickname.
 */

import type {
  AccountView,
  NetworkParams,
  NodeStatus,
  VariantName,
  VariantParams,
} from "../types";

/** The node's fallback clock for networks defined before token time controls. */
const DEFAULT_TIME_CONTROL_INITIAL_MS = 600_000;
const DEFAULT_TIME_CONTROL_INCREMENT_MS = 0;

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
  };
}

/** Every token this network supports, native ZQWX first. */
export function tokensFor(params: NetworkParams | undefined): Token[] {
  if (params === undefined) return [ZQWX];
  const tokens = (params.variants ?? []).map(tokenForVariantParams);
  if (!tokens.some((token) => token.native)) tokens.unshift(ZQWX);
  return tokens.sort((left, right) => Number(right.native) - Number(left.native));
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
