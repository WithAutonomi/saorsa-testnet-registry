resource "digitalocean_droplet" "client_vm" {
  name     = "${var.name}-ant-client-${var.role}-${var.region}-${var.client_index}"
  region   = var.region
  size     = var.droplet_size
  image    = "ubuntu-24-04-x64"
  ssh_keys = var.ssh_key_ids
}
