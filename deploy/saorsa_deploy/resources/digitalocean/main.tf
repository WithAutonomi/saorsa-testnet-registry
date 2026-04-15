resource "digitalocean_droplet" "node_vm" {
  count    = var.vm_count
  name     = "${var.name}-ant-node-${var.region}-${count.index + 1}"
  region   = var.region
  size     = var.droplet_size
  image    = "ubuntu-24-04-x64"
  ssh_keys = var.ssh_key_ids
}

resource "digitalocean_volume" "node_storage" {
  count                   = var.vm_count
  region                  = var.region
  name                    = "${lower(var.name)}-ant-storage-${var.region}-${count.index + 1}"
  size                    = var.attached_volume_size
  initial_filesystem_type = "ext4"
}

resource "digitalocean_volume_attachment" "node_storage_attach" {
  count      = var.vm_count
  droplet_id = digitalocean_droplet.node_vm[count.index].id
  volume_id  = digitalocean_volume.node_storage[count.index].id
}
