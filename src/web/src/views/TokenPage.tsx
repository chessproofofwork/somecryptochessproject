/**
 * One token: its immutable chain record, off-chain description and the activity
 * the node can derive from the adopted branch.
 *
 * The page makes one request, `GET /v1/tokens/{reference}/overview`, because
 * holders, transactions and miners all have to agree on the same tip. The prose
 * is deliberately local (`lib/tokenDescriptions.ts`): the chain stores no
 * description, so the section labels it as off-chain instead of pretending the
 * node sent it.
 */

import type { ReactNode } from "react";
import { ScrollView, StyleSheet, useWindowDimensions, View } from "react-native";
import { Divider, Text } from "react-native-paper";

import { fetchTokenOverview } from "../api";
import { StatCard } from "../components/BlockTable";
import {
  EmptyState,
  FailureNotice,
  KeyValue,
  Link,
  Loading,
  Mono,
  Section,
  StatusPill,
} from "../components/Chrome";
import { tableStyles } from "../components/tableStyles";
import { variantLabel } from "../lib/assets";
import {
  formatInteger,
  formatMultiPeriodTimeControl,
  formatTokenAmount,
  shortAddress,
  shortHash,
} from "../lib/format";
import { tokenDescription } from "../lib/tokenDescriptions";
import type { TokenTransactionView, TokenView } from "../types";
import { useResource } from "../useResource";

