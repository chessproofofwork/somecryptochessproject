/**
 * The token summary: one row per chess variant and the asset it mints.
 *
 * A chain-registered token has a four-digit ticket (a unique registration
 * serial), a user-chosen name and a printable ticker. Only the network-assigned
 * token id identifies it: names and tickers may repeat. Built-in variants have
 * a ticker but no ticket. Issuance is split into what has been minted already
 * and what the next block can still mint; fees are deliberately absent, they
 * are transfers not supply.
 */

import type { ReactNode } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";

import {
  currentBlockReward,
  shortTokenId,
  tokenMinted,
  variantLabel,
  type Token,
} from "../lib/assets";
import { formatMultiPeriodTimeControl, formatTokenAmount } from "../lib/format";
import type { BlockSummary, NodeStatus } from "../types";
import { tableStyles } from "./tableStyles";

export function TokenTable({
  tokens,
  status,
  tip,
}: {
  tokens: readonly Token[];
  status: NodeStatus;
  tip: BlockSummary | undefined;
}): ReactNode {
  const difficulty =
    tip === undefined ? "-" : `${tip.wins_required} wins / ${tip.seal_bits} bits`;
  return (
    <ScrollView horizontal style={tableStyles.scroll} contentContainerStyle={tableStyles.scrollContent}>
      <View style={[tableStyles.table, styles.table]}>
        <View style={tableStyles.header}>
          <Text variant="labelSmall" style={[styles.colVariant, tableStyles.muted]}>
            VARIANT
          </Text>
          <Text variant="labelSmall" style={[styles.colTime, tableStyles.muted]}>
            TIME CONTROL
          </Text>
          <Text variant="labelSmall" style={[styles.colTicket, tableStyles.muted]}>
            TICKET
          </Text>
          <Text variant="labelSmall" style={[styles.colTicker, tableStyles.muted]}>
            TICKER
          </Text>
          <Text variant="labelSmall" style={[styles.colTokenId, tableStyles.muted]}>
            TOKEN ID
          </Text>
          <Text variant="labelSmall" style={[styles.colMinted, tableStyles.muted]}>
            TOTAL MINTED
          </Text>
          <Text variant="labelSmall" style={[styles.colReward, tableStyles.muted]}>
            BLOCK REWARD
          </Text>
          <Text variant="labelSmall" style={[styles.colDifficulty, tableStyles.muted]}>
            DIFFICULTY
          </Text>
        </View>
        {tokens.map((token) => {
          const minted = tokenMinted(status, token);
          // The node's supply-cap check counts fees toward `minted`; the display
          // total does not, so cap the reward against the fee-inclusive counter.
          const againstCap = token.native ? status.minted : minted;
          const reward = currentBlockReward(token, status.height, againstCap);
          return (
            <View key={token.asset ?? token.symbol} style={tableStyles.row}>
              <Text variant="bodyMedium" style={styles.colVariant}>
                {variantLabel(token.variant)}
              </Text>
              <Text variant="bodyMedium" style={[styles.colTime, tableStyles.numeric]}>
                {formatMultiPeriodTimeControl(
                  token.timeControlInitialMs,
                  token.timeControlIncrementMs,
                  token.timeControlDelayMs,
                  token.timeControlFirstMoves,
                  token.timeControlPeriods,
                )}
              </Text>
              <Text variant="bodyMedium" style={[styles.colTicket, tableStyles.numeric]}>
                {token.ticket ?? "-"}
              </Text>
              <Text variant="bodyMedium" style={styles.colTicker}>
                {token.symbol}
              </Text>
              <Text
                variant="bodySmall"
                style={[styles.colTokenId, tableStyles.numeric, tableStyles.muted]}
                selectable
              >
                {shortTokenId(token.tokenId)}
              </Text>
              <Text variant="bodyMedium" style={[styles.colMinted, tableStyles.numeric]}>
                {formatTokenAmount(minted, token.symbol, token.decimals)}
              </Text>
              <Text variant="bodyMedium" style={[styles.colReward, tableStyles.numeric]}>
                {formatTokenAmount(reward, token.symbol, token.decimals)}
              </Text>
              <Text variant="bodyMedium" style={[styles.colDifficulty, tableStyles.numeric]}>
                {difficulty}
              </Text>
            </View>
          );
        })}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  table: { minWidth: 980 },
  colVariant: { width: 112 },
  colTime: { width: 116 },
  colTicket: { width: 64, textAlign: "right" },
  colTicker: { width: 72 },
  colTokenId: { width: 132, textAlign: "right" },
  colMinted: { flexGrow: 1, flexShrink: 1, minWidth: 150, textAlign: "right" },
  colReward: { width: 130, textAlign: "right" },
  colDifficulty: { width: 150, textAlign: "right" },
});
