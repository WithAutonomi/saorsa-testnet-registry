resource "digitalocean_droplet" "bootstrap_vm" {
  name     = "${var.name}-ant-bootstrap-${var.bootstrap_index}"
  region   = var.region
  size     = var.droplet_size
  image    = "ubuntu-24-04-x64"
  ssh_keys = var.ssh_key_ids
}

resource "digitalocean_volume" "bootstrap_storage" {
  region                  = var.region
  name                    = "${lower(var.name)}-ant-bootstrap-storage-${var.bootstrap_index}"
  size                    = var.attached_volume_size
  initial_filesystem_type = "ext4"
}

resource "digitalocean_volume_attachment" "bootstrap_storage_attach" {
  droplet_id = digitalocean_droplet.bootstrap_vm.id
  volume_id  = digitalocean_volume.bootstrap_storage.id
}
