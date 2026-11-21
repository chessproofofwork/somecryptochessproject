# Test the bridge on testnet (Sepolia)

This is the testnet counterpart to
[`TEST_DEVNET_BRIDGE_DEX.md`](TEST_DEVNET_BRIDGE_DEX.md). It covers the two jobs
that are different on a public testnet:

1. **Operator setup** — get Sepolia ETH, deploy `EthHtlc`, and point a
   `testnet` SCCP node at that contract and RPC.
2. **User flow** — fund a `testnet` ZQWX wallet, publish an order on the Bridge
   page, lock Sepolia ETH, and claim both legs with MetaMask.

The bridge is the same trustless HTLC as on devnet. The SCCP node never holds an
Ethereum key: it publishes the Sepolia chain id, contract and confirmation depth
through `GET /v1/bridge`, and the browser signs the ETH leg through an EIP-1193
wallet. The SCCP side remains an ordinary on-chain HTLC.

The mapping is fixed in `sccp.bridge.config` and cannot be overridden:

| SCCP network | Ethereum | Chain id | ETH confirmations | SCCP margin |
| --- | --- | ---: | ---: | ---: |
| `testnet` | Sepolia | `11155111` | `6` | `24` blocks |

A fresh testnet stack ships with the bridge **visible but disabled**. The hosted
testnet currently reports the same:

```bash
curl -sS http://app.proofofchess.com:8645/v1/bridge \
  | jq '{network, eth_chain_name, eth_chain_id, configured, eth_htlc_contract}'
```

```json
{
  "network": "testnet",
  "eth_chain_name": "Sepolia",
  "eth_chain_id": 11155111,
  "configured": false,
  "eth_htlc_contract": null
}
```

`configured: false` means the Bridge page can show SCCP HTLC activity but cannot
create an ETH leg. Sections 3–4 fix that; section 5 funds the wallets. If you do
not operate the node, only its operator can change this; ask them for the Sepolia
contract address and RPC.
There is no shared public Sepolia contract in this repository to point at, so
each deployment needs its own `EthHtlc` address.

> **Everything here is test value.** Sepolia ETH and testnet ZQWX have no
> monetary value. Never reuse a mainnet key, and never point a mainnet node at
> this contract.

---

## 0. Prerequisites

You need:

- a checkout and the CLI from [`INSTALL.md`](INSTALL.md);
- a reachable `testnet` node (`http://app.proofofchess.com:8645` for the hosted
  stack, or your own);
- **Foundry** (`forge`, `cast`) to deploy and inspect the contract — install it
  from <https://book.getfoundry.sh/getting-started/installation>;
- a Sepolia RPC endpoint and Sepolia ETH for gas and the swap;
- MetaMask (or another EIP-1193 wallet) and two browser profiles for the maker
  and taker;
- two `testnet` SCCP keyfiles, one per role.

Set the shared variables once per shell:

```bash
export SCCP_NETWORK=testnet
export SCCP_NODE='http://app.proofofchess.com:8645'
# or: export SCCP_NODE='http://127.0.0.1:8645'

curl -sS "$SCCP_NODE/v1/status" | jq '{network, height, tip_hash}'
```

The response must say `"network": "testnet"`. The hosted deployment is plain
HTTP and resets on every deploy, so addresses and balances from an earlier
deploy are gone; keep the keyfiles, but fund them again.

Use two terminals (or more): one for the node and one for commands. Shell
variables do not cross terminals, so repeat the `export` lines where needed.

---

## 1. What the two roles need

The bridge moves **ETH → ZQWX** in this guide. The maker sells ZQWX and receives
ETH; the taker pays ETH and receives ZQWX.

| Role | Needs on Sepolia | Needs on SCCP testnet |
| --- | --- | --- |
| Maker | an address to receive ETH, plus gas to claim it | ZQWX to sell, plus the lock fee |
| Taker | the ETH principal, plus gas to lock it | an address to receive ZQWX |

The taker locks ETH first. The maker verifies that lock on Ethereum, then locks
ZQWX under the same SHA-256 hash. The taker claims ZQWX, revealing the preimage;
the maker claims ETH with it. Each leg has its own timelock refund.

The testnet defaults are deliberately conservative:

- the maker waits **6 ETH confirmations** before locking ZQWX;
- the taker waits **`reorg_depth + 1` = 11 SCCP confirmations** before claiming
  ZQWX;