/** The page for one token, addressed by ticket, token id, name or ticker. */
export function TokenPage({ reference }: { reference: string }): ReactNode {
  const overview = useResource(
    (signal) => fetchTokenOverview(reference, { transactionLimit: 50, signal }),
    [reference],
    { pollMs: 15_000 },
  );
  const { width } = useWindowDimensions();
  const wide = width >= 720;

  if (overview.error !== undefined) return <FailureNotice error={overview.error} />;
  if (overview.data === undefined) return <Loading />;

  const { token, holders, holder_count, transactions, transaction_total, miners } = overview.data;
  const description = tokenDescription(token);
  const minted = token.minted;
  const cap =
    token.max_supply === null
      ? "uncapped"
      : formatTokenAmount(token.max_supply * 10 ** token.decimals, token.symbol, token.decimals);

  return (
    <>
      <View style={styles.titleRow}>
        <View style={styles.titleBlock}>
          <Text variant="headlineSmall">{token.display_name}</Text>
          <Mono copy>{token.token_id}</Mono>
        </View>
        <View style={styles.titleActions}>
          <StatusPill label={token.symbol} tone="info" />
          <StatusPill label={variantLabel(token.ruleset ?? token.rules)} tone="neutral" />
          {!token.approved && <StatusPill label="not preapproved" tone="warn" />}
        </View>
      </View>

      <View style={styles.stats}>
        <StatCard
          label="Ticker"
          value={token.symbol}
          hint={token.ticket === null ? "no ticket" : `ticket ${token.ticket}`}
        />
        <StatCard
          label="Current minted"
          value={formatTokenAmount(minted, token.symbol, token.decimals)}
          hint={`initial mint ${formatTokenAmount(token.initial_supply, token.symbol, token.decimals)}`}
        />
        <StatCard
          label="Holders"
          value={formatInteger(holder_count)}
          hint={holder_count > 10 ? "top 10 shown below" : "wallets with a balance"}
        />
        {wide && (
          <StatCard
            label="Top miners"
            value={formatInteger(miners.length)}
            hint="ranked by blocks sealed with this token"
          />
        )}
      </View>

      <Section
        title="Token record"
        subtitle="fixed by the registration transaction; names and tickers may repeat"
      >
        <KeyValue label="Token name">{token.display_name}</KeyValue>
        <KeyValue label="Internal name" mono>
          {token.name}
        </KeyValue>
        <KeyValue label="Ticker" mono>
          {token.symbol}
        </KeyValue>
        <KeyValue label="Ticket" mono>
          {token.ticket ?? "none"}
        </KeyValue>
        <KeyValue label="Token id">
          <Mono copy>{token.token_id}</Mono>
        </KeyValue>
        <KeyValue label="Variant" mono>
          {token.name}
        </KeyValue>
        <KeyValue label="Ruleset" mono>
          {token.ruleset ?? token.rules}
        </KeyValue>
        <KeyValue label="Rules">{token.rules}</KeyValue>
        <KeyValue label="Initial premine / genesis mint" mono>
          {formatTokenAmount(token.initial_supply, token.symbol, token.decimals)}
        </KeyValue>
        <KeyValue label="Total current minted" mono>
          {formatTokenAmount(minted, token.symbol, token.decimals)}
        </KeyValue>
        <KeyValue label="Supply cap" mono>
          {cap}
        </KeyValue>
        <KeyValue label="Block subsidy" mono>
          {formatTokenAmount(token.subsidy, token.symbol, token.decimals)}
          {token.halving_interval === null
            ? " · no halving"
            : ` · halves every ${formatInteger(token.halving_interval)} blocks`}
        </KeyValue>
        <KeyValue label="Time control" mono>
          {formatMultiPeriodTimeControl(
            token.time_control_initial_ms,
            token.time_control_increment_ms,
            token.time_control_delay_ms,
            token.time_control_first_moves,
            token.time_control_periods,
          )}
        </KeyValue>
        <KeyValue label="Start position" mono>
          {token.start_fen ?? `ruleset default (${token.ruleset ?? token.rules})`}
        </KeyValue>
        <KeyValue label="Owner">
          <Link href={`#/accounts/${token.owner}`} mono>
            {token.owner}
          </Link>
        </KeyValue>
        <KeyValue label="Registered at">
          {token.registered_at_height === 0 ? (
            "genesis"
          ) : (
            <Link href={`#/blocks/${token.registered_at_height}`} mono>
              #{formatInteger(token.registered_at_height)}
            </Link>
          )}
        </KeyValue>
      </Section>

      <Section
        title="Description"
        subtitle="maintained by the explorer; this prose is not stored on the blockchain"
        action={<StatusPill label="off-chain" tone="neutral" />}
      >
        {description === undefined ? (
          <EmptyState>No off-chain description is published for this token</EmptyState>
        ) : (
          <>
            <Text variant="bodyMedium" style={styles.prose}>
              {description.body}
            </Text>
            {description.source !== undefined && (
              <Text variant="bodySmall" style={styles.muted}>
                Source: {description.source}
              </Text>
            )}
          </>
        )}
      </Section>

      <Divider />

      <Section
        title="Top holders"
        subtitle={
          holder_count === 0
            ? `no wallet holds ${token.symbol} yet`
            : `${holders.length} of ${formatInteger(holder_count)} holder${
                holder_count === 1 ? "" : "s"
              }, largest first`
        }
      >
        {holders.length === 0 ? (
          <EmptyState>No holder yet</EmptyState>
        ) : (
          <ScrollView
            horizontal
            style={tableStyles.scroll}
            contentContainerStyle={tableStyles.scrollContent}
          >
            <View style={[tableStyles.table, styles.holdersTable]}>
              <View style={tableStyles.header}>
                <Text variant="labelSmall" style={[styles.colRank, tableStyles.muted]}>
                  #
                </Text>
                <Text variant="labelSmall" style={[styles.colWallet, tableStyles.muted]}>
                  WALLET
                </Text>
                <Text variant="labelSmall" style={[styles.colHeld, tableStyles.muted]}>
                  HELD
                </Text>
                <Text variant="labelSmall" style={[styles.colShare, tableStyles.muted]}>
                  SHARE
                </Text>
              </View>
              {holders.map((holder, index) => (
                <View key={holder.address} style={tableStyles.row}>
                  <Text variant="bodySmall" style={[styles.colRank, tableStyles.muted]}>
                    {index + 1}
                  </Text>
                  <View style={styles.colWallet}>
                    <Link href={`#/accounts/${holder.address}`} mono>
                      {shortAddress(holder.address)}
                    </Link>
                  </View>
                  <View style={styles.colHeld}>
                    <Text variant="bodyMedium" style={[tableStyles.numeric, styles.right]}>
                      {formatTokenAmount(holder.total, token.symbol, token.decimals)}
                    </Text>
                    {holder.immature > 0 && (
                      <Text variant="bodySmall" style={[tableStyles.muted, styles.right]}>
                        incl. {formatTokenAmount(holder.immature, token.symbol, token.decimals)} immature
                      </Text>
                    )}
                  </View>
                  <Text
                    variant="bodySmall"
                    style={[styles.colShare, tableStyles.numeric, styles.right]}
                  >
                    {shareOf(holder.total, minted)}
                  </Text>
                </View>
              ))}
            </View>
          </ScrollView>
        )}
      </Section>

      <Section
        title="Transactions"
        subtitle={
          transaction_total === 0
            ? `no confirmed transaction touches ${token.symbol}`
            : `newest ${formatInteger(transactions.length)} of ${formatInteger(
                transaction_total,
              )} confirmed transactions touching ${token.symbol}`
        }
      >
        {transactions.length === 0 ? (
          <EmptyState>No token transaction on this node&apos;s adopted branch</EmptyState>
        ) : (
          <ScrollView
            horizontal
            style={tableStyles.scroll}
            contentContainerStyle={tableStyles.scrollContent}
          >
            <View style={[tableStyles.table, styles.txTable]}>
              <View style={tableStyles.header}>
                <Text variant="labelSmall" style={[styles.colTx, tableStyles.muted]}>
                  TXID
                </Text>
                <Text variant="labelSmall" style={[styles.colType, tableStyles.muted]}>
                  TYPE
                </Text>
                <Text variant="labelSmall" style={[styles.colDetail, tableStyles.muted]}>
                  DETAILS
                </Text>
                <Text variant="labelSmall" style={[styles.colFrom, tableStyles.muted]}>
                  FROM
                </Text>
                <Text variant="labelSmall" style={[styles.colBlock, tableStyles.muted]}>
                  BLOCK
                </Text>
              </View>
              {transactions.map((transaction) => (
                <TransactionRow key={transaction.txid} transaction={transaction} token={token} />
              ))}
            </View>
          </ScrollView>
        )}
      </Section>

      <Section
        title="Top miners"
        subtitle="settled proofs for this token's variant, ranked by blocks sealed"
      >
        {miners.length === 0 ? (
          <EmptyState>No miner has settled a proof for this token</EmptyState>
        ) : (
          <ScrollView
            horizontal
            style={tableStyles.scroll}
            contentContainerStyle={tableStyles.scrollContent}
          >
            <View style={[tableStyles.table, styles.minerTable]}>
              <View style={tableStyles.header}>
                <Text variant="labelSmall" style={[styles.colRank, tableStyles.muted]}>
                  #
                </Text>
                <Text variant="labelSmall" style={[styles.colMiner, tableStyles.muted]}>
                  MINER
                </Text>
                <Text variant="labelSmall" style={[styles.colProofs, tableStyles.muted]}>
                  PROOFS
                </Text>
                <Text variant="labelSmall" style={[styles.colWins, tableStyles.muted]}>
                  WINS
                </Text>
              </View>
              {miners.map((miner, index) => (
                <View key={miner.address} style={tableStyles.row}>
                  <Text variant="bodySmall" style={[styles.colRank, tableStyles.muted]}>
                    {index + 1}
                  </Text>
                  <View style={styles.colMiner}>
                    <Link href={`#/miners/${miner.address}`} mono>
                      {shortAddress(miner.address)}
                    </Link>
                  </View>
                  <Text
                    variant="bodyMedium"
                    style={[styles.colProofs, tableStyles.numeric, styles.right]}
                  >
                    {formatInteger(miner.proofs)}
                  </Text>
                  <Text
                    variant="bodyMedium"
                    style={[styles.colWins, tableStyles.numeric, styles.right]}
                  >
                    {formatInteger(miner.wins)}
                  </Text>
                </View>
              ))}
            </View>
          </ScrollView>
        )}
        <Text variant="bodySmall" style={styles.muted}>
          A proof pays this token to the block&apos;s miner. The same address can be the
          winner, which is why proofs and wins are separate columns.
        </Text>
      </Section>
    </>
  );
}

