/**
 * The token list every table shares: network variants plus trusted chain tokens.
 *
 * `/v1/tokens` is polled with the chain head, and the browser's pinned ids are
 * read once per network. `approve` records a manually looked-up token so it
 * survives a reload; `unapprove` removes it again.
 */

import { useCallback, useMemo, useState, type Dispatch, type SetStateAction } from "react";

import { fetchTokens } from "./api";
import { tokensWithRegistry, type Token } from "./lib/assets";
import {
  approveToken,
  approvedReferences,
  readApprovedTokens,
  unapproveToken,
  type ApprovedToken,
} from "./lib/tokenRegistry";
import { useNode } from "./node";
import type { TokenView } from "./types";
import { useResource, type Resource } from "./useResource";

export interface TokenRegistry {
  /** Built-in tokens first, then the chain tokens this client trusts. */
  tokens: Token[];
  /** Every chain-registered token, including ones not approved for display. */
  registered: Resource<{ network: string; tokens: TokenView[] }>;
  approved: ApprovedToken[];
  approve: (view: TokenView) => void;
  unapprove: (view: TokenView) => void;
}

export function useTokenRegistry(): TokenRegistry {
  const { params, network } = useNode();
  const registered = useResource((signal) => fetchTokens(signal), [network], {
    pollMs: 15_000,
  });
  const [approved, setApproved] = useTokenRegistryState(network);
  const tokens = useMemo(
    () =>
      tokensWithRegistry(
        params.data,
        registered.data?.tokens ?? [],
        approvedReferences(approved),
      ),
    [params.data, registered.data, approved],
  );
  const approve = useCallback(
    (view: TokenView) => {
      setApproved(
        approveToken({
          network: view.network,
          ticket: view.ticket,
          tokenId: view.token_id,
          symbol: view.symbol,
          addedAt: Date.now(),
        }),
      );
    },
    [setApproved],
  );
  const unapprove = useCallback(
    (view: TokenView) => {
      setApproved(unapproveToken(view.network, view.token_id));
    },
    [setApproved],
  );
  return { tokens, registered, approved, approve, unapprove };
}

/** Read the pinned ids, resetting the view when the tab changes network. */
function useTokenRegistryState(
  network: string | null,
): [ApprovedToken[], Dispatch<SetStateAction<ApprovedToken[]>>] {
  const [state, setState] = useState<{
    network: string | null;
    tokens: ApprovedToken[];
  }>(() => ({ network, tokens: readApprovedTokens() }));
  const tokens = state.network === network ? state.tokens : readApprovedTokens();
  const setTokens = useCallback<Dispatch<SetStateAction<ApprovedToken[]>>>(
    (value) => {
      setState((current) => {
        const previous = current.network === network ? current.tokens : readApprovedTokens();
        return {
          network,
          tokens: typeof value === "function" ? value(previous) : value,
        };
      });
    },
    [network],
  );
  return [tokens, setTokens];
}
