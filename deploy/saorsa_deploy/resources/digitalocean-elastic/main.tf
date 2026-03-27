resource "digitalocean_droplet" "elastic" {
  name     = "${var.name}-ant-elastic-${var.region}"
  region   = var.region
  size     = var.droplet_size
  image    = "ubuntu-24-04-x64"
  ssh_keys = var.ssh_key_ids
}

resource "digitalocean_volume" "elastic_storage" {
  region                  = var.region
  name                    = "${lower(var.name)}-ant-elastic-storage-${var.region}"
  size                    = var.volume_size
  initial_filesystem_type = "ext4"
}

resource "digitalocean_volume_attachment" "elastic_storage_attach" {
  droplet_id = digitalocean_droplet.elastic.id
  volume_id  = digitalocean_volume.elastic_storage.id
}
