# The address everything else must use. For a public-ingest instance that is
# the reserved IP, not the droplet's own: the DNS record points at the reserved
# IP, so a certificate issued against the droplet address would fail validation.
output "elastic_ip" {
  value = var.public_ingest ? (
    var.reserved_ip != "" ? var.reserved_ip : digitalocean_reserved_ip.elastic[0].ip_address
  ) : digitalocean_droplet.elastic.ipv4_address
}

output "elastic_droplet_ip" {
  value = digitalocean_droplet.elastic.ipv4_address
}

output "elastic_reserved_ip" {
  value = var.public_ingest ? (
    var.reserved_ip != "" ? var.reserved_ip : digitalocean_reserved_ip.elastic[0].ip_address
  ) : ""
}

output "elastic_droplet_id" {
  value = digitalocean_droplet.elastic.id
}

output "elastic_volume_id" {
  value = digitalocean_volume.elastic_storage.id
}
