output "monitoring_ip" {
  value = digitalocean_droplet.monitoring.ipv4_address
}

output "monitoring_droplet_id" {
  value = digitalocean_droplet.monitoring.id
}

output "monitoring_volume_id" {
  value = digitalocean_volume.monitoring_storage.id
}
