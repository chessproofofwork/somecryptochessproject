# Create your own crypto with the CLI

On SCCP a new **crypto is a chain-registered variant token**, not a new
blockchain and not a deployed contract. One signed transaction names a ruleset
the network already verifies, fixes the token's supply schedule, and derives its
asset id. After that transaction is in a block, the token behaves like the
built-in Chess960 asset: it has balances, can be transferred, can be paired in a
constant-product pool, and is minted when the chain settles games of its
variant.

The registration is permissionless but **immutable**. There is no owner key, no
mint function, no metadata edit and no way to change the name, symbol, decimals,
ruleset or supply later. Choose the parameters once, carefully.

This page uses **testnet** coins, which have no value. The same commands work on
`devnet`; on `mainnet` the registration fee is real money and a mistake cannot be
undone.

## What you need

| Item | Why |
| --- | --- |
| `sccp` installed | See [`INSTALL.md`](INSTALL.md) for the shared install and node setup. |
| A funded keyfile | The registration pays one network fee in native **ZQWX**. The fee comes from the spendable balance; `--initial-supply` is minted, not paid for. |
| A node | The CLI reads the account and nonce from it, then submits the signed transaction to it. |
| A token name, symbol and supply plan | All of them are fixed at registration. |

Create and fund a wallet first if you have not already:

