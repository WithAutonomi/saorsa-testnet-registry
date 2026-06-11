output "monitoring_ip" {
  value = digitalocean_droplet.monitoring.ipv4_address
}

output "grafana_ip" {
  value = var.enable_grafana ? digitalocean_droplet.grafana[0].ipv4_address : ""
}

output "monitoring_droplet_id" {
  value = digitalocean_droplet.monitoring.id
}

output "grafana_droplet_id" {
  value = var.enable_grafana ? digitalocean_droplet.grafana[0].id : ""
}

output "monitoring_volume_id" {
  value = digitalocean_volume.monitoring_storage.id
}
