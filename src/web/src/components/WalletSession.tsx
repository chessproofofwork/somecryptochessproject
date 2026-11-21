/**
 * One unlocked wallet for every route in the tab.
 *
 * The wallet used to own its keypair in component state. Play and DEX are now
 * separate pages, so the key has to outlive a tab switch: this provider holds the
 * keyfile index, the in-memory keypair, the optimistic nonce and the idle timer,
 * and every page reads the same session. The provider itself imports no crypto;
 * `unlock` loads the scrypt/AES module on demand, so an explorer visit does not
 * pay for the wallet's key derivation.
 *
 * The keypair is never persisted, never written to a URL and never placed in a
 * cookie. Locking fills its seed buffers with zeroes before dropping the
 * reference. The in-app menu switches with a hash, so the one origin keeps its
 * IndexedDB keyfile and this session stays alive.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { fetchTransaction, submitTransaction } from "../api";
import { useNotice } from "../components/Notices";
import { formatDuration, shortAddress } from "../lib/format";
import type { HybridKeypair } from "../lib/keys";
import type { KeyfileDocument } from "../lib/keystore";
import {
  addPending,
  prunePending,
  readPending,
  settlePending,
  writePending,
  type PendingOptions,
  type PendingTransaction,
} from "../lib/pending";
import type { Transaction } from "../lib/tx";
import {
  readAutoLockSeconds,
  readLastAddress,
  writeAutoLockSeconds,
  writeLastAddress,
} from "../lib/settings";
import {
  deleteKeyfile,
  listKeyfiles,
  renameKeyfile,
  saveKeyfile,
  type StoredKeyfile,
} from "../lib/vault";

export interface WalletSession {
  record: StoredKeyfile;
  keypair: HybridKeypair;
}

export interface WalletSessionValue {
  session: WalletSession | null;
  stored: StoredKeyfile[] | null;
  storeError: Error | null;
  selected: string | null;
  pendingNonce: number | null;
  /** Ledger actions signed here that no block has carried yet. */
  pending: PendingTransaction[];
  autoLockSeconds: number;
  refresh: () => Promise<void>;
  select: (address: string) => void;
  unlock: (record: StoredKeyfile, passphrase: string) => Promise<void>;
  save: (document: KeyfileDocument, keypair: HybridKeypair) => Promise<void>;
  forget: (address: string) => Promise<void>;
  rename: (label: string) => Promise<void>;
  lock: (reason?: string) => void;
  setPendingNonce: (nonce: number | null) => void;
  /**
   * Sign-and-send through one bookkeeping path.
   *
   * The row is written before the request leaves the tab, so History can show it
   * while the node is still deciding. A failed submission removes it; an accepted
   * one keeps it with the txid the node answered.
   */
  sendPending: (tx: Transaction, options: PendingOptions) => Promise<{ txid: string }>;
  /** Record a transaction the node built, such as a faucet grant. */
  recordPending: (entry: PendingTransaction) => void;
  /** Retire rows whose nonce the committed account state has passed. */
  prunePendingAtNonce: (committedNonce: number) => void;
  setAutoLockSeconds: (seconds: number) => void;
}

const WalletSessionContext = createContext<WalletSessionValue | null>(null);

/** How often the idle timer checks whether the tab has been left alone. */
const AUTO_LOCK_TICK_MS = 5000;

/** How often the session asks the node whether a pending row has been mined. */
const PENDING_POLL_MS = 5000;

function describe(failure: unknown): Error {
  return failure instanceof Error ? failure : new Error(String(failure));
}

function wipe(keypair: HybridKeypair): void {
  keypair.seed.fill(0);
  keypair.ed25519.seed.fill(0);
}