- [`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) — keyfile, faucet,
  balance in five minutes.
- [`SEND_COINS.md`](SEND_COINS.md) — fees, nonces, confirmations and the
  one-transfer-at-a-time rule.

For the run below, set these in the shell. Replace the key path with your own;
keep the keyfile outside the repository.

```bash
export SCCP_NETWORK=testnet
export NODE="http://127.0.0.1:8645"          # or the hosted testnet node
export KEY="$HOME/.sccp/testnet-alice.key"

sccp --version
curl -sS "$NODE/v1/status" | jq '{network, height, tip_hash}'
sccp wallet balance --network testnet --key "$KEY" --node "$NODE"
```

The balance line must show enough **spendable** ZQWX for one fee. On the shipped
networks the minimum fee is `1000` base units (`0.00001000` ZQWX). If the node is
on another network, the CLI refuses the keyfile before signing anything.

---

## 1. Decide what the token is

A registration reuses an existing ruleset. It does **not** upload code, so the
only game rules available are the ones the node already ships:

| `--ruleset` | Games settle as | Typical use |
| --- | --- | --- |
| `standard` | normal chess | a new asset whose games are ordinary chess |
| `chess960` | Fischer Random / C960 | a second Chess960-denominated asset |

The token's `--name` is the variant label miners, the relay and proofs use. It
must be a new lowercase slug: 1–32 characters from letters, digits, `-` and
`_`, starting with a letter or digit.
`standard` and `chess960` are reserved and can never be registered again.

The asset id is derived, not issued:

```text
H("sccp/v1/asset", {variant, symbol, decimals, ruleset})
```

That means the id commits to the variant name, symbol, decimals and ruleset. It
contains no issuer key, so there is nobody to trust after registration. The
exact payload and derivation are normative in
[`PROTOCOL.md`](../design/PROTOCOL.md) section 10.2; the design rationale is in
[`BIG_PICTURE.md`](../design/BIG_PICTURE.md) section 5.3.

### The parameters this example fixes

This page registers **Dragon Chess**, a standard-rules token with a limited
supply:

| Flag | Example | Meaning |
| --- | --- | --- |
| `--name` | `dragonchess` | Variant slug. Immutable and reserved forever. |
| `--ruleset` | `standard` | Existing rules that settle its games. |
| `--symbol` | `DRG` | Display symbol, 1–16 characters. Pick a unique one. |
| `--display-name` | `Dragon Chess` | Human-readable label, up to 64 characters. |
| `--decimals` | `8` | 0–18. Committed to the asset id; use `8` unless you need otherwise. |
| `--subsidy` | `50` | Whole coins minted per block that settles a Dragon Chess game. |
| `--initial-supply` | `100` | Whole coins minted to the registering account in the registration block. |
| `--max-supply` | `21000000` | Lifetime cap, in whole coins. Must be paired with `--halving-interval`. |
| `--halving-interval` | `210000` | Blocks between subsidy halvings, counted from chain height. |
| `--min-plies` | `20` | Shorter games do not earn the token. |
| `--max-games-per-block` | `1` | Games of this token a block may settle, as served by the node. |
| `--opening-book` | *(omit)* | Defaults to the network's real `book_version`. |

The example omits two clock flags that also become immutable:
`--time-control-initial-ms` (default `600000`, ten minutes) and
`--time-control-increment-ms` (default `0`). Both must be whole seconds. Set
them if the token's games should have a different clock from the network
variant.

Two supply modes are possible:

- **Capped**: pass both `--max-supply` and `--halving-interval`. The subsidy is
  halved every `--halving-interval` blocks and no reward is paid once
  `max_supply * 10^decimals` base units have been minted.
- **Uncapped**: omit both. The token pays `--subsidy` forever.

`--initial-supply` counts against the cap exactly like a mined reward. If you
set a cap of 21,000,000 coins and mint 100 to yourself at registration, those 100
are part of the 21,000,000, not extra.

The CLI takes **whole coins** for `--subsidy`, `--initial-supply` and
`--max-supply`. It converts them to base units using `--decimals`. A
`--decimals 2` token with `--subsidy 1` pays `100` base units per reward, not
`100_000_000`.

Keep `--decimals 8` for this walkthrough. The registration flags convert with
the token's own decimal count, but the transfer and DEX commands convert their
whole-coin amounts at the network's 8-decimal coin unit. A different decimal
count needs a tool that speaks base units; otherwise `--amount 5` means the
wrong number of token units.

Leave `--opening-book` unset in the normal case. The registration then records
the network's own book version, so the token plays the same book as the native
variant. Overriding it is an advanced choice: the relay must be able to load the
book version you name.

---

## 2. Check that the name is free

The built-in variants are not returned by `dex tokens`. The command lists only
tokens created by `register_token`:

```bash
sccp dex tokens --node "$NODE"
```

A fresh chain says:

```text
no registered tokens on this chain
```

`standard` and `chess960` are still reserved even when the list is empty. If the
node already has a token named `dragonchess`, its line appears here and the
registration will be rejected. Names and asset ids are never reused, even if the
token has no supply or was never traded.

To inspect one token in detail, ask the node directly:

```bash
curl -sS "$NODE/v1/tokens/dragonchess" | jq
```

That endpoint accepts a name, a symbol or an asset id when the reference is
unambiguous.

## 3. Register the token

The command builds the transaction from the node's account view, signs it with
the keyfile, and submits it. Nothing secret leaves the machine.

```bash
sccp dex register-token \
    --network testnet \
    --key "$KEY" \
    --name dragonchess \
    --ruleset standard \
    --symbol DRG \
    --display-name "Dragon Chess" \
    --subsidy 50 \
    --initial-supply 100 \
    --max-supply 21000000 \
    --halving-interval 210000 \
    --min-plies 20 \
    --max-games-per-block 1 \
    --node "$NODE"
```

```text
registered token dragonchess (DRG) as asset <64-hex-asset-id>
txid: <txid>
```

Registration is **one transaction**. The human output gives you the two values
you need later:

- the **variant name** (`dragonchess`), used by miners and the relay;
- the **asset id**, used by transfers and pools.

Scripts should ask for JSON. The asset id is then a field, not text to parse:

```bash
sccp --json dex register-token \
    --network testnet \
    --key "$KEY" \
    --name dragonchess \
    --ruleset standard \
    --symbol DRG \
    --display-name "Dragon Chess" \
    --subsidy 50 \
    --initial-supply 100 \
    --max-supply 21000000 \
    --halving-interval 210000 \
    --min-plies 20 \
    --max-games-per-block 1 \
    --node "$NODE" \
    | tee "$HOME/.sccp/dragonchess-registration.json"
```

```json
{
  "txid": "<txid>",
  "network": "testnet",
  "type": "register_token",
  "from": "sccpt1...",
  "nonce": 4,
  "fee": 1000,
  "accepted": true,
  "variant": "dragonchess",
  "asset": "<64-hex-asset-id>",
  "symbol": "DRG",
  "ruleset": "standard",
  "initial_supply": 10000000000,
  "subsidy": 5000000000
}
```

`accepted: true` means the node put the transaction in its **mempool**. It does
not mean a block includes it yet. Do not submit another transaction from the same
address until this one is mined: the next nonce comes from the chain, not from
the mempool, so a second transaction would reuse the same nonce and be refused.

### What can fail at this point

The CLI checks the local view first and the node checks again when the block is
built. The common refusals are:

| Message | Cause |
| --- | --- |
| `variant 'dragonchess' is already known` | A built-in or registered token already owns that name. Names are permanent. |
| `asset id ... is already registered` | The name, symbol, decimals and ruleset hash to an existing token. Change one of them. |
| `payload field 'variant' must be a lowercase slug...` | The name has spaces, uppercase letters or an unsupported character. |
| `invalid token parameters` | A value violates the protocol, for example `min_plies` above the network maximum or `max_supply` without `halving_interval`. |
| `initial_supply ... exceeds the ... supply cap` | The initial mint is larger than `max_supply`. |
| `cannot pay the ... ZQWX fee` | The registration is paid in native ZQWX; the token balance does not exist yet. |

The exact consensus checks are in
[`PROTOCOL.md`](../design/PROTOCOL.md) under `register_token`; do not rely on the
CLI alone to remember them.

---

## 4. Wait for the registration block

Capture the two identifiers from the JSON:

```bash
REGISTRATION="$HOME/.sccp/dragonchess-registration.json"
TXID="$(jq -r .txid "$REGISTRATION")"
ASSET="$(jq -r .asset "$REGISTRATION")"

curl -sS "$NODE/v1/transactions/$TXID" | jq '{height, in_mempool, block_hash}'
```

While it is pending:

```json
{"height": null, "in_mempool": true, "block_hash": null}
```

After a miner includes it:

```json
{"height": 42, "in_mempool": false, "block_hash": "<block-hash>"}
```

Then the chain has the token and the initial supply is credited to the
registering address:

```bash
sccp dex tokens --node "$NODE"
```

```text
dragonchess (DRG) standard asset <64-hex-asset-id> cap 21000000 coins
  minted 10000000000 of initial 10000000000, subsidy 5000000000 base units every win, 8 decimals
```

`minted`, `initial_supply` and `subsidy` in that view are **base units**:
`10_000_000_000` is 100 whole DRG at 8 decimals. The cap is shown in whole
coins because `max_supply` is stored that way.

`wallet balance` reads the token from the account's `assets` map and shows it as
a shortened asset id, because the static network parameters do not contain
tokens created after genesis:

```bash
sccp wallet balance --network testnet --key "$KEY" --node "$NODE"
```

```text
sccpt1... 999.99999000 (spendable 999.99999000, bonded 0.00000000, immature 0.00000000)
  <first-8-hex>… spendable 100.00000000, immature 0.00000000
```

The native ZQWX line stays separate from the DRG line. A token cannot pay a fee;
every DRG transaction still needs a little ZQWX for the network fee.

---

## 5. Move the token

Transfer it exactly like native ZQWX, but name the asset id the registration
printed:

```bash
sccp wallet send \
    --network testnet \
    --key "$KEY" \
    --to sccpt1... \
    --asset "$ASSET" \
    --amount 5 \
    --node "$NODE"
```

```text
sent 5.00000000 <64-hex-asset-id> to sccpt1... (fee 0.00001000)
txid: <txid>
```

The amount is in whole DRG. The fee is ZQWX and is paid by the sender. The
recipient needs no setup: the token arrives in the same `assets` map as every
other asset, and the address does not need to have registered anything.

The registration block and any transfer must be included before the next action
that depends on them. `wallet send` reads the next nonce from the node, so wait
for inclusion between sends from one address. Settlement and fee details are in
[`SEND_COINS.md`](SEND_COINS.md).

---

## 6. Give it a market

A token with no pool still has balances and can be transferred, but there is no
on-chain price. The DEX creates one pool per asset pair; the pool id is derived
from the two asset ids, so the same pair cannot have two pools.

This initial deposit pairs **50 ZQWX** (the omitted first side) with **100 DRG**
(`--asset-b` is the token):

```bash
sccp dex create-pool \
    --network testnet \
    --key "$KEY" \
    --amount-a 50 \
    --asset-b "$ASSET" \
    --amount-b 100 \
    --node "$NODE"
```

```text
created pool <64-hex-pool-id>
txid: <txid>
```

The chain orders the pair by asset id. Native ZQWX is the empty asset id, so it
sorts first and is `asset_a` in a token pair. Read the stored order from the pool
itself:

```bash
POOL="<64-hex-pool-id>"
sccp dex pool --pool "$POOL" --node "$NODE"
```

Wait until the pool transaction is in a block before quoting or swapping; an
unknown pool is a 404, not a pending pool.

```bash
sccp dex pools --node "$NODE"
sccp dex pool --pool "$POOL" --node "$NODE"
```

```text
<64-hex-pool-id> ZQWX/<64-hex-asset-id>
reserves 5000000000/10000000000 shares 7071067811 fee 30 bps
```

The fee is the network's `dex_fee_bps` (30 basis points on every shipped
network), not a pool parameter. Fees stay in the input reserve for the liquidity
providers.

### Quote, then swap

Quote without signing anything:

```bash
sccp --network testnet dex quote \
    --pool "$POOL" \
    --amount-in 10 \
    --asset-in "$ASSET" \
    --node "$NODE"
```

```text
in   10.00000000 at <64-hex-asset-id>
out  4.53305446
min  4.51038918 (0.50% slippage)
```

Then swap with the same input and a minimum the signature enforces:

```bash
sccp dex swap \
    --network testnet \
    --key "$KEY" \
    --pool "$POOL" \
    --amount-in 10 \
    --asset-in "$ASSET" \
    --min-out 4 \
    --node "$NODE"
```

```text
swapped 10.00000000 for at least 4.00000000 (fee 0.00001000)
txid: <txid>
```

Omit `--min-out` to let the CLI use the current reserves and
`--slippage-bps` (default 50 = 0.50%). The signed payload always carries a
minimum output, so a price move before inclusion cannot fill below that bound.
The swap input must be in the pool, and the sender still needs ZQWX for the fee.

Add liquidity later with the two amounts in the pool's own `asset_a`/`asset_b`
order:

```bash
sccp dex add-liquidity \
    --network testnet \
    --key "$KEY" \
    --pool "$POOL" \
    --amount-a 10 \
    --amount-b 20 \
    --node "$NODE"
```

The deposit must be proportional to the current reserves or the chain mints the
smaller share count, so quote the pool first. Removing liquidity redeems shares
for both sides:

```bash
sccp dex remove-liquidity \
    --network testnet \
    --key "$KEY" \
    --pool "$POOL" \
    --shares 1000 \
    --node "$NODE"
```

---

## 7. How the supply grows

Registration creates the asset and its initial supply. It does not create blocks
or play games. The token is minted by the normal proof-of-work loop:

1. Miners register for the token's **variant** (`dragonchess`), not for
   `standard` or `chess960`.
2. The relay pairs those miners and certifies games under the token's ruleset,
   minimum plies, clock and opening book.
3. A block that settles a token proof mints that token's subsidy **once for that
   variant**, paid to the block miner.
4. The subsidy halves at each `halving_interval` boundary measured in absolute
   chain height, and stops at the cap. The initial supply is already counted.

A token with only one registered miner cannot play: pairing needs two seats. A
token with no miners keeps its initial supply and existing balances but never
mints another coin. The operator flow for relay, miner registration, availability
and proofs is in
[`RUN_A_TESTNET_MINER.md`](../setup/RUN_A_TESTNET_MINER.md); this page stops at
the asset and its first market. On the miner side, the token name is the value
for `--variant`: use `--variant dragonchess` with both `miner register` and
`miner run`, and give the miner the node's chain database through `--data-dir`.
The relay gets the token's ruleset and clock from the node, so it needs no local
token definition.

The relay learns the token's parameters from `GET /v1/tokens`, so a running relay
does not need a restart when a new token is registered. Its next epoch roster can
include miners whose on-chain registration names the new variant.

---

## 8. The irreversibility checklist

Before you submit a registration on a network you care about:

- **Name**: the slug is reserved forever, even if the token is never used.
- **Ruleset**: `standard` and `chess960` play differently; a token cannot switch.
- **Symbol and decimals**: both are part of the asset id. A different symbol or
  decimal count is a different token, not an edit.
- **Supply**: `max_supply` and `halving_interval` are fixed together. You cannot
  add a cap later, remove one, or change the subsidy.
- **Initial supply**: it is public, minted to the registering address, and counts
  against the cap.
- **Keyfile**: the registering key has no special rights after the transaction,
  but it must still pay the fee. Back up the keyfile and mnemonic as described in
  [`OPEN_A_WALLET.md`](OPEN_A_WALLET.md).

There is no admin key to recover, freeze, burn or reissue the token. If the
parameters are wrong, the only remedy is to register a new token with a new name
and a new asset id.

---

## 9. Troubleshooting

| Symptom | What it means |
| --- | --- |
| `unknown asset 'dragonchess'` from `dex quote`, `dex swap` or `dex create-pool` | Registered tokens are named by their **64-character asset id** on the DEX commands; built-in variants can still use their name or symbol. Get the id from `dex tokens --json` or the registration output. |
| `transaction ... already has a pending transaction with nonce N` | The previous transaction from that address is still in the mempool. Wait for it to be included. |
| `the node serves 'devnet', not 'testnet'` | `--node` and the keyfile disagree. Point at the right node or pass the matching `--network`. |
| `no chain database at ...` | The command could not read the node and fell back to `--data-dir`. Pass the URL of the node that serves the chain. |
| `pool ... already exists` | A pair has one pool by construction. Use `dex pools` to find its id and add liquidity instead. |
| `the swap pays ..., below the requested minimum` | The price moved past your `--min-out` (or the quoted minimum). Re-quote and sign a new swap. |
| `cannot pay the ... ZQWX fee` | Token balances do not pay fees. Send a little native ZQWX to the address. |

---

## Next

- The full transaction and asset-id rules: [`PROTOCOL.md`](../design/PROTOCOL.md).
- The variant-token design and its limits:
  [`BIG_PICTURE.md`](../design/BIG_PICTURE.md).
- Run the relay and miners that make the token's games count:
  [`RUN_A_TESTNET_MINER.md`](../setup/RUN_A_TESTNET_MINER.md).
- Exercise C960, pools and swaps on a disposable devnet:
  [`TEST_DEVNET_BRIDGE_DEX.md`](TEST_DEVNET_BRIDGE_DEX.md).
- All guides by task: [`docs/README.md`](../README.md).
