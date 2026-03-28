output "server_ip" {
  value = hcloud_server.production_node.ipv4_address
}

output "server_id" {
  value = hcloud_server.production_node.id
}

output "volume_a_id" {
  value = hcloud_volume.stripe_a.id
}

output "volume_b_id" {
  value = hcloud_volume.stripe_b.id
}
