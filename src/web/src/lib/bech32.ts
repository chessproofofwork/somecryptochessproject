/**
 * BIP-173 bech32, the only encoding SCCP addresses use.
 *
 * `sccp/crypto/address.py` encodes a 33-byte program (version byte + 32-byte
 * hash) with the *primitives*, not the segwit helpers, because those hard-code
 * witness lengths. This is the same subset: polymod, hrp expansion, convertbits
 * and the checksum, written out so a browser can derive an address from a
 * public key without shipping a bech32 dependency.
 *
 * There is one program version: version 1 over the complete SHA-256 digest of a
 * hybrid Ed25519 + ML-DSA-65 public key. A 21-byte version-0 program (the
 * retired truncated-Ed25519 address) is rejected rather than decoded.
 */

const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";

const GENERATOR = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];

/** The only program version byte (`ADDRESS_VERSION` in Python). */
export const ADDRESS_VERSION = 1;

/** Address hash length in bytes: the complete SHA-256 digest, not a prefix. */
export const HASH_SIZE = 32;

/** `version || hash`, the exact 33 bytes the bech32 string commits to. */
export const PROGRAM_SIZE = 1 + HASH_SIZE;

/** BIP-173's limit for the whole string, HRP and checksum included. */
export const MAX_ADDRESS_LENGTH = 90;

export class Bech32Error extends Error {
  override name = "Bech32Error";
}

function polymod(values: readonly number[]): number {
  let checksum = 1;
  for (const value of values) {
    const top = checksum >>> 25;
    checksum = ((checksum & 0x1ffffff) << 5) ^ value;
    for (let bit = 0; bit < 5; bit += 1) {
      if (((top >>> bit) & 1) === 1) {
        checksum ^= GENERATOR[bit] as number;
      }
    }
  }
  return checksum >>> 0;
}

function hrpExpand(hrp: string): number[] {
  const out: number[] = [];
  for (const char of hrp) out.push(char.charCodeAt(0) >> 5);
  out.push(0);
  for (const char of hrp) out.push(char.charCodeAt(0) & 31);
  return out;
}

/**
 * Regroup `data` from `from`-bit to `to`-bit units (BIP-173 `convertbits`).
 *
 * Returns `null` for the padding cases Python rejects, so a caller cannot
 * silently produce an address from a malformed program.
 */
export function convertBits(
  data: ArrayLike<number>,
  from: number,
  to: number,
  pad: boolean,
): number[] | null {
  let accumulator = 0;
  let bits = 0;
  const out: number[] = [];
  const maxValue = (1 << to) - 1;
  const maxAccumulator = (1 << (from + to - 1)) - 1;
  for (let index = 0; index < data.length; index += 1) {
    const value = data[index] as number;
    if (value < 0 || value >> from !== 0) return null;
    accumulator = ((accumulator << from) | value) & maxAccumulator;
    bits += from;
    while (bits >= to) {
      bits -= to;
      out.push((accumulator >> bits) & maxValue);
    }
  }
  if (pad) {
    if (bits > 0) out.push((accumulator << (to - bits)) & maxValue);
  } else if (bits >= from || ((accumulator << (to - bits)) & maxValue) !== 0) {
    return null;
  }
  return out;
}

/** Encode 5-bit `words` under `hrp` with the bech32 checksum. */
export function bech32Encode(hrp: string, words: readonly number[]): string {
  const values = [...hrpExpand(hrp), ...words];
  for (let index = 0; index < 6; index += 1) values.push(0);
  const checksum = polymod(values) ^ 1;
  let out = `${hrp}1`;
  for (const word of words) {
    if (word < 0 || word > 31) throw new Bech32Error(`word ${word} is not 5 bits`);
    out += CHARSET[word];
  }
  for (let index = 0; index < 6; index += 1) {
    out += CHARSET[(checksum >>> (5 * (5 - index))) & 31];
  }
  return out;
}

/** Encode a raw 33-byte address program as a lowercase bech32 address. */
export function encodeProgram(hrp: string, program: Uint8Array): string {
  if (program.length !== PROGRAM_SIZE) {
    throw new Bech32Error(`address program must be ${PROGRAM_SIZE} bytes`);
  }
  if (program[0] !== ADDRESS_VERSION) {
    throw new Bech32Error(`unsupported address version ${program[0] as number}`);
  }
  const words = convertBits(program, 8, 5, true);
  if (words === null) throw new Bech32Error("cannot convert address program to 5-bit words");
  const address = bech32Encode(hrp, words);
  if (address.length > MAX_ADDRESS_LENGTH) {
    throw new Bech32Error(`address exceeds the ${MAX_ADDRESS_LENGTH}-character limit`);
  }
  return address;
}

/** A parsed address: its network prefix and the 33-byte program it commits to. */
export interface DecodedAddress {
  hrp: string;
  version: number;
  program: Uint8Array;
}

/**
 * Decode a bech32 address into the program it commits to.
 *
 * The checksum and the program layout are both checked, so an explorer search
 * can tell a real address from a lookalike before it asks the node. Returns
 * `null` for anything this module's encoder would not have produced; callers
 * decide whether a network prefix mismatch is a miss or an error.
 *
 * An all-uppercase address is valid bech32 and decodes to the same bytes as its
 * lowercase spelling. The chain stores addresses lowercase, so callers that are
 * going to use the address as a key should lower it after decoding.
 */
export function decodeAddress(value: string): DecodedAddress | null {
  if (value.length === 0 || value.length > MAX_ADDRESS_LENGTH) return null;
  if (value !== value.toLowerCase() && value !== value.toUpperCase()) return null;

  const lower = value.toLowerCase();
  const separator = lower.lastIndexOf("1");
  if (separator < 1 || separator + 7 > lower.length) return null;

  const hrp = lower.slice(0, separator);
  const words: number[] = [];
  for (const char of lower.slice(separator + 1)) {
    const word = CHARSET.indexOf(char);
    if (word < 0) return null;
    words.push(word);
  }
  if (polymod([...hrpExpand(hrp), ...words]) !== 1) return null;

  // The last six words are the checksum; the rest is the address program.
  const program = convertBits(words.slice(0, -6), 5, 8, false);
  if (
    program === null ||
    program.length !== PROGRAM_SIZE ||
    program[0] !== ADDRESS_VERSION
  ) {
    return null;
  }
  return { hrp, version: ADDRESS_VERSION, program: Uint8Array.from(program) };
}

/** The human-readable prefix each network uses (`NetworkParams.address_hrp`). */
export const ADDRESS_HRP: Readonly<Record<string, string>> = {
  mainnet: "sccp",
  testnet: "sccpt",
  devnet: "sccpt",
};

/** The HRP for a network id, defaulting to the testnet prefix. */
export function addressHrp(network: string): string {
  return ADDRESS_HRP[network] ?? "sccpt";
}
