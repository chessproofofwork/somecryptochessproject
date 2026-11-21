/**
 * Resolve whatever the explorer's search box was given.
 *
 * A height is unambiguous, and a bech32 address has its own shape. A 32-byte
 * hash is not: block hashes and txids have the same shape, so the only authority
 * is the node. Ask for the block first, then the transaction, and let a transport
 * failure surface instead of turning it into "not found".
 */

import { fetchAccount, fetchBlock, fetchTransaction, NotFoundError } from "../api";
import type { Route } from "../router";
import { ADDRESS_HRP, decodeAddress } from "./bech32";

/** A canonical 32-byte hex reference; the node stores these lowercase. */
const HEX_32_BYTES = /^[0-9a-fA-F]{64}$/;
const HEIGHT = /^\d+$/;
const KNOWN_ADDRESS_HRPS: ReadonlySet<string> = new Set(Object.values(ADDRESS_HRP));

/**
 * The route the search box should open, or `null` when no height, block,
 * transaction or account matches. The caller decides how to present a miss;
 * this helper does not invent a route that is known to fail.
 *
 * `addressHrp` is the node's `address_hrp` from `/v1/params`. When it is known,
 * an address for another network is a miss rather than a page that the node will
 * reject with a 400. Before the parameters land, a well-formed address for any
 * network this build knows is accepted.
 */
export async function resolveSearchTarget(
  raw: string,
  signal?: AbortSignal,
  addressHrp?: string,
): Promise<Route | null> {
  const query = raw.trim();
  if (query === "") return null;

  if (HEIGHT.test(query)) return { name: "block", reference: query };

  // Decode locally so a typo never becomes a request the node answers with a
  // 400. The chain's account keys are lowercase, so normalize before using the
  // address as a route parameter.
  const decoded = decodeAddress(query);
  if (decoded !== null) {
    const expectedHrp =
      addressHrp ?? (KNOWN_ADDRESS_HRPS.has(decoded.hrp) ? decoded.hrp : undefined);
    if (expectedHrp === undefined || decoded.hrp !== expectedHrp) return null;
    const address = query.toLowerCase();
    await fetchAccount(address, signal);
    return { name: "account", address };
  }

  // Hex references are case-insensitive to a person pasting one, even though
  // the chain spells every digest in lowercase.
  const reference = HEX_32_BYTES.test(query) ? query.toLowerCase() : query;

  try {
    await fetchBlock(reference, signal);
    return { name: "block", reference };
  } catch (error) {
    if (!(error instanceof NotFoundError)) throw error;
  }

  try {
    const record = await fetchTransaction(reference, signal);
    return { name: "transaction", txid: record.txid };
  } catch (error) {
    if (error instanceof NotFoundError) return null;
    throw error;
  }
}
