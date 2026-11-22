/**
 * Manual token lookup by ticket, token id, name or ticker.
 *
 * The token tables show what the node approved or this browser already pinned;
 * a permissionless registration can otherwise be invisible. Paste a unique
 * ticket or token id, or a name/ticker when only one token uses it, to read the
 * chain's record and keep the token in the local registry. The lookup itself
 * goes to `GET /v1/tokens/{reference}`, so the displayed rules, clock and
 * issuance are the node's, never the entry's.
 */

import { useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, Divider, HelperText, Text, TextInput } from "react-native-paper";

import { fetchToken, NotFoundError } from "../api";
import { tokenFromLookup, variantLabel } from "../lib/assets";
import { formatMultiPeriodTimeControl, formatTokenAmount, shortAddress } from "../lib/format";
import type { ApprovedToken } from "../lib/tokenRegistry";
import type { TokenView } from "../types";
import { KeyValue, Mono } from "./Chrome";

export function TokenLookup({
  network,
  approved,
  onApprove,
  onUnapprove,
}: {
  network: string | null;
  approved: readonly ApprovedToken[];
  onApprove: (view: TokenView) => void;
  onUnapprove: (view: TokenView) => void;
}): ReactNode {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<TokenView | undefined>(undefined);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const reference = query.trim();

  const lookup = async (): Promise<void> => {
    if (reference === "" || loading) return;
    setLoading(true);
    setError(undefined);
    try {
      setResult(await fetchToken(reference));
    } catch (failure) {
      setResult(undefined);
      setError(
        failure instanceof NotFoundError
          ? new Error(`No token on ${network ?? "this network"} uses ${reference}.`)
          : failure instanceof Error
            ? failure
            : new Error(String(failure)),
      );
    } finally {
      setLoading(false);
    }
  };

  const token = result === undefined ? undefined : tokenFromLookup(result);
  const pinned =
    result !== undefined &&
    approved.some(
      (entry) => entry.network === result.network && entry.tokenId === result.token_id,
    );

  return (
    <View style={styles.form}>
      <View style={styles.search}>
        <TextInput
          mode="outlined"
          dense
          label="Token id, ticket, name or ticker"
          value={query}
          onChangeText={(value) => {
            setQuery(value);
            setError(undefined);
          }}
          autoCapitalize="none"
          autoCorrect={false}
          onSubmitEditing={() => void lookup()}
          style={styles.input}
        />
        <Button
          mode="contained"
          loading={loading}
          disabled={reference === "" || loading}
          onPress={() => void lookup()}
        >
          Look up
        </Button>
      </View>
      <HelperText type="info" visible>
        The 64-hex token id is the identity. A four-digit ticket is also unique;
        names and tickers may repeat, so they resolve only when exactly one token uses them.
      </HelperText>
      {error !== undefined && (
        <HelperText type="error" visible>
          {error.message}
        </HelperText>
      )}
      {token !== undefined && result !== undefined && (
        <View style={styles.result}>
          <View style={styles.resultRow}>
            <Text variant="titleMedium">
              {token.name} ({token.symbol})
            </Text>
            <Text variant="bodySmall" style={styles.muted}>
              {result.approved || pinned ? "shown in the tables" : "not yet approved"}
            </Text>
          </View>
          <KeyValue label="Ticket">{result.ticket ?? "legacy token, no ticket"}</KeyValue>
          <KeyValue label="Token id" mono>
            <View style={styles.monoWrap}>
              <Mono copy>{result.token_id}</Mono>
            </View>
          </KeyValue>
          <KeyValue label="Ruleset">{variantLabel(result.ruleset ?? result.rules)}</KeyValue>
          <KeyValue label="Time control">
            {formatMultiPeriodTimeControl(
              result.time_control_initial_ms,
              result.time_control_increment_ms,
              result.time_control_delay_ms,
              result.time_control_first_moves,
              result.time_control_periods,
            )}
          </KeyValue>
          <KeyValue label="Opening lines">
            {result.openings.length === 0
              ? "shared book"
              : `${result.openings.length} token lines`}
          </KeyValue>
          <KeyValue label="Minted">
            {formatTokenAmount(result.minted, result.symbol, result.decimals)}
          </KeyValue>
          <KeyValue label="Owner" mono>
            {shortAddress(result.owner)}
          </KeyValue>
          <Divider />
          <View style={styles.resultRow}>
            {pinned ? (
              <Button mode="outlined" onPress={() => onUnapprove(result)}>
                Remove from this browser
              </Button>
            ) : (
              <Button mode="contained" onPress={() => onApprove(result)}>
                Keep this token
              </Button>
            )}
            {!result.approved && !pinned && (
              <Text variant="bodySmall" style={styles.muted}>
                Keeping it adds the token to this browser&apos;s approved registry only.
              </Text>
            )}
          </View>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  form: { gap: 6 },
  search: { flexDirection: "row", alignItems: "center", gap: 8 },
  input: { flexGrow: 1, flexShrink: 1 },
  result: {
    gap: 8,
    padding: 12,
    borderRadius: 8,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: "rgba(128,128,128,0.4)",
  },
  resultRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    flexWrap: "wrap",
    gap: 8,
  },
  muted: { opacity: 0.7 },
  monoWrap: { flexShrink: 1 },
});
