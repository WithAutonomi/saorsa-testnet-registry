import assert from "node:assert/strict";
import { test } from "node:test";

import { fetchPaymentNetwork } from "../dist/client.js";

// The harness types ant-core through hand-written interfaces, so `tsc` cannot
// see an upstream API change: these fakes are the only thing that can. Each one
// is the shape of a real bundle — `main` (PR #206) dropped `BrowserNodeClient`
// and added `BrowserNetworkClient.connect()`, `ant-cli-v0.3.8` has only the
// former — and a bundle built from either ref must upload.

const PAYMENT = {
  chain_id: 42161,
  payment_token_address: "0xToken",
  payment_vault_address: "0xVault",
};

/** ant-client `main`: HELLO comes back from the network client itself. */
function mainShape(hello = { payment: PAYMENT }) {
  const client = {
    closed: false,
    connects: 0,
    async connect() {
      client.connects += 1;
      return hello;
    },
    close() {
      client.closed = true;
    },
  };
  return { core: {}, client };
}

/** `ant-cli-v0.3.8`: a throwaway node client, closed once HELLO is read. */
function legacyShape(hello = { payment: PAYMENT }) {
  const session = {
    closed: false,
    freed: false,
    async hello() {
      return hello;
    },
    close() {
      session.closed = true;
    },
    free() {
      session.freed = true;
    },
  };
  const node = { freed: false, endpoint: null };
  const core = {
    BrowserNodeClient: class {
      constructor(endpoint) {
        node.endpoint = endpoint;
      }
      async connect() {
        return session;
      }
      free() {
        node.freed = true;
      }
    },
  };
  const client = {
    closed: false,
    close() {
      client.closed = true;
    },
  };
  return { core, client, session, node };
}

function collect() {
  const lines = [];
  return { lines, log: (line) => lines.push(line) };
}

test("the main shape reads the payment network from connect()", async () => {
  const { core, client } = mainShape();
  const { lines, log } = collect();

  const payment = await fetchPaymentNetwork(core, client, "seed-endpoint", log);

  assert.deepEqual(payment, PAYMENT);
  assert.equal(client.connects, 1);
  assert.deepEqual(lines, ["payment network via BrowserNetworkClient.connect()"]);
});

test("the main shape leaves the client open for the upload to reuse", async () => {
  // `close()` is terminal on `main` ("WebRTC client pool is closed"), and the
  // authenticated session stays in this client's pool — closing it here would
  // make every following upload unrecoverable.
  const { core, client } = mainShape();

  await fetchPaymentNetwork(core, client, "seed-endpoint", collect().log);

  assert.equal(client.closed, false);
});

test("the legacy shape reads the payment network from the node session", async () => {
  const { core, client, node } = legacyShape();
  const { lines, log } = collect();

  const payment = await fetchPaymentNetwork(core, client, "seed-endpoint", log);

  assert.deepEqual(payment, PAYMENT);
  assert.equal(node.endpoint, "seed-endpoint");
  assert.deepEqual(lines, ["payment network via BrowserNodeClient (legacy)"]);
});

test("the legacy shape closes and frees what it created, and only that", async () => {
  const { core, client, session, node } = legacyShape();

  await fetchPaymentNetwork(core, client, "seed-endpoint", collect().log);

  assert.equal(session.closed, true);
  assert.equal(session.freed, true);
  assert.equal(node.freed, true);
  assert.equal(client.closed, false);
});

test("connect() wins when a bundle exposes both", async () => {
  const { core } = legacyShape();
  const { client } = mainShape();
  const { lines, log } = collect();

  await fetchPaymentNetwork(core, client, "seed-endpoint", log);

  assert.equal(client.connects, 1);
  assert.deepEqual(lines, ["payment network via BrowserNetworkClient.connect()"]);
});

test("neither member present is an error naming both", async () => {
  const { client } = legacyShape();

  await assert.rejects(
    () => fetchPaymentNetwork({}, client, "seed-endpoint", collect().log),
    (e) => {
      assert.match(e.message, /BrowserNetworkClient\.connect\(\)/);
      assert.match(e.message, /BrowserNodeClient/);
      return true;
    },
  );
});

test("the payment_network key is still honoured on the new path", async () => {
  const { core, client } = mainShape({ payment_network: PAYMENT });

  assert.deepEqual(
    await fetchPaymentNetwork(core, client, "seed-endpoint", collect().log),
    PAYMENT,
  );
});

test("the payment_network key is still honoured on the legacy path", async () => {
  const { core, client } = legacyShape({ payment_network: PAYMENT });

  assert.deepEqual(
    await fetchPaymentNetwork(core, client, "seed-endpoint", collect().log),
    PAYMENT,
  );
});

test("a HELLO with no payment network is an error on both paths", async () => {
  for (const shape of [mainShape({ peer_id: "p" }), legacyShape({ peer_id: "p" })]) {
    await assert.rejects(
      () => fetchPaymentNetwork(shape.core, shape.client, "seed-endpoint", collect().log),
      /no payment network/,
    );
  }
});
