/**
 * The `payForQuotes` wallet callback.
 *
 * No private key ever enters WASM. `ant-core` verifies each storage quote
 * (ML-DSA signature, peer binding, price curve), then hands the verified set to
 * this callback and expects a mined transaction back. The two contract calls
 * map one-to-one onto the quote fields:
 *
 *     ERC-20  approve(vault, total)
 *     vault   payForQuotes([{ rewardsAddress, amount, quoteHash }])
 *
 * Verified end to end against a local devnet before this was written.
 */

import {
  createPublicClient,
  createWalletClient,
  defineChain,
  http,
  parseAbi,
  parseAbiItem,
  parseEventLogs,
  type Hex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";

/** The payment identity a node advertises in HELLO. */
export interface PaymentNetwork {
  chain_id: number;
  payment_token_address: string;
  payment_vault_address: string;
}

/** One quote as ant-core hands it to the callback. */
export interface VerifiedQuote {
  /** Lowercase hex, WITHOUT a 0x prefix. */
  quoteHash: string;
  /** Lowercase hex, WITH a 0x prefix. */
  rewardsAddress: string;
  /** Decimal atto, as a string (can exceed Number.MAX_SAFE_INTEGER). */
  amount: string;
}

export interface PaymentResult {
  transactionHash: string;
  totalAmount: string;
}

const ERC20_ABI = parseAbi([
  "function approve(address spender, uint256 amount) returns (bool)",
]);
const VAULT_ABI = parseAbi([
  "function payForQuotes((address rewardsAddress, uint256 amount, bytes32 quoteHash)[] _payments)",
]);

function hexPrefixed(value: string): Hex {
  return (value.startsWith("0x") ? value : `0x${value}`) as Hex;
}

/** Same 20% headroom evmlib's native wallet puts on every vault transaction. */
export function withGasBuffer(estimated: bigint): bigint {
  return (estimated * 120n) / 100n;
}

/**
 * ant-core hands the callback the submission journal hook as a third
 * argument: call `onSubmission(receipt)` as soon as the payment transaction
 * is broadcast (before it is mined) so the upload checkpoint records the hash
 * and a crash between broadcast and receipt cannot lose track of spent
 * funds. The fourth argument is the pending-payment attempt (for recovery
 * wallets); this harness does not resume across restarts and ignores it.
 */
export type OnSubmission = (receipt: PaymentResult) => Promise<unknown> | unknown;

export type PayForQuotes = (
  network: PaymentNetwork,
  quotes: VerifiedQuote[],
  onSubmission?: OnSubmission,
  attempt?: unknown,
) => Promise<PaymentResult>;

/**
 * A merkle batch payment as ant-core prepares it (`MerklePaymentRequest`).
 *
 * Merkle payments are not a wallet feature: they are the batched settlement
 * path -- one `payForMerkleTree` vault transaction covering a whole batch
 * instead of one payment per chunk -- used to cut gas on uploads larger than
 * 256 chunks. ant-core (via evmlib) prepares the canonical calldata and the
 * eligible winning pools in Rust; the JS side only signs, submits, and reads
 * the on-chain outcome back.
 */
export interface MerklePaymentRequest {
  /** Canonical `payForMerkleTree` calldata, 0x-prefixed hex. */
  calldata: string;
  /** Maximum spend across the possible winning pools; the token approval amount. */
  maximumAmount: string;
  depth: number;
  timestamp: number;
  /** Eligible winning pool hashes; the mined event names the actual winner. */
  poolHashes: string[];
}

export interface MerklePaymentResult {
  transactionHash: string;
  winnerPoolHash: string;
  totalAmount: string;
}

export type PayForMerkle = (
  network: PaymentNetwork,
  request: MerklePaymentRequest,
  onSubmission?: OnSubmission,
  attempt?: unknown,
) => Promise<MerklePaymentResult>;

/** The vault event a merkle payment settles through; ant-core needs its args back. */
const MERKLE_PAYMENT_MADE = parseAbiItem(
  "event MerklePaymentMade(bytes32 indexed winnerPoolHash, uint8 depth, uint256 totalAmount, uint64 merklePaymentTimestamp)",
);

/**
 * Build the callback ant-core invokes for each paid upload.
 *
 * The chain is defined from the payment network the NODES advertise rather than
 * from a hardcoded table, so the client cannot pay on a chain the network it is
 * talking to does not use.
 */
function makeClients(secretKey: string, rpcUrl: string, network: PaymentNetwork) {
  const chain = defineChain({
    id: network.chain_id,
    name: `ant-${network.chain_id}`,
    nativeCurrency: { name: "ETH", symbol: "ETH", decimals: 18 },
    rpcUrls: { default: { http: [rpcUrl] } },
  });
  const account = privateKeyToAccount(hexPrefixed(secretKey));
  const wallet = createWalletClient({ account, chain, transport: http(rpcUrl) });
  const pub = createPublicClient({ chain, transport: http(rpcUrl) });
  return { wallet, pub };
}

export function makeWallet(secretKey: string, rpcUrl: string, network: PaymentNetwork): PayForQuotes {
  const { wallet, pub } = makeClients(secretKey, rpcUrl, network);

  return async (
    net: PaymentNetwork,
    quotes: VerifiedQuote[],
    onSubmission?: OnSubmission,
  ): Promise<PaymentResult> => {
    // ant-core enforces both of these after the callback returns, so getting
    // either wrong fails the upload rather than mispaying:
    //  - the total must EXACTLY equal the sum of the quote amounts, and
    //  - a non-empty quote set must come back with a transaction hash.
    const total = quotes.reduce((sum, q) => sum + BigInt(q.amount), 0n);

    const approveHash = await wallet.writeContract({
      address: hexPrefixed(net.payment_token_address),
      abi: ERC20_ABI,
      functionName: "approve",
      args: [hexPrefixed(net.payment_vault_address), total],
    });
    await pub.waitForTransactionReceipt({ hash: approveHash });

    const payHash = await wallet.writeContract({
      address: hexPrefixed(net.payment_vault_address),
      abi: VAULT_ABI,
      functionName: "payForQuotes",
      args: [
        quotes.map((q) => ({
          rewardsAddress: hexPrefixed(q.rewardsAddress),
          amount: BigInt(q.amount),
          quoteHash: hexPrefixed(q.quoteHash),
        })),
      ],
    });
    const receipt: PaymentResult = { transactionHash: payHash, totalAmount: total.toString() };
    // Journal the broadcast before waiting on it, as upstream's browser
    // harness does (`await submitted(receipt); await transaction.wait()`).
    if (onSubmission) await onSubmission(receipt);
    // Wait for the receipt: nodes verify this hash on-chain during put_chunk,
    // so returning an unmined hash makes every subsequent store fail.
    await pub.waitForTransactionReceipt({ hash: payHash });

    return receipt;
  };
}

/**
 * Build the callback ant-core invokes for a merkle batch payment.
 *
 * The contract picks the winning pool on-chain (its selection includes
 * `msg.sender` as entropy), so the actual amount and winner are only known
 * from the mined `MerklePaymentMade` event -- which is why the approval is
 * for `maximumAmount` and the event, not the request, is what is returned.
 * ant-core rejects the receipt if the winner is not one of the eligible
 * pools or the amount exceeds the maximum.
 */
export function makeMerklePayer(secretKey: string, rpcUrl: string, network: PaymentNetwork): PayForMerkle {
  const { wallet, pub } = makeClients(secretKey, rpcUrl, network);

  return async (
    net: PaymentNetwork,
    request: MerklePaymentRequest,
    onSubmission?: OnSubmission,
  ): Promise<MerklePaymentResult> => {
    const vault = hexPrefixed(net.payment_vault_address);
    const maximum = BigInt(request.maximumAmount);

    const approveHash = await wallet.writeContract({
      address: hexPrefixed(net.payment_token_address),
      abi: ERC20_ABI,
      functionName: "approve",
      args: [vault, maximum],
    });
    await pub.waitForTransactionReceipt({ hash: approveHash });

    // The contract selects the winning pool at execution time, so the gas
    // the mined path needs can exceed what a send-time simulation measured.
    // evmlib's native wallet pads its estimate by 20% for exactly this reason
    // (retry.rs, "add 20% buffer to avoid out-of-gas reverts"); without it,
    // 1 of 19 merkle payments on DEV-01-wasm (2026-09-16) ran out of gas at
    // precisely its estimated limit.
    const estimated = await pub.estimateGas({
      account: wallet.account,
      to: vault,
      data: hexPrefixed(request.calldata),
    });
    const payHash = await wallet.sendTransaction({
      to: vault,
      data: hexPrefixed(request.calldata),
      gas: withGasBuffer(estimated),
    });
    // Journal the broadcast before waiting on it; the final amount is not
    // known until the event is mined, so only the hash is recorded here.
    if (onSubmission) await onSubmission({ transactionHash: payHash, totalAmount: request.maximumAmount });
    const receipt = await pub.waitForTransactionReceipt({ hash: payHash });
    if (receipt.status !== "success") {
      throw new Error(`merkle payment ${payHash} reverted`);
    }

    const events = parseEventLogs({
      abi: [MERKLE_PAYMENT_MADE],
      eventName: "MerklePaymentMade",
      logs: receipt.logs.filter((log) => log.address.toLowerCase() === vault.toLowerCase()),
    });
    const made = events[0];
    if (!made) {
      throw new Error(`merkle payment ${payHash} mined without a MerklePaymentMade event`);
    }
    return {
      transactionHash: payHash,
      winnerPoolHash: made.args.winnerPoolHash,
      totalAmount: made.args.totalAmount.toString(),
    };
  };
}
