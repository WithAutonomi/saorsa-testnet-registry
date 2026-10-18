/**
 * Limits that come from the network, not from this harness.
 *
 * A node admits at most four WebRTC sessions per source IPv4 address
 * (`max_connections_per_ip`, a `>=` check in saorsa-transport's
 * `src/webrtc/direct.rs`), and one browser client holds one session per node,
 * so a fifth instance on a VM is refused by every node it dials.
 *
 * Kept in a module of its own so the number appears once on the VM side;
 * `saorsa_deploy.client_roles.BROWSER_MAX_INSTANCES` is the deploy-side copy
 * and the Python suite asserts the two agree.
 */

export const BROWSER_MAX_INSTANCES = 4;
