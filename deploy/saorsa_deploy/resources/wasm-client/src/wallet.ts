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

export type PayForQuotes = (
  network: PaymentNetwork,
  quotes: VerifiedQuote[],
) => Promise<PaymentResult>;

/**
 * Build the callback ant-core invokes for each paid upload.
 *
 * The chain is defined from the payment network the NODES advertise rather than
 * from a hardcoded table, so the client cannot pay on a chain the network it is
 * talking to does not use.
 */
export function makeWallet(secretKey: string, rpcUrl: string, network: PaymentNetwork): PayForQuotes {
  const chain = defineChain({
    id: network.chain_id,
    name: `ant-${network.chain_id}`,
    nativeCurrency: { name: "ETH", symbol: "ETH", decimals: 18 },
    rpcUrls: { default: { http: [rpcUrl] } },
  });
  const account = privateKeyToAccount(hexPrefixed(secretKey));
  const wallet = createWalletClient({ account, chain, transport: http(rpcUrl) });
  const pub = createPublicClient({ chain, transport: http(rpcUrl) });

  return async (net: PaymentNetwork, quotes: VerifiedQuote[]): Promise<PaymentResult> => {
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
    // Wait for the receipt: nodes verify this hash on-chain during put_chunk,
    // so returning an unmined hash makes every subsequent store fail.
    await pub.waitForTransactionReceipt({ hash: payHash });

    return { transactionHash: payHash, totalAmount: total.toString() };
  };
}
