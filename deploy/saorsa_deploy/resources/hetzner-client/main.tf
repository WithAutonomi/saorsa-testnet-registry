resource "hcloud_server" "client_vm" {
  name        = "${var.name}-ant-client-${var.role}-${var.region}-${var.client_index}"
  server_type = var.server_type
  location    = var.region
  image       = "ubuntu-24.04"
  ssh_keys    = var.ssh_key_ids
}
