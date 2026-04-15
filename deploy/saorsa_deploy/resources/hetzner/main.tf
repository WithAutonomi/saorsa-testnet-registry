resource "hcloud_server" "node_vm" {
  count       = var.vm_count
  name        = "${var.name}-ant-node-${var.region}-${count.index + 1}"
  server_type = var.server_type
  location    = var.region
  image       = "ubuntu-24.04"
  ssh_keys    = var.ssh_key_ids
}

resource "hcloud_volume" "node_storage" {
  count    = var.vm_count
  name     = "${lower(var.name)}-ant-storage-${var.region}-${count.index + 1}"
  size     = var.attached_volume_size
  location = var.region
  format   = "ext4"
}

resource "hcloud_volume_attachment" "node_storage_attach" {
  count     = var.vm_count
  volume_id = hcloud_volume.node_storage[count.index].id
  server_id = hcloud_server.node_vm[count.index].id
  automount = false
}