function TransactionRow({
  transaction,
  token,
}: {
  transaction: TokenTransactionView;
  token: TokenView;
}): ReactNode {
  return (
    <View style={tableStyles.row}>
      <View style={styles.colTx}>
        <Link href={`#/transactions/${transaction.txid}`} mono>
          {shortHash(transaction.txid, 10)}
        </Link>
      </View>
      <Text variant="bodySmall" style={styles.colType}>
        {transaction.type}
      </Text>
      <Text variant="bodySmall" style={[styles.colDetail, tableStyles.muted]}>
        {describeTransaction(transaction, token)}
      </Text>
      <View style={styles.colFrom}>
        <Link href={`#/accounts/${transaction.from}`} mono>
          {shortAddress(transaction.from)}
        </Link>
      </View>
      <View style={styles.colBlock}>
        <Link href={`#/blocks/${transaction.height}`} mono>
          #{formatInteger(transaction.height)}
        </Link>
      </View>
    </View>
  );
}

/** A human-readable one-line description of a token-touching transaction. */
function describeTransaction(transaction: TokenTransactionView, token: TokenView): string {
  const payload = transaction.payload;
  const amount = (key: string): string =>
    formatTokenAmount(Number(payload[key]), token.symbol, token.decimals);
  const text = (key: string): string =>
    typeof payload[key] === "string" ? String(payload[key]) : "";
  switch (transaction.type) {
    case "register_token":
      return "registered the token";
    case "transfer":
      return `${amount("amount")} → ${shortAddress(text("to"))}`;
    case "create_pool":
      return `created a pool with ${assetLabel(payload.asset_a)} / ${assetLabel(payload.asset_b)}`;
    case "add_liquidity":
      return `added liquidity to pool ${shortHash(text("pool_id"), 8)}`;
    case "remove_liquidity":
      return `redeemed ${formatInteger(Number(payload.shares))} pool shares from ${shortHash(
        text("pool_id"),
        8,
      )}`;
    case "swap":
      return text("asset_in") === token.asset
        ? `sold ${amount("amount_in")} through pool ${shortHash(text("pool_id"), 8)}`
        : `swapped through pool ${shortHash(text("pool_id"), 8)}`;
    case "htlc_lock":
      return `locked ${amount("amount")} until height ${formatInteger(
        Number(payload.timelock_height),
      )}`;
    case "htlc_claim":
      return `claimed lock ${shortHash(text("htlc_id"), 8)}`;
    case "htlc_refund":
      return `refunded lock ${shortHash(text("htlc_id"), 8)}`;
    default:
      return transaction.type.replace(/_/g, " ");
  }
}

