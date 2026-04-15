output "vm_ips" {
  value = hcloud_server.node_vm[*].ipv4_address
}

output "vm_ids" {
  value = hcloud_server.node_vm[*].id
}

output "volume_ids" {
  value = hcloud_volume.node_storage[*].id
}
