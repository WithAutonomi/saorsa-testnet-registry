output "droplet_ips" {
  value = digitalocean_droplet.node_vm[*].ipv4_address
}

output "droplet_ids" {
  value = digitalocean_droplet.node_vm[*].id
}

output "volume_ids" {
  value = digitalocean_volume.node_storage[*].id
}