- the maker waits **10 more SCCP blocks** after that claim before claiming ETH;
- the ZQWX timelock is at least **24 SCCP blocks** before the ETH timelock.

At the testnet target of 30 seconds per block, 24 blocks is about 12 minutes of
margin. The UI's default 6-hour ETH lock leaves plenty of room. Do not shorten
it below what the page accepts.

---

## 2. Get Sepolia ETH and an RPC endpoint

### 2.1 Add Sepolia to the browser wallet

MetaMask usually knows Sepolia already. If it does not, add it manually:

| Field | Value |
| --- | --- |
| Network name | `Sepolia` |
| RPC URL | your Sepolia endpoint from section 2.2 |
| Chain ID | `11155111` |
| Currency symbol | `ETH` |
| Block explorer | `https://sepolia.etherscan.io` |

Use **two different Ethereum accounts**: one for the maker and one for the
taker. They can live in the same MetaMask installation, but the two browser
profiles must select different accounts. Never import a key that holds real
funds.

### 2.2 Get Sepolia ETH

Sepolia ETH is free test currency. Use a faucet you qualify for; eligibility
and drip sizes change, so check the page rather than assuming a fixed amount:

- <https://www.alchemy.com/faucets/ethereum-sepolia> — currently 0.1 ETH per
  address per 24 hours, with mainnet-balance and activity requirements;
- <https://cloud.google.com/application/web3/faucet/ethereum/sepolia> — Google
  Cloud Web3 faucet, Google sign-in required;
- <https://faucet.quicknode.com/ethereum/sepolia> — wallet connection and a
  small mainnet balance are currently required;
- <https://ethereum.org/en/developers/docs/networks/#sepolia> — the maintained
  list of Sepolia faucets.

Never pay for Sepolia ETH; any site selling it is a scam. Faucets are
rate-limited, so if one rejects your wallet, try another before asking the
testnet operator for a grant.

Ask for enough to cover both roles. With the example order below, the taker
locks `0.01 ETH` and needs gas; the maker needs gas only to claim. Two drips of
`0.1 ETH` are more than enough. Import the funded accounts into MetaMask and
record their addresses:

```bash
export ETH_MAKER=0x...
export ETH_TAKER=0x...
```

### 2.3 Get an RPC endpoint

The node needs a Sepolia JSON-RPC URL. The Bridge page also publishes that URL
so a wallet that does not yet know Sepolia can add it. Any provider works
(Alchemy, Infura, QuickNode, a self-hosted Sepolia node):

```bash
export SEPOLIA_RPC_URL='https://eth-sepolia.g.alchemy.com/v2/<your-key>'
# or: export SEPOLIA_RPC_URL='https://sepolia.infura.io/v3/<your-key>'

cast chain-id --rpc-url "$SEPOLIA_RPC_URL"
# 11155111
cast block-number --rpc-url "$SEPOLIA_RPC_URL"
```

The chain id must be `11155111`. The deploy script refuses to broadcast if it is
not.

> **The node publishes `SCCP_ETH_RPC_URL` in `GET /v1/bridge`.** That is how the
> Bridge page can offer to add Sepolia to MetaMask. Treat the URL as public: use
> a dedicated, rate-limited provider key, not a secret shared with other
> production systems.

---

## 3. Deploy `EthHtlc` on Sepolia

The SCCP node holds no Ethereum key. Deploy the contract once with a funded
Sepolia account, then give the node its address. `EthHtlc` has no owner, proxy
or admin function; the deployer pays gas and keeps no special power.

### 3.1 Install Foundry and the Solidity dependency

```bash
foundryup            # skip if forge and cast are already installed
cd contracts
forge install foundry-rs/forge-std --no-commit
forge test
```

`forge test` runs the contract's adversarial suite. A failing test means do not
deploy.

### 3.2 Deploy

From `contracts/`, with the deployer key in an environment variable and never in
the repository:

```bash
export SCCP_NETWORK=testnet
export DEPLOYER_PRIVATE_KEY=0x...

forge script script/Deploy.s.sol:Deploy \
  --rpc-url "$SEPOLIA_RPC_URL" \
  --private-key "$DEPLOYER_PRIVATE_KEY" \
  --broadcast
```

The script prints:

```text
SCCP network: testnet
Ethereum chain id: 11155111
EthHtlc: 0x...
Set SCCP_ETH_HTLC_CONTRACT to the address above.
```

Record the address and prove code exists there:

```bash
export SCCP_ETH_HTLC_CONTRACT=0x...
test "$(cast code "$SCCP_ETH_HTLC_CONTRACT" --rpc-url "$SEPOLIA_RPC_URL")" != "0x" \
  && echo "contract deployed"
```

Optional: verify the source on Sepolia Etherscan so users can read the exact
contract they are calling:

```bash
export ETHERSCAN_API_KEY=...
forge verify-contract "$SCCP_ETH_HTLC_CONTRACT" EthHtlc.sol:EthHtlc \
  --chain sepolia \
  --etherscan-api-key "$ETHERSCAN_API_KEY"
```

The contract is deployed once per Sepolia environment and shared by every user
of that node. Do not change the address while orders are open: an order does not
carry the contract address, so the node and the browser must agree on it.

---

## 4. Point the testnet node at Sepolia

The node reads these variables at start-up:

| Variable | Required | Meaning |
| --- | --- | --- |
| `SCCP_ETH_RPC_URL` | yes, for the browser to add Sepolia | Sepolia JSON-RPC endpoint |
| `SCCP_ETH_HTLC_CONTRACT` | yes | deployed `EthHtlc` address |
| `SCCP_ETH_CONFIRMATIONS` | no | defaults to `6` on testnet |
| `SCCP_ETH_SCCP_MARGIN` | no | defaults to `24` blocks; must exceed `reorg_depth` (`10`) |

`configured` is true as soon as the contract is set. Set the RPC too: without it
the page can use a wallet that already knows Sepolia, but it cannot offer to add
the network.

### 4.1 A node you run directly

```bash
export SCCP_ETH_RPC_URL="$SEPOLIA_RPC_URL"
export SCCP_ETH_HTLC_CONTRACT=0x...

sccp --network testnet --data-dir data node run --web --port 8645
```

The exports must be in the environment of the node process. If the node is
already running, stop it and start it again; there is no live reload for the
bridge environment.

### 4.2 The Compose stack

Add these lines to `.env` next to `docker-compose.yml` (create the file if it is
absent; it is git-ignored), or export the same values in the shell that runs
Compose:

```dotenv
SCCP_ETH_RPC_URL=https://sepolia.example/rpc
SCCP_ETH_HTLC_CONTRACT=0x...
```

Then recreate the node so the new environment takes effect:

```bash
docker compose up -d --no-build --force-recreate node
```

Compose already passes `SCCP_ETH_RPC_URL`, `SCCP_ETH_HTLC_CONTRACT`,
`SCCP_ETH_CONFIRMATIONS` and `SCCP_ETH_SCCP_MARGIN` into the `node` service.
`--force-recreate` makes the new environment take effect without deleting the
chain volume.

### 4.3 The hosted testnet

The deploy workflow copies the environment into the remote Compose command, so
the contract is configured with repository variables, not a file on the VM.

In GitHub: **Settings → Secrets and variables → Actions → Variables** and add:

- `SCCP_ETH_RPC_URL`
- `SCCP_ETH_HTLC_CONTRACT`
- optionally `SCCP_ETH_CONFIRMATIONS` and `SCCP_ETH_SCCP_MARGIN`

Then run **Actions → deploy-testnet-miners → Run workflow** (or merge to
`main`). The workflow passes those values to the node when it recreates the
stack.

> That workflow is destructive: it runs `docker compose down -v` and resets the
> chain. It is appropriate for this valueless testnet, not for a chain whose
> history matters. The web app itself does not need a rebuild: it reads
> `GET /v1/bridge` at runtime.

### 4.4 Verify the node's bridge document

```bash
curl -sS "$SCCP_NODE/v1/bridge" \
  | jq '{network, eth_chain_id, eth_chain_name, eth_rpc_url, eth_htlc_contract,
         eth_confirmations, sccp_margin, configured}'
```

A configured testnet node returns:

```json
{
  "network": "testnet",
  "eth_chain_id": 11155111,
  "eth_chain_name": "Sepolia",
  "eth_rpc_url": "https://sepolia.example/rpc",
  "eth_htlc_contract": "0x...",
  "eth_confirmations": 6,
  "sccp_margin": 24,
  "configured": true
}
```