function assetLabel(value: unknown): string {
  return typeof value === "string" && value !== "" ? shortHash(value, 6) : "ZQWX";
}

/** A holder's share of current minted supply, as a percentage. */
function shareOf(amount: number, minted: number): string {
  if (minted <= 0) return "-";
  const percent = (amount / minted) * 100;
  if (percent > 0 && percent < 0.01) return "<0.01%";
  return `${percent.toFixed(percent >= 10 ? 1 : 2)}%`;
}

const styles = StyleSheet.create({
  titleRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  },
  titleBlock: { gap: 4, flexShrink: 1 },
  titleActions: { flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap" },
  stats: { flexDirection: "row", flexWrap: "wrap", gap: 12 },
  prose: { maxWidth: 760, lineHeight: 22 },
  muted: { opacity: 0.66 },
  right: { textAlign: "right" },
  holdersTable: { minWidth: 620 },
  txTable: { minWidth: 900 },
  minerTable: { minWidth: 520 },
  colRank: { width: 36 },
  colWallet: { flexGrow: 1, flexShrink: 1, minWidth: 150 },
  colHeld: { width: 210 },
  colShare: { width: 82 },
  colTx: { width: 150 },
  colType: { width: 128 },
  colDetail: { flexGrow: 1, flexShrink: 1, minWidth: 220 },
  colFrom: { width: 150 },
  colBlock: { width: 78 },
  colMiner: { flexGrow: 1, flexShrink: 1, minWidth: 180 },
  colProofs: { width: 82 },
  colWins: { width: 72 },
});

