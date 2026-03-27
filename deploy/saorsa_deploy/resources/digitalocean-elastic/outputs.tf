output "elastic_ip" {
  value = digitalocean_droplet.elastic.ipv4_address
}

output "elastic_droplet_id" {
  value = digitalocean_droplet.elastic.id
}

output "elastic_volume_id" {
  value = digitalocean_volume.elastic_storage.id
}