The CLI reads the same document:

```bash
sccp bridge info --node "$SCCP_NODE"
```

If `configured` is still false, the node did not see
`SCCP_ETH_HTLC_CONTRACT`; fix section 4 and restart it. If `eth_rpc_url` is
`null`, users with an existing Sepolia network can still swap, but the page
cannot add the network automatically.

On `https://app.proofofchess.com/#/bridge`, a configured node shows
**Sepolia ready**. An unconfigured node shows **ETH not configured** and disables
the ETH actions.

---

## 5. Fund two SCCP testnet wallets

The maker needs ZQWX to sell; the taker needs a wallet to receive it. Create two
keyfiles outside the repository:

```bash
sccp wallet new --network testnet --out ~/.sccp/testnet-maker.key
sccp wallet new --network testnet --out ~/.sccp/testnet-taker.key

export MAKER="$(sccp wallet address --network testnet \
  --key ~/.sccp/testnet-maker.key)"
export TAKER="$(sccp wallet address --network testnet \
  --key ~/.sccp/testnet-taker.key)"
echo "maker $MAKER"
echo "taker $TAKER"
```

Both addresses start with `sccpt1`. Ask the testnet faucet for ZQWX. The hosted
policy is one grant of 1,000 coins per address per hour, with five grants per
client IP per hour; `GET /v1/faucet` is authoritative:

```bash
for ADDRESS in "$MAKER" "$TAKER"; do
  curl -sS -X POST "$SCCP_NODE/v1/faucet" \
    -H 'Content-Type: application/json' \
    -d "{\"address\":\"$ADDRESS\"}" | jq
done
```

A faucet response means the transfer is in the mempool, not that it is spendable
yet. Wait for the height to advance, then check:

```bash
sccp wallet balance --network testnet --key ~/.sccp/testnet-maker.key \
  --node "$SCCP_NODE"
sccp wallet balance --network testnet --key ~/.sccp/testnet-taker.key \
  --node "$SCCP_NODE"
```