export function WalletSessionProvider({ children }: { children: ReactNode }): ReactNode {
  const notify = useNotice();
  const [stored, setStored] = useState<StoredKeyfile[] | null>(null);
  const [storeError, setStoreError] = useState<Error | null>(null);
  const [selected, setSelected] = useState<string | null>(readLastAddress);
  const [session, setSession] = useState<WalletSession | null>(null);
  const [pendingNonce, setPendingNonce] = useState<number | null>(null);
  const [pending, setPending] = useState<PendingTransaction[]>([]);
  const [autoLockSeconds, setAutoLockState] = useState(readAutoLockSeconds);
  const pendingRef = useRef<PendingTransaction[]>([]);
  pendingRef.current = pending;

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setStored(await listKeyfiles());
      setStoreError(null);
    } catch (failure) {
      setStoreError(describe(failure));
      setStored([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Pending rows belong to one key. Locking clears the view; unlocking reads the
  // book back, because a transaction outlives the passphrase that signed it.
  const sessionAddress = session?.record.address ?? null;
  const sessionNetwork = session?.record.network ?? null;
  useEffect(() => {
    if (sessionAddress === null || sessionNetwork === null) {
      setPending([]);
      return;
    }
    setPending(readPending(sessionAddress, sessionNetwork));
  }, [sessionAddress, sessionNetwork]);

  const select = useCallback((address: string): void => {
    setSelected(address);
    writeLastAddress(address);
  }, []);

  const lock = useCallback(
    (reason?: string): void => {
      setSession((current) => {
        if (current !== null) wipe(current.keypair);
        return null;
      });
      setPendingNonce(null);
      setPending([]);
      if (reason !== undefined) notify(reason);
    },
    [notify],
  );


  const adopt = useCallback(
    (record: StoredKeyfile, keypair: HybridKeypair): void => {
      setSession((current) => {
        if (current !== null && current.keypair !== keypair) wipe(current.keypair);
        return { record, keypair };
      });
      setSelected(record.address);
      writeLastAddress(record.address);
      setPendingNonce(null);
      notify(`unlocked ${shortAddress(record.address)}`);
    },
    [notify],
  );

  const unlock = useCallback(
    async (record: StoredKeyfile, passphrase: string): Promise<void> => {
      const { unlockKeyfile } = await import("../lib/keystore");
      adopt(record, await unlockKeyfile(record.document, passphrase));
    },
    [adopt],
  );

  const save = useCallback(
    async (document: KeyfileDocument, keypair: HybridKeypair): Promise<void> => {
      adopt(await saveKeyfile(document), keypair);
      await refresh();
    },
    [adopt, refresh],
  );

  const forget = useCallback(
    async (target: string): Promise<void> => {
      await deleteKeyfile(target);
      setSession((current) => {
        if (current?.record.address !== target) return current;
        wipe(current.keypair);
        return null;
      });
      setPendingNonce(null);
      setPending([]);
      setSelected((current) => {
        if (current !== target) return current;
        writeLastAddress(null);
        return null;
      });
      await refresh();
      notify(`forgot ${shortAddress(target)}`);
    },
    [notify, refresh],
  );

  const rename = useCallback(
    async (label: string): Promise<void> => {
      if (session === null) return;
      try {
        const record = await renameKeyfile(session.record.address, label);
        setSession((current) => (current === null ? current : { ...current, record }));
        await refresh();
        notify(record.label === "" ? "nickname cleared" : `nickname set to ${record.label}`);
      } catch (failure) {
        notify(describe(failure).message);
      }
    },
    [notify, refresh, session],
  );

  const setAutoLockSeconds = useCallback((seconds: number): void => {
    setAutoLockState(seconds);
    writeAutoLockSeconds(seconds);
  }, []);

  const recordPending = useCallback(
    (entry: PendingTransaction): void => {
      if (session === null) return;
      const { address, network } = session.record;
      const next = addPending(readPending(address, network), entry);
      writePending(address, network, next);
      setPending(next);
    },
    [session],
  );

  const settlePendingEntry = useCallback(
    (draftTxid: string, acceptedTxid: string): void => {
      if (session === null) return;
      const { address, network } = session.record;
      const next = settlePending(readPending(address, network), draftTxid, acceptedTxid);
      writePending(address, network, next);
      setPending(next);
    },
    [session],
  );

  const discardPending = useCallback(
    (txid: string): void => {
      if (session === null) return;
      const { address, network } = session.record;
      const next = readPending(address, network).filter((entry) => entry.txid !== txid);
      writePending(address, network, next);
      setPending(next);
    },
    [session],
  );

  /**
   * Sign-and-send through one bookkeeping path.
   *
   * The draft row is written before the request leaves the tab. If the node
   * refuses it -- or the request never reaches the node -- the row is removed
   * again, so History never claims a transaction that cannot be found.
   */
  const sendPending = useCallback(
    async (tx: Transaction, options: PendingOptions): Promise<{ txid: string }> => {
      if (session === null) throw new Error("unlock a wallet before sending");
      // Load the transaction/txid code only when there is something to send;
      // the provider is mounted on every explorer page and must stay light.
      const { pendingFromTransaction } = await import("../lib/pendingTx");
      const draft = pendingFromTransaction(tx, { ...options, status: "submitting" });
      recordPending(draft);
      try {
        const accepted = await submitTransaction({ ...tx, signature: tx.signature ?? null });
        settlePendingEntry(draft.txid, accepted.txid);
        return { txid: accepted.txid };
      } catch (failure) {
        discardPending(draft.txid);
        throw failure;
      }
    },
    [discardPending, recordPending, session, settlePendingEntry],
  );

  /**
   * Retire rows whose nonce the committed account state has already passed.
   *
   * The node's tx-status endpoint is the primary check; this is the fallback for
   * a node that has caught up between polls, and it is what makes a row leave
   * History as soon as the balance page shows the new nonce.
   */
  const prunePendingAtNonce = useCallback(
    (committedNonce: number): void => {
      if (session === null) return;
      const { address, network } = session.record;
      const current = readPending(address, network);
      if (current.length === 0) return;
      const included = current.filter(
        (entry) => entry.nonce !== null && entry.nonce < committedNonce,
      );
      const next = prunePending(current, committedNonce);
      if (next.length === current.length) return;
      writePending(address, network, next);
      setPending(next);
      for (const entry of included) {
        const nonce = entry.nonce === null ? "" : ` (nonce ${entry.nonce})`;
        notify(`included: ${entry.label}${nonce}`);
      }
    },
    [notify, session],
  );

  // Auto-lock: any pointer or key in the tab counts as activity; silence for the
  // chosen window locks. A timer, not one `setTimeout`, so a sleeping tab wakes up
  // hours later and still notices that the window has passed.
  const lastActivity = useRef(Date.now());
  useEffect(() => {
    if (session === null) return undefined;
    lastActivity.current = Date.now();
    const bump = (): void => {
      lastActivity.current = Date.now();
    };
    globalThis.addEventListener("pointerdown", bump);
    globalThis.addEventListener("keydown", bump);
    const timer = setInterval(() => {
      if (Date.now() - lastActivity.current >= autoLockSeconds * 1000) {
        lock(`locked after ${formatDuration(autoLockSeconds)} idle`);
      }
    }, AUTO_LOCK_TICK_MS);
    return () => {
      globalThis.removeEventListener("pointerdown", bump);
      globalThis.removeEventListener("keydown", bump);
      clearInterval(timer);
    };
  }, [autoLockSeconds, lock, session]);

  // The nonce fallback needs a committed account read; the tx-status endpoint
  // knows about the transaction itself. Ask it while any row is waiting, so a
  // pending row becomes "included" even when the wallet page is not open.
  useEffect(() => {
    if (session === null || pending.length === 0) return undefined;
    let active = true;
    const controller = new AbortController();
    const check = async (): Promise<void> => {
      const current = pendingRef.current;
      if (current.length === 0) return;
      const results = await Promise.all(
        current.map(async (entry) => {
          try {
            return { entry, record: await fetchTransaction(entry.txid, controller.signal) };
          } catch {
            return { entry, record: null };
          }
        }),
      );
      if (!active) return;
      const included = results
        .filter((result) => result.record !== null && result.record.height !== null)
        .map((result) => result.entry);
      const includedIds = new Set(included.map((entry) => entry.txid));
      const { address, network } = session.record;
      // Re-read after the await: a send may have recorded a newer nonce while
      // these status requests were in flight, and a stale book would drop it.
      const latest = readPending(address, network);
      const next = prunePending(
        latest.filter((entry) => !includedIds.has(entry.txid)),
        -1,
        Date.now(),
      );
      if (next.length === latest.length) return;
      writePending(address, network, next);
      setPending(next);
      for (const entry of included) {
        const nonce = entry.nonce === null ? "" : ` (nonce ${entry.nonce})`;
        notify(`included: ${entry.label}${nonce}`);
      }
    };
    const timer = setInterval(() => void check(), PENDING_POLL_MS);
    void check();
    return () => {
      active = false;
      controller.abort();
      clearInterval(timer);
    };
  }, [notify, pending.length, session]);

  const value = useMemo<WalletSessionValue>(
    () => ({
      session,
      stored,
      storeError,
      selected,
      pendingNonce,
      pending,
      autoLockSeconds,
      refresh,
      select,
      unlock,
      save,
      forget,
      rename,
      lock,
      setPendingNonce,
      sendPending,
      recordPending,
      prunePendingAtNonce,
      setAutoLockSeconds,
    }),
    [
      autoLockSeconds,
      forget,
      lock,
      pending,
      pendingNonce,
      prunePendingAtNonce,
      recordPending,
      refresh,
      rename,
      save,
      select,
      selected,
      sendPending,
      session,
      storeError,
      stored,
      unlock,
    ],
  );

  return (
    <WalletSessionContext.Provider value={value}>{children}</WalletSessionContext.Provider>
  );
}

export function useWalletSession(): WalletSessionValue {
  const value = useContext(WalletSessionContext);
  if (value === null) {
    throw new Error("useWalletSession must be used inside WalletSessionProvider");
  }
  return value;
}
