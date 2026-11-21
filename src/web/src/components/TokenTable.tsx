/**
 * The token summary: one row per chess variant and the asset it mints.
 *
 * A token is identified by its variant, clock and ticker; issuance is split into
 * what has been minted already and what the next block can still mint. Fees are
 * deliberately absent from both numbers: they are transfers, not new supply.
 */

import type { ReactNode } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";

import {
  currentBlockReward,
  tokenMinted,
  variantLabel,
  type Token,
} from "../lib/assets";
import { formatTimeControl, formatTokenAmount } from "../lib/format";
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
          <Text variant="labelSmall" style={[styles.colTicker, tableStyles.muted]}>
            TICKER
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
                {formatTimeControl(token.timeControlInitialMs, token.timeControlIncrementMs)}
              </Text>
              <Text variant="bodyMedium" style={styles.colTicker}>
                {token.symbol}
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
  table: { minWidth: 760 },
  colVariant: { width: 96 },
  colTime: { width: 96 },
  colTicker: { width: 64 },
  colMinted: { flexGrow: 1, flexShrink: 1, minWidth: 150, textAlign: "right" },
  colReward: { width: 130, textAlign: "right" },
  colDifficulty: { width: 150, textAlign: "right" },
});