The maker only needs the 20 ZQWX used below plus the lock fee. The taker does not
need ZQWX: it receives them from the maker's HTLC. See
[`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) for the full faucet
and keyfile details.

---

## 6. Run the swap in the Bridge page

Use two browser profiles, A and B. Each profile has its own IndexedDB keyfile,
its own MetaMask account and its own local swap store. On `#/wallet`, open the
keyfile you funded in section 5 (**Open**), one keyfile per profile. Do not run
both roles in one profile: the wallet session is one SCCP key and one Ethereum
account.

### 6.1 Maker: publish the sell order

In profile A:

1. Open `https://app.proofofchess.com/#/bridge` (or
   `http://127.0.0.1:8645/#/bridge` for a local node).
2. Open or create the maker's SCCP wallet on `#/wallet`.
3. Return to `#/bridge` and confirm the status says **Sepolia ready**.
4. Select **Sell ZQWX**.
5. Press **Connect Ethereum**, choose the maker account, and approve Sepolia.
6. Enter:
   - **ZQWX to sell**: `20`
   - **ETH to receive**: `0.01`
   - **expiry**: `24` hours
7. Press **Publish sell order**. The order is signed by the SCCP key; the maker's
   Ethereum address is bound into that signature. Nothing is escrowed yet.
8. The order appears under **My orders** with `waiting for a taker`.

If the page says **ETH not configured**, the node is still missing the contract
from section 4. If it says the wallet is on the wrong chain, switch MetaMask to
Sepolia chain `11155111`.

### 6.2 Taker: lock ETH and publish the take

In profile B:

1. Open `#/bridge → Buy ZQWX` and open the taker's SCCP wallet if prompted.
2. Select the maker's `20 ZQWX` order. Check the price, maker and order expiry.
3. Press **Connect Ethereum** and choose the taker account.
4. Keep **6 hours** for the ETH lock, or choose 12 or 24. Do not use a shorter
   lock: the SCCP leg must expire at least 24 blocks before the ETH leg.
5. Press **Lock ETH and take order**. MetaMask signs `lock(hashlock, timelock,
   beneficiary)` and sends `0.01 ETH` to the contract.
6. The page stores the 32-byte preimage in this origin's `localStorage` before
   publishing the take. **Do not clear site data while the swap is open.** The
   preimage is what lets the taker claim ZQWX.
7. Wait for the take to appear and for the maker's ZQWX lock.

The ETH lock is verifiable independently on
`https://sepolia.etherscan.io/address/<contract>` and through the node's
`bridge eth-lock-info` command once the lock id is known (section 7).

### 6.3 Maker: verify the ETH lock and lock ZQWX

Back in profile A, under **Sell ZQWX → My orders**:

1. The order now shows the taker, the ETH transaction and both timelocks.
2. Press **Verify ETH lock and lock ZQWX**. The page reads the transaction
   through MetaMask, waits for **6 Sepolia confirmations**, and checks the
   depositor, contract, value, hashlock, timelock and beneficiary.
3. Only after those checks pass does it sign the SCCP `htlc_lock` for 20 ZQWX.
4. Wait for the SCCP transaction to be mined. The taker cannot claim until the
   lock has **11 SCCP confirmations** (`reorg_depth + 1`).

If the verification fails, the page does not lock ZQWX. The taker still has the
ETH locked and can refund it after the ETH timelock; the maker has lost nothing
but time.

### 6.4 Taker: claim ZQWX and reveal the preimage

In profile B:

1. Wait until **ZQWX status** is `locked` and the page reports 11/11
   confirmations.
2. Press **Claim ZQWX and reveal secret**.
3. The SCCP claim writes the preimage into consensus state. The taker's ZQWX
   balance rises by 20 (less the claim fee).
4. Keep the tab open until the claim is mined. The local swap record is cleared
   only after the node reports the HTLC as `claimed`.

### 6.5 Maker: claim ETH with the revealed preimage

Back in profile A:

1. Wait until the ZQWX claim is final: the page needs **10 more SCCP blocks**
   after the claim height. At the 30-second target that is about five minutes.
2. Under **My orders**, press **Claim ETH with preimage**. The preimage comes
   from the on-chain ZQWX record, so it does not have to be copied by hand.
3. MetaMask signs `claim(lockId, preimage)`; the contract pays `0.01 ETH` to the
   maker and marks the lock claimed.
4. Wait for **6 Sepolia confirmations**. The order is now complete.

Both legs are now closed. The **Activity** tab lists the SCCP HTLC, its timelock
and the revealed preimage; Sepolia Etherscan shows the contract's `Claimed`
event.

### 6.6 If one side stalls

- **Taker's ETH is locked and the maker never locks ZQWX.** Wait until the ETH
  timelock passes, then use **Buy ZQWX → Refund expired ETH**. Only the original
  depositor can call the contract's `refund`.
- **Maker locked ZQWX and the taker never claims.** Wait until the SCCP timelock
  height, then use **Sell ZQWX → My orders → Refund ZQWX**.
- The ZQWX leg expires first by `sccp_margin` blocks. A last-moment ZQWX claim
  still leaves the maker time to claim ETH; that is why the page refuses a take
  whose SCCP timelock is too close to the ETH timelock.

---

## 7. Check the swap from the CLI

The order book and the SCCP legs are readable without MetaMask:

```bash
# The node's Sepolia environment
sccp bridge info --node "$SCCP_NODE"

# Orders, best price first; a taken order remains readable
sccp bridge orders --node "$SCCP_NODE"

# One order and its take
export ORDER_ID=...
sccp bridge order --order-id "$ORDER_ID" --node "$SCCP_NODE"

# SCCP HTLCs for one participant or one hashlock
sccp bridge htlcs --address "$MAKER" --node "$SCCP_NODE"
export HASHLOCK=...
sccp bridge htlcs --hashlock "$HASHLOCK" --node "$SCCP_NODE"

# One SCCP HTLC, including the preimage once claimed
export HTLC_ID=...
sccp bridge htlc --htlc-id "$HTLC_ID" --node "$SCCP_NODE"

# Wallet balances
sccp wallet balance --network testnet --key ~/.sccp/testnet-maker.key \
  --node "$SCCP_NODE"
sccp wallet balance --network testnet --key ~/.sccp/testnet-taker.key \
  --node "$SCCP_NODE"
```

The ETH lock can be read directly from the contract when the lock id is known
(the UI derives it from the `Locked` event):

```bash
export LOCK_ID=0x...
sccp bridge eth-lock-info --lock-id "$LOCK_ID" --node "$SCCP_NODE"
```

Or with Foundry:

```bash
cast call "$SCCP_ETH_HTLC_CONTRACT" \
  "getLock(bytes32)(address,address,bytes32,uint64,uint256,bool,bool)" \
  "$LOCK_ID" \
  --rpc-url "$SEPOLIA_RPC_URL"
```

### Why `bridge eth-lock` and `bridge eth-claim` are not the testnet path

Those two CLI commands send from an account the JSON-RPC node has **unlocked**.
That is exactly how Anvil works on devnet. A Sepolia provider does not hold
private keys, so the supported public-testnet path is the Bridge page and
MetaMask. For automation, sign locally with a dedicated test key and submit with
`cast send`; never unlock a funded account on a shared RPC.

The order book is off-consensus and in the node's memory. A node restart drops
the book (orders can be re-posted), but a taken swap's SCCP HTLC remains in
consensus state and stays visible at `#/accounts/<address>` and through
`bridge htlcs`.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `/v1/bridge` says `"configured": false` | node has no `SCCP_ETH_HTLC_CONTRACT` | set it and restart the node (section 4) |
| The page says **ETH not configured** | same, or the page is talking to another node | check `$SCCP_NODE/v1/bridge`, then reload |
| The page says **Sepolia ready** but cannot add the network | `eth_rpc_url` is `null` | set `SCCP_ETH_RPC_URL` and restart |
| `switch the Ethereum wallet to Sepolia (chain 11155111)` | MetaMask is on another chain | switch to Sepolia, or add it with the table in section 2.1 |
| `the wallet is on chain 1, expected 11155111` | wrong network selected | switch MetaMask to Sepolia |
| `insufficient funds for gas * price + value` | the taker has no Sepolia ETH | use a faucet in section 2.2 |
| `the ETH lock was not sent by the taker` | the selected MetaMask account is not the take's `taker_eth` | reconnect the account that locked the ETH |
| `the ETH transaction did not call the bridge contract` | node and user are looking at different contracts | restore the correct `SCCP_ETH_HTLC_CONTRACT`; do not lock ZQWX |
| `the ETH lock carries a different hashlock or timelock` | stale local swap or a mismatched transaction | do not lock ZQWX; refund the ETH after its timelock |
| `the ETH lock is unknown` | the transaction is not mined yet, or the hash is wrong | wait and check Sepolia Etherscan |
| ZQWX is locked but **Claim ZQWX** is disabled | fewer than 11 SCCP confirmations | wait for `reorg_depth + 1` confirmations |
| The maker cannot claim ETH yet | the ZQWX claim is not final | wait 10 more SCCP blocks |
| `execution reverted: Expired` on claim | the ETH timelock has passed | use the refund path |
| `execution reverted: NotYetClaimable` on refund | the timelock has not passed | wait for the timelock |
| HTTP `429` from `/v1/faucet` | address or client-IP cooldown | wait for `Retry-After`, or use another funded wallet |
| The testnet node was redeployed and balances vanished | the hosted stack resets its chain on deploy | recreate/fund the wallets; keyfiles are unchanged |
| `no code at` the contract address | wrong address or wrong chain | redeploy on Sepolia `11155111` and update the node |
| A Sepolia transaction stays pending | network congestion or a low fee | wait, then inspect the hash on Etherscan |

---

## 9. What a completed swap looks like

| Check | Pass condition |
| --- | --- |
| `GET /v1/bridge` | `network: testnet`, chain `11155111`, non-null contract, `configured: true` |
| `GET /v1/orders` or `sccp bridge orders` | the maker's order remains listed; a taken order stays readable |
| `sccp bridge order --order-id ...` | the take names the taker, ETH transaction, hashlock and both timelocks |
| `sccp bridge htlcs --hashlock ...` | one ZQWX lock from maker to taker, then `status: claimed` with a preimage |
| `sccp bridge eth-lock-info --lock-id ...` | the taker is depositor, the maker is beneficiary, and `claimed: true` |
| Sepolia Etherscan | the contract emitted `Locked` and then `Claimed` for the same lock id |
| Wallet balances | maker lost 20 ZQWX and gained 0.01 ETH; taker gained 20 ZQWX less fees and paid 0.01 ETH plus gas |

That is the same atomic swap as devnet, with Sepolia's real block cadence and
confirmation depth in place of Anvil's instant blocks.

---

## 10. Safety checklist

- [ ] The Ethereum accounts are dedicated test accounts, not mainnet keys.
- [ ] The contract address on the node is the one you deployed, and
      `cast code` or Etherscan shows code there.
- [ ] The node's `/v1/bridge` and the Bridge page both say chain `11155111`.
- [ ] `SCCP_ETH_RPC_URL` is safe to publish, because `/v1/bridge` exposes it.
- [ ] The maker and taker use different SCCP keyfiles and different Ethereum
      accounts.
- [ ] The taker has not cleared browser storage in the middle of a swap; the
      preimage lives there until the ZQWX claim. If it is lost, the swap cannot
      complete: each side must refund after its own timelock.
- [ ] You are using test value only. Sepolia ETH and testnet ZQWX cannot pay for
      anything real, and `EthHtlc` has not had an external audit.
- [ ] You have not pointed a mainnet node at this contract; mainnet is chain
      `1`, not `11155111`.

---

## 11. Quick reference

### Operator setup

```bash
# Sepolia RPC and a funded, disposable deployer key
export SEPOLIA_RPC_URL='https://...'
export DEPLOYER_PRIVATE_KEY=0x...

# One-time contract deploy
cd contracts
forge install foundry-rs/forge-std --no-commit
SCCP_NETWORK=testnet forge script script/Deploy.s.sol:Deploy \
  --rpc-url "$SEPOLIA_RPC_URL" \
  --private-key "$DEPLOYER_PRIVATE_KEY" \
  --broadcast
cd ..

# Point the node at Sepolia and restart it
export SCCP_ETH_RPC_URL="$SEPOLIA_RPC_URL"
export SCCP_ETH_HTLC_CONTRACT=0x...
sccp --network testnet --data-dir data node run --web --port 8645

# Confirm the document the browser will read
curl -sS http://127.0.0.1:8645/v1/bridge | jq
```

For the hosted stack, set the same two values as GitHub Actions repository
variables and redeploy; do not edit the VM's chain database.

### User flow

```text
Sepolia faucet -> MetaMask maker and taker accounts
       |
       v
#/wallet  -> open the two funded testnet SCCP keyfiles
#/bridge -> Sell ZQWX: 20 ZQWX for 0.01 ETH, 24 h expiry
#/bridge -> Buy ZQWX: lock 0.01 ETH for 6 h, publish take
#/bridge -> maker verifies the ETH lock and locks ZQWX
#/bridge -> taker claims ZQWX after 11 SCCP confirmations
#/bridge -> maker claims ETH after the claim is final + 6 ETH confirmations
```

### Commands worth remembering

```bash
sccp bridge info       --node "$SCCP_NODE"
sccp bridge orders     --node "$SCCP_NODE"
sccp bridge order      --order-id "$ORDER_ID" --node "$SCCP_NODE"
sccp bridge htlcs      --hashlock "$HASHLOCK" --node "$SCCP_NODE"
sccp bridge eth-lock-info --lock-id "$LOCK_ID" --node "$SCCP_NODE"

curl -sS "$SCCP_NODE/v1/bridge" | jq
curl -sS "$SCCP_NODE/v1/orders" | jq
curl -sS "$SCCP_NODE/v1/htlcs"  | jq
```

---

## Related documentation

- [`TEST_DEVNET_BRIDGE_DEX.md`](TEST_DEVNET_BRIDGE_DEX.md) — the same bridge on
  Anvil, plus the DEX and Chess960 flows, with a CLI path for both legs.
- [`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) — keyfiles, the
  faucet and the testnet balance rules.
- [`../setup/TESTNET_DEPLOYMENT.md`](../setup/TESTNET_DEPLOYMENT.md) — the
  hosted stack, its GitHub variables and its destructive deploy.
- [`../../contracts/README.md`](../../contracts/README.md) — `EthHtlc`'s
  interface, trust boundary and Foundry test suite.
- [`../../deploy/README.md`](../../deploy/README.md) — the Compose stack and the
  ETH bridge environment.
- [`../../src/web/README.md`](../../src/web/README.md) — the bridge page's
  wallet, order and local-swap behavior.

All pages, by task: [`docs/README.md`](../README.md).

