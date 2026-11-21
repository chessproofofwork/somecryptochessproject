import type { HybridKeypair } from "../../lib/keys";
import type { PendingOptions } from "../../lib/pending";
import type { Transaction } from "../../lib/tx";
import type {
  AccountView,
  BridgeView,
  NetworkParams,
  NodeStatus,
  OrderView,
} from "../../types";

/** Everything every bridge panel needs from the page's one wallet and node. */
export interface BridgeContext {
  network: string;
  address: string;
  keypair: HybridKeypair;
  account: AccountView;
  params: NetworkParams;
  status: NodeStatus;
  bridge: BridgeView;
  orders: OrderView[];
  nonce: number;
  reload: () => void;
  setPendingNonce: (nonce: number | null) => void;
  sendPending: (tx: Transaction, options: PendingOptions) => Promise<{ txid: string }>;
  notify: (message: string) => void;
  ethAccount: string | null;
  connectEth: () => Promise<string | null>;
}
