/**
 * Turn a signed ledger transaction into the row the wallet's History tab shows.
 *
 * This lives apart from `pending.ts` so the shared wallet session can keep the
 * pending book without importing `lib/tx` (and, through it, the post-quantum key
 * stack) into the explorer's main bundle. The session loads this module on
 * demand, only when something is actually being signed.
 */

import { pendingLabel, type PendingOptions, type PendingTransaction } from "./pending";
import { txId, type Transaction } from "./tx";

function payloadText(tx: Transaction, field: string): string | null {
  const value = tx.payload[field];
  return typeof value === "string" && value !== "" ? value : null;
}

function payloadInt(tx: Transaction, field: string): number | null {
  const value = tx.payload[field];
  return typeof value === "number" && Number.isSafeInteger(value) ? value : null;
}

/**
 * Describe a signed transaction before it reaches the node.
 *
 * The sender already knows the type, nonce, fee and payload; this only turns them
 * into the one row History can render without having to re-implement each
 * transaction's payload shape. A transfer names its recipient and amount, a swap
 * names its input leg, and an HTLC lock names its beneficiary.
 */
export function pendingFromTransaction(
  tx: Transaction,
  options: PendingOptions = {},
): PendingTransaction {
  const asset = payloadText(tx, "asset") ?? payloadText(tx, "asset_in") ?? null;
  const amount = payloadInt(tx, "amount") ?? payloadInt(tx, "amount_in") ?? null;
  return {
    txid: txId(tx),
    type: tx.type,
    label: options.label ?? pendingLabel(tx.type),
    to: payloadText(tx, "to") ?? payloadText(tx, "beneficiary"),
    amount,
    asset,
    symbol: options.symbol ?? (asset === null ? "ZQWX" : undefined),
    fee: tx.fee,
    nonce: tx.nonce,
    submittedAt: options.submittedAt ?? Date.now(),
    status: options.status ?? "pending",
  };
}
